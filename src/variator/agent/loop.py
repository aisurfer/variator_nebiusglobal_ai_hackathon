"""Nemotron agent over the variator-core MCP tools (Token Factory tool calling).

The agent talks to the FastMCP server through the in-memory client. Token budget rules:

* only ``tools`` are exposed (default ``CORE_TOOLS``; each schema costs tokens on every step);
* stages (tool tags "build", "analyze"): the model sees only the tools of the current
  stage; a successful ``simulate`` moves the run to "analyze" (forward only);
* short history: tool replies older than ``keep_replies`` steps become one line with the handles;
* prompt cache (implicit prefix cache on Token Factory): the system prompt and the tool
  order never change, a stage changes once, and a compacted reply never changes again;
* a tool reply longer than ``max_tool_chars`` is cut before it goes back to the model.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client, FastMCP

from variator.llm import TokenFactoryClient, recommended_sampling
from variator.llm.models import ROLE_MODEL
from variator.mcp.guides import GUIDES
from variator.telemetry import CURRENT, StageRef, StatsStore

CORE_TOOLS = (
    "plugins_list",
    "ingest",
    "quality",
    "research_business",
    "research_get",
    "case_from_template",
    "case_get",
    "case_patch",
    "rules_add",
    "simulate",
    "rank",
    "best_path",
    "rag",
    "tunnel",
    "simplify_case",
    "report",
)
MAX_TOOL_CHARS = 4_000
KEEP_REPLIES = 2
STAGES = ("build", "analyze")
HANDLE_KEYS = ("dataset", "research", "case", "version", "run", "path", "error")
SYSTEM = (
    "You are a decision analyst. Use the tools; never compute numbers yourself. "
    "Answer in ASD-STE100 Simplified Technical English: short sentences, active voice. "
    "Quote only numbers that the tools returned. Do not give a recommendation until the same decision "
    "has a successful simulate and rank. Flow:\n" + GUIDES["flow"]
)
DECISION_GATE_PROMPT = (
    "Do not give a final recommendation yet. The decision is incomplete. "
    "You must successfully call simulate and then rank for the same decision before answering. "
    "Use only numbers returned by the tools."
)


@dataclass
class AgentStep:
    tool: str
    arguments: dict[str, Any]
    ok: bool
    chars: int


@dataclass(frozen=True)
class DecisionContract:
    """Fixed inputs for a comparable decision run."""

    plugin: str
    template: str
    weights: dict[str, float]
    datasets: tuple[str, ...] = ()
    scenarios: tuple[str, ...] = ()
    n: int = 4096
    seed: int = 42
    sampling: str = "sobol"

    @classmethod
    def from_json(cls, raw: str) -> DecisionContract:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("decision contract must be a JSON object")
        return cls(
            plugin=str(payload["plugin"]),
            template=str(payload["template"]),
            weights={str(k): float(v) for k, v in dict(payload["weights"]).items()},
            datasets=tuple(str(v) for v in payload.get("datasets", ())),
            scenarios=tuple(str(v) for v in payload.get("scenarios", ())),
            n=int(payload.get("n", 4096)),
            seed=int(payload.get("seed", 42)),
            sampling=str(payload.get("sampling", "sobol")),
        )


@dataclass
class AgentResult:
    answer: str
    decision_ready: bool = False
    simulation_run: str | None = None
    ranking: dict[str, Any] | None = None
    steps: list[AgentStep] = field(default_factory=list)
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    stages: list[str] = field(default_factory=list)  # stage of each LLM request


def tool_specs(tools: Sequence[Any]) -> list[dict[str, Any]]:
    """MCP tool descriptions -> chat-completions tool specs."""
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description or "", "parameters": t.inputSchema},
        }
        for t in tools
    ]


def tool_tags(tool: Any) -> set[str]:
    return set(((tool.meta or {}).get("fastmcp") or {}).get("tags") or ())


def summarize_reply(name: str, text: str) -> str:
    """One line for an old tool reply: the tool, the handles, the original size."""
    handles = ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            handles = ", ".join(f"{k}={data[k]}" for k in HANDLE_KEYS if isinstance(data.get(k), str | int))
    elif text.startswith("error:"):
        handles = text[:160]
    return f"[old {name} reply, {len(text)} chars{': ' + handles if handles else ''}]"


async def run_agent(
    question: str,
    *,
    server: FastMCP,
    llm: TokenFactoryClient,
    stats: StatsStore | None = None,
    contract: DecisionContract | None = None,
    **kwargs: Any,
) -> AgentResult:
    """Let the model call the MCP tools until it answers; ``tools=None`` exposes all tools.

    With ``stats`` (default: the server's stats store), the run is one ``agent_run`` stage;
    its LLM calls and its tool calls (child stages) share one session id.
    """
    stats = stats if stats is not None else getattr(getattr(server, "services", None), "stats", None)
    if stats is None:
        return await _run(question, server=server, llm=llm, contract=contract, **kwargs)
    stats.attach(llm.ledger)
    session = "agent-" + uuid.uuid4().hex[:10]
    stage = stats.begin_stage(
        session=session, entry="agent", tool="agent_run", plugin="agent", args_chars=len(question)
    )
    token = CURRENT.set(StageRef(stage, session))
    started, ok, answer = time.perf_counter(), False, ""
    try:
        result = await _run(question, server=server, llm=llm, contract=contract, **kwargs)
        ok, answer = result.decision_ready, result.answer
        return result
    finally:
        stats.end_stage(stage, ok=ok, duration_s=time.perf_counter() - started, reply_chars=len(answer))
        CURRENT.reset(token)


async def _run(
    question: str,
    *,
    server: FastMCP,
    llm: TokenFactoryClient,
    model: str = ROLE_MODEL["agent_loop"],
    tools: Sequence[str] | None = CORE_TOOLS,
    extra_tools: Sequence[str] = (),
    max_steps: int = 12,
    max_tool_chars: int = MAX_TOOL_CHARS,
    reasoning: bool | None = False,
    stages: bool = True,
    keep_replies: int | None = KEEP_REPLIES,
    contract: DecisionContract | None = None,
) -> AgentResult:
    result = AgentResult(answer="")
    async with Client(server) as client:
        wanted = None if tools is None else set(tools) | set(extra_tools)
        available = await client.list_tools()
        visible = {t.name for t in available}
        if "tools_enable" in visible and (wanted is None or wanted - visible):
            await client.call_tool("tools_enable", {"groups": ["all"]})  # this session; the agent filters
            available = await client.list_tools()
        chosen = [t for t in available if wanted is None or t.name in wanted]
        names = {t.name for t in chosen}  # any chosen tool may run, also outside the stage
        by_stage = {
            s: tool_specs(
                [t for t in chosen if not stages or s in tool_tags(t) or not tool_tags(t) & set(STAGES)]
            )
            for s in STAGES
        }
        stage = STAGES[0]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_prompt(contract)},
            {"role": "user", "content": question},
        ]
        replies: list[tuple[int, str, str]] = []  # (message index, tool, full text) per step, in order
        step_ends: list[int] = []  # len(replies) after each step
        case_id: str | None = None
        simulated_run: str | None = None
        ranked_run: str | None = None
        sampling = recommended_sampling(reasoning=bool(reasoning), tools=True)
        for step_no in range(max_steps):
            if keep_replies is not None and len(step_ends) > keep_replies:
                for index, name, text in replies[: step_ends[-keep_replies - 1]]:
                    messages[index]["content"] = summarize_reply(name, text)  # same text each time
            result.stages.append(stage)
            reply = await asyncio.to_thread(
                llm.chat,
                model,
                messages,
                tools=by_stage[stage],
                reasoning=reasoning,
                max_tokens=2_000,
                **sampling,
            )
            result.prompt_tokens += reply.usage.prompt_tokens
            result.cached_tokens += reply.usage.cached_tokens
            result.completion_tokens += reply.usage.completion_tokens
            if not reply.tool_calls:
                if simulated_run is not None and ranked_run == simulated_run and result.ranking is not None:
                    result.answer = reply.content
                    result.decision_ready = True
                    break
                if step_no + 1 >= max_steps:
                    result.answer = (
                        "Decision incomplete: successful simulate and rank are required "
                        "before a recommendation."
                    )
                    break
                messages.append({"role": "assistant", "content": reply.content or None})
                messages.append({"role": "user", "content": DECISION_GATE_PROMPT})
                continue
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.arguments},
                        }
                        for c in reply.tool_calls
                    ],
                }
            )
            for call in reply.tool_calls:
                raw_args = _json_args(call.arguments)
                forced_args, contract_error = _contract_args(
                    call.name, raw_args, contract, case_id=case_id, simulated_run=simulated_run
                )
                if contract_error:
                    full, ok, args = f"error: {contract_error}", False, raw_args
                else:
                    full, ok, args = await _invoke(
                        client, names, call.name, call.arguments, args_override=forced_args
                    )
                text = full
                if len(text) > max_tool_chars:
                    text = text[:max_tool_chars] + f"... [cut: {len(text)} chars]"
                result.steps.append(AgentStep(call.name, args, ok, len(text)))
                replies.append((len(messages), call.name, full))
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
                if ok:
                    if call.name in {"case_from_template", "case_from_question"}:
                        case_id = _case_handle(full)
                    elif call.name == "simulate":
                        simulated_run = _run_handle(full)
                        ranked_run = None
                        result.ranking = None
                        result.simulation_run = simulated_run
                        stage = "analyze"
                    elif call.name == "rank":
                        ranked_run = args.get("run") if isinstance(args.get("run"), str) else None
                        result.ranking = _json_object(full) if ok else None
            step_ends.append(len(replies))
        else:
            result.answer = f"No final answer after {max_steps} steps."
    result.cost_usd = round(llm.ledger.total_cost(), 6)
    return result


def _run_handle(text: str) -> str | None:
    """Return the simulation or ranking run handle from a successful tool reply."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload.get("run") if isinstance(payload, dict) and isinstance(payload.get("run"), str) else None


def _case_handle(text: str) -> str | None:
    payload = _json_object(text)
    return payload.get("case") if payload and isinstance(payload.get("case"), str) else None


def _json_args(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _system_prompt(contract: DecisionContract | None) -> str:
    if contract is None:
        return SYSTEM
    return (
        f"{SYSTEM}\n\nFixed decision contract. Use plugin={contract.plugin!r}, "
        f"template={contract.template!r}, datasets={list(contract.datasets)!r}, "
        f"scenarios={list(contract.scenarios)!r}, simulation n={contract.n}, seed={contract.seed}, "
        f"sampling={contract.sampling!r}, and rank weights={contract.weights!r}. "
        "Do not change any contract value."
    )


def _contract_args(
    name: str,
    args: dict[str, Any],
    contract: DecisionContract | None,
    *,
    case_id: str | None,
    simulated_run: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if contract is None:
        return None, None
    if name in {"research_business", "research_get", "case_from_question", "case_patch", "rules_add"}:
        return None, f"{name} is disabled for a fixed decision contract"
    if name == "case_from_template":
        expected = {
            "plugin": contract.plugin,
            "template": contract.template,
            "datasets": list(contract.datasets),
            "scenarios": list(contract.scenarios),
        }
        mismatch = any(
            args.get(key, [] if key in {"datasets", "scenarios"} else None) != value
            for key, value in expected.items()
        )
        if mismatch:
            return None, "case_from_template arguments do not match the fixed decision contract"
        return expected, None
    if name == "simulate":
        if case_id is None or args.get("case") != case_id:
            return None, "simulate must use the fixed case created by the contract"
        return {
            "case": case_id,
            "n": contract.n,
            "seed": contract.seed,
            "sampling": contract.sampling,
        }, None
    if name in {"rank", "best_path", "rag", "tunnel", "report"}:
        if simulated_run is None or args.get("run") != simulated_run:
            return None, f"{name} must use the latest simulation run from the contract"
        forced = {"run": simulated_run}
        if name in {"rank", "best_path"}:
            forced["weights"] = dict(contract.weights)
        return forced, None
    return None, None


async def _invoke(
    client: Client,
    names: set[str],
    name: str,
    raw: str,
    *,
    args_override: dict[str, Any] | None = None,
) -> tuple[str, bool, dict[str, Any]]:
    if name not in names:
        return f"error: tool {name!r} is not available", False, {}
    if args_override is not None:
        args = args_override
    else:
        try:
            args = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            return f"error: arguments are not valid JSON: {exc}", False, {}
        if not isinstance(args, dict):
            return "error: tool arguments must be a JSON object", False, {}
    response = await client.call_tool(name, args, raise_on_error=False)
    text = response.content[0].text if response.content else ""
    return (f"error: {text}" if response.is_error else text), not response.is_error, args
