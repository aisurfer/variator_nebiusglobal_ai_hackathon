"""variator-core: the FastMCP server.

All tools except ``research_business``, ``case_from_question`` and ``explain`` are local code (0 LLM tokens).
They return handles (``ds1``, ``case2``, ``run3``) and compact summaries; the arrays
stay in the server ``Store``.

Run: ``python -m variator.mcp`` (stdio) or ``python -m variator.mcp --http 8000``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from fastmcp import Context, FastMCP
from pydantic import Field

from variator.adapters import Dataset, read_csv, read_json, read_source, read_url
from variator.containers import RECIPES, DockerCLI, Runner, run_recipe
from variator.containers.findings import DefectDojo
from variator.containers.findings import summary as findings_summary
from variator.containers.findings import write_scan as write_findings
from variator.containers.recipes import GROUPS as RECIPE_GROUPS
from variator.containers.recipes import expand as expand_recipes
from variator.domain import RagThresholds, apply_scenarios
from variator.mcda import best_path as summarize_path
from variator.mcda import rank as rank_options
from variator.mcp.case import Case, apply_patch, dataset_facts, variable_quality
from variator.mcp.guides import GUIDES
from variator.mcp.plugins import Plugin, discover, mount_all, remote_servers
from variator.mcp.sessions import OwnerMiddleware
from variator.mcp.store import Store
from variator.quality import DEFAULT_THRESHOLD, score_series, select_trend, simplify, swing_shares
from variator.rag import rag_report, resolve_thresholds
from variator.rag import tunnel as tunnel_map
from variator.report import report_payload, write_report
from variator.research import (
    USD_PER_CREDIT,
    TavilyCache,
    TavilyClient,
    business_profile_queries,
    gather_external_facts,
)
from variator.research.models import ResearchBundle, SearchDepth
from variator.router import DecisionCache, NemotronBackend, Resolver
from variator.rules import RuleError, load_pack
from variator.simulation import simulate_tensor
from variator.telemetry import StatsMiddleware, StatsStore

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Decision analysis. Flow: ingest -> research_business (for external business context) -> "
    "case_from_template | case_from_question -> rules_add -> simulate "
    "-> rank | rag | tunnel -> report. Weight changes: rank (0 tokens). Low data quality: simplify. "
    "More tools: tools_enable(groups) with 'code', 'ops', a plugin name or 'all'. "
    "Details: resource variator://guide/{rules|patch|flow}."
)
MAX_SCENARIOS = 200_000
ENV_TOOLS = "VARIATOR_TOOLS"  # "all" shows every tool at once (clients without list_changed)
# Tool tags: "core" is visible by default; "build" and "analyze" are the agent stages.
BUILD, ANALYZE, BOTH = {"core", "build"}, {"core", "analyze"}, {"core", "build", "analyze"}


def _sig(x: float, digits: int = 4) -> float:
    return float(f"{float(x):.{digits}g}")


class Services:
    """Shared state of one server process."""

    def __init__(
        self,
        plugins: dict[str, Plugin],
        *,
        store: Store | None = None,
        resolver: Resolver | None = None,
        llm: Any = None,
        report_dir: Path | None = None,
        runner: Runner | None = None,
        stats: StatsStore | None = None,
        isolate: bool = False,
        tavily: TavilyClient | None = None,
    ):
        self.plugins = plugins
        self.isolate = isolate  # handles belong to the owner that made them (variator.mcp.sessions)
        self.defectdojo: Any = None  # tests put a client here; else built from the env per call
        self.runner = runner or DockerCLI()
        self.store = store or Store()
        self.stats = stats
        self._llm = llm
        self._tavily = tavily
        if llm is not None and stats is not None:
            stats.attach(llm.ledger)
        self.resolver = resolver or Resolver(
            [NemotronBackend(llm)] if llm is not None else [], cache=DecisionCache(Path(".cache") / "router")
        )
        self.report_dir = report_dir or Path("reports")

    @property
    def tavily(self) -> TavilyClient:
        if self._tavily is None:
            self._tavily = TavilyClient(cache=TavilyCache.from_env())
        return self._tavily

    @property
    def llm(self) -> Any:
        if self._llm is None:
            from variator.llm import TokenFactoryClient

            self._llm = TokenFactoryClient()
            if self.stats is not None:
                self.stats.attach(self._llm.ledger)
        return self._llm

    def plugin(self, name: str) -> Plugin:
        if name not in self.plugins:
            raise KeyError(f"unknown plugin {name!r} (known: {', '.join(self.plugins) or 'none'})")
        return self.plugins[name]

    def new_case(
        self,
        model: Any,
        *,
        plugins: list[str],
        datasets: list[str],
        notes: list[str],
        template: str | None = None,
    ) -> Case:
        for name in plugins:
            plugin = self.plugin(name)
            if plugin.extend is not None and not _has_plugin_axes(model, plugin):
                model = plugin.extend(model)
                notes.append(f"plugin {name}: axes added {[c.name for c in plugin.criteria]}")
        case = Case(self.store.new_id("case"), model, plugins=plugins, datasets=datasets, notes=notes)
        for name in plugins:
            for pack in self.plugin(name).rule_packs:
                if pack.templates is not None and template not in pack.templates:
                    continue  # the pack belongs to another template of the plugin
                trial = case.packs + [pack]
                try:
                    Case(case.id, model, packs=trial).rules()
                except RuleError as exc:
                    notes.append(f"rule pack {pack.pack} skipped: {exc}")
                    continue
                case.packs = trial
        ds = [self.store.dataset(d) for d in datasets]
        case.quality, quality_notes = variable_quality(model, ds, self.resolver)
        case.notes.extend(quality_notes)
        self.store.save_case(case)
        return case


def _has_plugin_axes(model: Any, plugin: Plugin) -> bool:
    names = {c.name for c in model.criteria}
    return bool(plugin.criteria) and all(c.name in names for c in plugin.criteria)


def _run_summary(run_id: str, run: Any) -> dict[str, Any]:
    mean, p5, p95 = run.values.mean(axis=0), *np.quantile(run.values, [0.05, 0.95], axis=0)
    return {
        "run": run_id,
        "n_scenarios": run.n_scenarios,
        "backend": run.backend,
        "elapsed_s": round(run.elapsed_s, 3),
        "criteria": [c.name for c in run.criteria],
        "options": {
            o: {
                c.name: [_sig(mean[j, k]), _sig(p5[j, k]), _sig(p95[j, k])]
                for k, c in enumerate(run.criteria)
            }
            for j, o in enumerate(run.option_names)
        },
        "format": "criterion: [mean, p5, p95]",
        "checks_failed": {
            r: dict(zip(run.option_names, (round(s, 4) for s in shares), strict=True))
            for r, shares in run.check_failures.items()
            if any(shares)
        },
        "rules_fired": {
            r: dict(zip(run.option_names, (round(s, 4) for s in shares), strict=True))
            for r, shares in run.rule_hits.items()
            if any(shares)
        },
        "notes": run.notes,
    }


def _research_summary(research_id: str, bundle: ResearchBundle) -> dict[str, Any]:
    return {
        "research": research_id,
        "question": bundle.question,
        "queries": len(bundle.queries),
        "sources": len(bundle.results),
        "facts": len(bundle.cited_facts),
        "dropped": len(bundle.dropped),
        "warnings": bundle.warnings,
        "tavily_credits": bundle.tavily_credits,
        "tavily_usd_estimate": round(bundle.tavily_credits * USD_PER_CREDIT, 6),
        "cache_hits": bundle.tavily_cache_hits,
    }


def build_server(
    plugins: dict[str, Plugin] | None = None,
    *,
    remotes: dict[str, str] | None = None,
    services: Services | None = None,
    all_tools: bool | None = None,
) -> FastMCP:
    """``all_tools=False`` (default, env ``VARIATOR_TOOLS``) shows only the "core" tools;
    a session enables more with ``tools_enable``."""
    plugins = discover() if plugins is None else plugins
    svc = services or Services(plugins)
    svc.plugins = plugins
    store = svc.store
    mcp = FastMCP("variator-core", instructions=INSTRUCTIONS)
    remotes = remote_servers() if remotes is None else remotes
    if svc.isolate:
        mcp.add_middleware(OwnerMiddleware())
    if svc.stats is not None:
        mcp.add_middleware(StatsMiddleware(svc.stats, [*plugins, *remotes]))
    mount_notes = mount_all(mcp, plugins, remotes)
    for note in mount_notes:
        log.warning(note)
    if not (all_tools if all_tools is not None else os.environ.get(ENV_TOOLS, "").lower() == "all"):
        mcp.disable(components={"tool"})
        mcp.enable(tags={"core"}, components={"tool"})

    @mcp.resource("variator://guide/{topic}")
    def guide(topic: str) -> str:
        """Short reference: rules, patch, flow."""
        return GUIDES.get(topic, f"unknown topic; use one of: {', '.join(GUIDES)}")

    @mcp.tool(tags=BUILD)
    def plugins_list() -> dict[str, Any]:
        """Domain plugins: criteria, templates, rule packs, demo data."""
        return {"plugins": [p.card() for p in plugins.values()], "mount_notes": mount_notes}

    @mcp.tool(tags={"core"})
    async def tools_enable(
        groups: Annotated[list[str], Field(description="'code', 'ops', a plugin name, or 'all'")],
        ctx: Context,
    ) -> dict[str, Any]:
        """Show more tools in this session only (the default set is the core flow)."""
        known = {"code", "ops", "all", *plugins, *remotes}
        unknown = set(groups) - known
        if unknown:
            raise ValueError(f"unknown groups {sorted(unknown)}; known: {sorted(known)}")
        if "all" in groups:
            await ctx.enable_components(match_all=True)
            return {"enabled": ["all"]}
        names: set[str] = set()
        for group in set(groups) & set(plugins):
            server = plugins[group].server
            if server is not None:
                names |= {f"{group}_{t.name}" for t in await server.list_tools()}
        tags = set(groups) & {"code", "ops"}
        if tags:
            await ctx.enable_components(tags=tags, components={"tool"})
        if names:
            await ctx.enable_components(names=names, components={"tool"})
        if set(groups) & set(remotes):
            await ctx.enable_components(match_all=True)  # proxied names are known only to the remote
        return {"enabled": sorted(groups), "plugin_tools": sorted(names)}

    @mcp.tool(tags=BUILD)
    def ingest(
        source: Annotated[str, Field(description="File path or URL (Google Sheets/Docs/Slides, http)")],
        name: str | None = None,
        origin: Annotated[str, Field(description="data | web | assumption")] = "data",
        plugin: Annotated[
            str | None, Field(description="Resolve `source` inside this plugin's data dir")
        ] = None,
    ) -> dict[str, Any]:
        """Read CSV, JSON, XLSX, PDF or a URL. Local code, 0 tokens."""
        if source.startswith(("http://", "https://")):
            dataset = read_url(source, name=name, origin=origin)
        else:
            path = Path(source)
            if plugin is not None and svc.plugin(plugin).data_dir is not None:
                path = svc.plugin(plugin).data_dir / source
            dataset = read_source(path, name=name, origin=origin)
        key = store.add_dataset(dataset)
        return {"dataset": key, **dataset.summary()}

    @mcp.tool(tags=BUILD)
    def ingest_inline(
        data: Annotated[Any, Field(description="JSON records, JSON columns, constants mapping or CSV text")],
        name: str = "inline",
        origin: str = "data",
    ) -> dict[str, Any]:
        """Data from the client or another MCP tool's output."""
        if isinstance(data, str) and data.lstrip()[:1] not in "[{":
            dataset: Dataset = read_csv(data, name=name, origin=origin)
        else:
            dataset = read_json(data, name=name, origin=origin)
        key = store.add_dataset(dataset)
        return {"dataset": key, **dataset.summary()}

    @mcp.tool(tags=BUILD)
    def quality(dataset: str) -> dict[str, Any]:
        """Q score per numeric column and the simplest trend model (AIC)."""
        ds = store.dataset(dataset)
        out = {}
        for column, values in ds.numeric().items():
            score = score_series(values, origin=ds.origin)
            trend = select_trend(values) if ds.kind == "table" and values.size >= 3 else None
            out[column] = {
                "q": score.q,
                "issues": score.issues,
                **({"trend": trend.model, "trend_params": trend.params} if trend else {}),
            }
        return {"dataset": dataset, "columns": out}

    @mcp.tool(tags=BUILD)
    def research_business(
        business: str,
        geography: str = "",
        max_queries: Annotated[int, Field(description="1..1000 Tavily searches")]=24,
        results_per_query: Annotated[int, Field(description="1..20 results per search")]=5,
        search_depth: SearchDepth = "basic",
        extract_top: Annotated[int, Field(description="0..20 sources to deepen with Tavily Extract")]=12,
        max_facts: Annotated[int, Field(description="Maximum validated facts to keep")]=50,
    ) -> dict[str, Any]:
        """Deep-research a business before building a Case.

        Searches cover the business model, customers, competitors, regulation,
        operations, labour, risks, benchmarks, market, technology and costs. Tavily
        supplies sources; Nemotron Lightning extracts only facts with checked citations.
        The returned handle can be passed to case_from_question(research=...).
        """
        if not 1 <= results_per_query <= 20:
            raise ValueError("results_per_query must be 1..20")
        if not 0 <= extract_top <= 20:
            raise ValueError("extract_top must be 0..20")
        if not 1 <= max_facts <= 1000:
            raise ValueError("max_facts must be 1..1000")
        queries = business_profile_queries(business, geography=geography, max_queries=max_queries)
        location = f" in {geography.strip()}" if geography.strip() else ""
        question = f"What external facts define the business of {business.strip()}{location}?"
        bundle = gather_external_facts(
            question,
            tf_client=svc.llm,
            tavily=svc.tavily,
            queries=queries,
            results_per_query=results_per_query,
            search_depth=search_depth,
            extract_top=extract_top,
            model="lightning",
            max_facts=max_facts,
            max_workers=4,
        )
        research_id = store.new_id("research")
        store.set_research(research_id, bundle)
        return _research_summary(research_id, bundle)

    @mcp.tool(tags=BUILD)
    def research_get(research: str, full: bool = False) -> dict[str, Any]:
        """Read a stored research summary; full=true includes checked facts and sources."""
        bundle = store.research(research)
        out = _research_summary(research, bundle)
        if full:
            out["cited_facts"] = [fact.model_dump(mode="json") for fact in bundle.cited_facts]
            out["facts"] = [fact.model_dump(mode="json") for fact in bundle.cited_facts]
            out["sources"] = [source.model_dump(mode="json") for source in bundle.results]
            out["dropped_facts"] = [fact.model_dump(mode="json") for fact in bundle.dropped]
        return out

    @mcp.tool(tags=BUILD)
    def case_from_template(
        plugin: str,
        template: str,
        extend: Annotated[list[str], Field(description="More plugins: each one adds its axes")] = [],  # noqa: B006
        datasets: list[str] = [],  # noqa: B006
        scenarios: Annotated[
            list[str], Field(description="Presets of the template (plugins_list); they combine")
        ] = [],  # noqa: B006
    ) -> dict[str, Any]:
        """New case from a plugin template, optionally with scenario presets. 0 tokens."""
        source = svc.plugin(plugin)
        if template not in source.templates:
            raise KeyError(
                f"plugin {plugin!r} has no template {template!r} (known: {sorted(source.templates)})"
            )
        model = source.templates[template]()
        notes = []
        if scenarios:
            model = apply_scenarios(model, source.scenarios.get(template, []), scenarios)
            notes.append(f"scenarios: {', '.join(scenarios)}")
        case = svc.new_case(
            model, plugins=[plugin, *extend], datasets=list(datasets), notes=notes, template=template
        )
        return case.summary()

    @mcp.tool(tags=BUILD)
    def case_from_question(
        question: str,
        datasets: list[str] = [],  # noqa: B006
        plugins: list[str] = [],  # noqa: B006
        options_hint: list[str] = [],  # noqa: B006
        research: str | None = None,
    ) -> dict[str, Any]:
        """New case from the question, data and web research: Nemotron Ultra designs the case (the forks, the
        metric, the horizon, the hard constraints), then Nemotron Super builds its decision model."""
        from variator.ingestion import question as framing
        from variator.reasoning import build_decision_model

        facts = dataset_facts([(d, store.dataset(d)) for d in datasets])
        notes = [f"{len(facts)} facts from datasets"]
        if research is not None:
            bundle = store.research(research)
            facts.extend(bundle.facts)
            notes.append(f"{len(bundle.facts)} checked facts from research {research}")
        brief = framing.parse_question(svc.llm, question, context_facts=facts)
        forks, months = len(brief.options), brief.horizon_months
        notes.append(f"case design: {forks} forks, metric {brief.metric}, horizon {months} months")
        model = build_decision_model(
            svc.llm,
            question,
            facts,
            options_hint=list(options_hint) or [o.name for o in brief.options],
            constraints=[c.description for c in brief.constraints],
        )
        case = svc.new_case(
            model, plugins=list(plugins), datasets=list(datasets), notes=notes
        )
        return case.summary()

    @mcp.tool(tags=BUILD)
    def case_get(case: str, full: bool = False) -> dict[str, Any]:
        """Case summary; full=true gives the complete model JSON."""
        c = store.case(case)
        return {**c.summary(), "model": c.model.model_dump(mode="json")} if full else c.summary()

    @mcp.tool(tags=BOTH)
    def case_patch(
        case: str,
        ops: Annotated[list[dict[str, Any]], Field(description="Patch ops; see variator://guide/patch")],
        version: Annotated[
            int | None, Field(description="Expected case version; another version is a conflict")
        ] = None,
    ) -> dict[str, Any]:
        """Change a case with small ops (a delta, not the full model)."""
        with store.case_lock(case):
            c = store.case(case)
            if version is not None and version != c.version:
                raise ValueError(f"conflict: {case} is at version {c.version}, not {version}; read it again")
            model = apply_patch(c.model, ops)
            Case(c.id, model, packs=c.packs).rules()  # rules must still compile
            c.model, c.version = model, c.version + 1
            c.notes.append(f"v{c.version}: {len(ops)} patch op(s)")
            store.save_case(c)
            return c.summary()

    @mcp.tool(tags=BUILD)
    def rules_add(
        case: str,
        yaml: Annotated[str | None, Field(description="Rule pack YAML; see variator://guide/rules")] = None,
        plugin: str | None = None,
        pack: str | None = None,
    ) -> dict[str, Any]:
        """Add a rule pack (YAML text or a plugin pack). Validated before use."""
        if yaml is not None:
            new = load_pack(yaml)
        elif plugin and pack:
            new = svc.plugin(plugin).pack(pack)
        else:
            raise ValueError("give `yaml`, or `plugin` and `pack`")
        with store.case_lock(case):
            c = store.case(case)
            packs = [p for p in c.packs if p.pack != new.pack] + [new]
            compiled = Case(c.id, c.model, packs=packs).rules()
            c.packs = packs
            store.save_case(c)
        return {"case": case, "rules": [r.id for r in compiled.rules], "packs": [p.pack for p in packs]}

    @mcp.tool(tags=BOTH)
    def simulate(
        case: str,
        n: Annotated[int, Field(description="Scenarios; a power of 2 for sobol")] = 4_096,
        seed: int = 42,
        sampling: Annotated[
            Literal["sobol", "random"], Field(description="sobol: same accuracy with 4-8x fewer scenarios")
        ] = "sobol",
    ) -> dict[str, Any]:
        """Monte Carlo over all options and criteria, with the case rules."""
        c = store.case(case)
        run = simulate_tensor(
            c.model,
            rules=c.rules(),
            n_scenarios=min(max(n, 100), MAX_SCENARIOS),
            seed=seed,
            sampling=sampling,
        )
        run.notes.extend(c.notes[-3:])
        return _run_summary(store.add_run(case, run), run)

    def ranked(run: str, weights: dict[str, float] | None, risk_aversion: float) -> dict[str, Any]:
        _, r = store.run(run)
        out = rank_options(r, weights, risk_aversion=risk_aversion).model_dump(mode="json")
        for option in out["options"]:
            option["stats"] = {k: {s: _sig(v) for s, v in st.items()} for k, st in option["stats"].items()}
            for key in ("score", "topsis", "prob_best", "robustness", "p_feasible"):
                option[key] = round(option[key], 4)
        out["weights"] = {k: round(v, 4) for k, v in out["weights"].items()}
        return out

    @mcp.tool(tags=ANALYZE)
    def rank(
        run: str,
        weights: Annotated[
            dict[str, float] | None, Field(description="criterion -> weight (any scale)")
        ] = None,
        risk_aversion: Annotated[float, Field(ge=0, le=1, description="0 = mean, 1 = worst 5%")] = 0.25,
    ) -> dict[str, Any]:
        """Weighted ranking, TOPSIS, P(best), Pareto, weight robustness. 0 tokens."""
        return ranked(run, weights, risk_aversion)

    @mcp.tool(tags=ANALYZE)
    def best_path(
        run: str,
        weights: Annotated[
            dict[str, float] | None, Field(description="criterion -> weight (any scale)")
        ] = None,
        risk_aversion: Annotated[float, Field(ge=0, le=1)] = 0.25,
    ) -> dict[str, Any]:
        """Summary: situation -> decision -> where it stays best -> expected result -> risks. 0 tokens."""
        c, r = store.run(run)
        situation = [
            name.strip()
            for note in c.notes
            if note.startswith("scenarios: ")
            for name in note.removeprefix("scenarios: ").split(",")
        ]
        stored = store.rag(run) or rag_report(r, resolve_thresholds(r), weights)
        red = {o.option: o.overall.red for o in stored.options}
        path = summarize_path(r, weights, risk_aversion=risk_aversion, situation=situation, red_share=red)
        return {"run": run, **path.model_dump(mode="json")}

    @mcp.tool(tags=ANALYZE)
    def rag(
        run: str,
        limits: Annotated[
            dict[str, dict[str, float]] | None,
            Field(description="criterion -> {green, red} in criterion units"),
        ] = None,
        baseline: Annotated[
            str | None, Field(description="Option for default limits (default: first)")
        ] = None,
        weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Share of green/amber/red scenarios per option and criterion."""
        _, r = store.run(run)
        resolved = resolve_thresholds(r, {k: RagThresholds(**v) for k, v in (limits or {}).items()}, baseline)
        report = rag_report(r, resolved, weights)
        store.set_rag(run, report)
        out = report.model_dump(mode="json")
        out["limits"] = {
            k: {"green": _sig(v["green"]), "red": _sig(v["red"])} for k, v in out["limits"].items()
        }
        for o in out["options"]:
            o["overall"] = {k: round(v, 3) for k, v in o["overall"].items()}
            o["criteria"] = {c: {k: round(v, 3) for k, v in s.items()} for c, s in o["criteria"].items()}
        return out

    @mcp.tool(tags=ANALYZE)
    def tunnel(
        run: str,
        x: str,
        y: str,
        criterion: Annotated[str | None, Field(description="null = overall color")] = None,
        grid: Annotated[int, Field(ge=5, le=41)] = 15,
        weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """RAG map over two drivers; the full grid goes to the report."""
        c, r = store.run(run)
        stored = store.rag(run)
        limits = stored.limits if stored else resolve_thresholds(r)
        tm = tunnel_map(
            c.model,
            x=x,
            y=y,
            limits=limits,
            rules=c.rules(),
            criterion=criterion,
            weights=weights,
            grid=grid,
            samples_per_cell=256,  # sobol 256 beats random 300 (measured 2026-10-26)
            sampling="sobol",
        )
        store.add_tunnel(run, tm)
        cells = grid * grid
        best = [b for row in tm.best for b in row]
        return {
            "run": run,
            "x": [_sig(tm.x_values[0]), _sig(tm.x_values[-1])],
            "y": [_sig(tm.y_values[0]), _sig(tm.y_values[-1])],
            "green_area": {o: round(float(np.mean(tm.green[o])), 3) for o in tm.options},
            "red_area": {o: round(float(np.mean(tm.red[o])), 3) for o in tm.options},
            "best_area": {o: round(best.count(o) / cells, 3) for o in tm.options},
            "note": "areas are shares of the grid; call report for the maps",
        }

    @mcp.tool(tags=BOTH)
    def simplify_case(case: str, threshold: float = DEFAULT_THRESHOLD) -> dict[str, Any]:
        """Occam's razor: simpler shapes for low-Q inputs, freeze tiny drivers, drop weak correlations."""
        with store.case_lock(case):
            c = store.case(case)
            shares = swing_shares(c.model)
            result = simplify(
                c.model, c.quality, threshold=threshold, swing_shares=shares, keep=sorted(c.rules().names)
            )
            if result.notes:
                c.model, c.version = result.model, c.version + 1
                c.notes.extend(result.notes)
                store.save_case(c)
        return {
            "case": case,
            "version": c.version,
            "changes": result.notes or ["no change: the model is already simple"],
        }

    @mcp.tool(tags=ANALYZE)
    def report(run: str, title: str | None = None) -> dict[str, Any]:
        """Write the HTML report (sliders, RAG, tunnels). Returns the file path."""
        c, r = store.run(run)
        payload = report_payload(
            r,
            title=title or c.model.question,
            rag=store.rag(run),
            tunnels=store.tunnels(run),
            notes=c.notes,
        )
        path = write_report(svc.report_dir / f"{c.id}_{run}.html", payload)
        return {"path": str(path.resolve())}

    @mcp.tool(tags=ANALYZE)
    def explain(run: str, weights: dict[str, float] | None = None) -> dict[str, Any]:
        """Short plain-language explanation by Nemotron Ultra, from the computed numbers only (LLM call)."""
        c, r = store.run(run)
        ranking = ranked(run, weights, 0.25)
        stored = store.rag(run)
        brief = {
            "question": c.model.question,
            "ranking": [
                {
                    k: o[k]
                    for k in ("option", "rank", "score", "p_feasible", "prob_best", "robustness", "pareto")
                }
                for o in ranking["options"]
            ],
            "tradeoffs": ranking["tradeoffs"],
            "order_changes_without": ranking["reversals"],
            "warnings": ranking["warnings"],
            "red_share": {o.option: round(o.overall.red, 3) for o in stored.options} if stored else None,
        }
        result = svc.llm.chat(
            "ultra",
            [
                {
                    "role": "system",
                    "content": "Explain a decision analysis in at most 8 short sentences. "
                    "Use only the given numbers. "
                    "Write in ASD-STE100 Simplified Technical English.",
                },
                {"role": "user", "content": json.dumps(brief, ensure_ascii=False)},
            ],
            reasoning=False,
            temperature=0.0,
            max_tokens=600,
        )
        return {
            "run": run,
            "text": result.content,
            "tokens": result.usage.prompt_tokens + result.usage.completion_tokens,
        }

    @mcp.tool(tags={"ops"})
    def stats_summary(
        group_by: Annotated[
            str, Field(description="tool | plugin | kind | session | case_id | entry | day | model")
        ] = "tool",
        since: Annotated[str | None, Field(description="ISO date or time, for example 2026-10-25")] = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Spending per stage group: calls, errors, seconds, reply size, tokens, cost (from the stats DB)."""
        if svc.stats is None:
            raise ValueError("run statistics are off (set VARIATOR_STATS to a path)")
        rows = (
            svc.stats.by_model(since=since)
            if group_by == "model"
            else svc.stats.summary(group_by, since=since, limit=limit)
        )
        return {"group_by": group_by, "rows": rows}

    @mcp.tool(tags={"code"})
    def container_status() -> dict[str, Any]:
        """Is a container runtime available; which scan recipes and groups exist."""
        available = svc.runner.available() if hasattr(svc.runner, "available") else True
        return {
            "available": available,
            "recipes": {n: r.description for n, r in RECIPES.items()},
            "groups": {g: list(names) for g, names in RECIPE_GROUPS.items()},
        }

    @mcp.tool(tags={"code"})
    def code_scan(
        source: Annotated[str, Field(description="Path of the code folder (mounted read-only)")],
        recipes: Annotated[
            list[str],
            Field(description="Recipes or groups: default, sca, secrets, duplication, architecture"),
        ] = ["scc"],  # noqa: B006
    ) -> dict[str, Any]:
        """Run scan recipes in containers: metrics + finding counts (0 tokens). Raw reports stay on disk."""
        names = expand_recipes(recipes)
        if not Path(source).is_dir():
            raise ValueError(f"{source!r} is not a folder")
        scan = store.new_id("scan")
        folder = svc.report_dir / "scans" / scan
        outcomes, out = [], {}
        for name in names:
            outcome = run_recipe(svc.runner, RECIPES[name], source, out_dir=folder / name)
            outcomes.append(outcome)
            out[name] = {"ok": outcome.ok, "elapsed_s": round(outcome.elapsed_s, 2), **outcome.metrics}
            if outcome.error:
                out[name]["error"] = outcome.error
        write_findings(folder, outcomes)
        return {"scan": scan, "source": source, "scans": out, "findings": findings_summary(folder)}

    @mcp.tool(tags={"code"})
    def findings_export(
        scan: str,
        product: Annotated[str, Field(description="DefectDojo product (created if new)")],
        engagement: str = "variator",
    ) -> dict[str, Any]:
        """Send the raw reports of a scan to DefectDojo (env DEFECTDOJO_URL, DEFECTDOJO_TOKEN)."""
        folder = svc.report_dir / "scans" / scan
        if not store.visible(scan) or not folder.is_dir():
            raise KeyError(f"unknown scan {scan!r}")
        url, token = os.environ.get("DEFECTDOJO_URL"), os.environ.get("DEFECTDOJO_TOKEN")
        if not url or not token:
            raise ValueError("set DEFECTDOJO_URL and DEFECTDOJO_TOKEN (API v2 key)")
        dojo = svc.defectdojo or DefectDojo(url, token)
        return {"scan": scan, "imports": dojo.push_scan(folder, product=product, engagement=engagement)}

    mcp.services = svc  # type: ignore[attr-defined]  # tests and the agent use it
    return mcp
