"""Platform quality run: measure the tests and write one row per value to ``quality_runs`` (variator.quality.runs).

Tests (``--tests``, default all):
  stability    PRIMARY: the same case input with K seeds -> does the same best option win? (winner and top-2
               agreement, Kendall tau of the order, the winner's pBest)
  determinism  the same case input and seed twice -> the same ranking, to the last digit
  convergence  winner agreement per number of scenarios n (250, 500, 1000, 2000)
  recovery     hidden behaviour parameters recovered from the records (the actors' recovery check)
  causal       chain defects from every meeting page and decision: a meeting's bases after a decision made at it,
               cycles, a cause dated after its effect, steps that only refer
  causal_gold  direct «why» links against the generator's gold links (precision, recall per kind) and the roots
               of the «why» evaluation cases; --history names the corpus (default: the deployed one, seed 7)
  answers      Ask answers against the known facts of the corpus evaluation cases (per category)
  grounded     numbers of the Ask answers found in the sources they cite
  iia          remove an option that does not win -> the winner and the order of the others stay
  weights      raise one criterion's weight (x1, 2, 4, 8) -> its favourite never falls in rank
  inputs       move each uncertain input by up to ±5% (--variants K) -> the same best option wins
  sanity       directional laws of the company model (pytest)
  regression   pass share of the automated tests of the engine, actors, quality store and API (pytest)
  calibration  rolling back-test: held-out month ends inside P10-P90 (ideal 80%); needs --history DIR;
               also writes sharpness (width and interval score of P10-P90 per unit of the actual value)

The case futures run on the deployed engine through the workspace API (``--base``, default the local dev server,
which proxies the deployed API). Usage: ``uv run python scripts/quality_run.py [--tests stability,recovery] [--seeds 10]``.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from itertools import combinations
from typing import Any

from variator.quality.runs import QualityStore

CASES = ("company_best_year", "delivery_best_way")
QUESTIONS = ("Why did we hire a DevOps engineer?", "Why did incidents rise?", "Why did cash fall?")
CAUSAL_START = ("jira.BIZ-197", "jira.BIZ-110", "confluence.page-2000016.dec-1")


def http(base: str, path: str, body: Any = None, timeout: float = 120) -> Any:
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    for attempt in range(3):  # a 502-504 of the proxy is retried after a pause
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code not in (502, 503, 504) or attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def order_of(month: dict[str, Any]) -> list[str]:
    return [o["option"] for o in sorted(month["ranking"]["options"], key=lambda o: o["rank"])]


def kendall_tau(a: list[str], b: list[str]) -> float:
    pos = {x: i for i, x in enumerate(b)}
    pairs = [(x, y) for x, y in combinations([x for x in a if x in pos], 2)]
    if not pairs:
        return 1.0
    agree = sum(1 if pos[x] < pos[y] else -1 for x, y in pairs)
    return agree / len(pairs)


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ""


class Run:
    def __init__(self, store: QualityStore, base: str) -> None:
        self.store, self.base = store, base
        self.run_id, self.sha = time.strftime("q%Y%m%d-%H%M%S"), git_sha()

    def add(self, test: str, metric: str, value: float | None, **kw: Any) -> None:
        self.store.add({"run_id": self.run_id, "git_sha": self.sha, "test": test, "metric": metric, "value": value, **kw})
        print(f"  {test:12} {kw.get('case_id') or '':20} {metric:22} {value if value is None else round(value, 4)}"
              f"{'' if kw.get('passed') is None else ('  ok' if kw['passed'] else '  FAIL')}")

    def month(self, case: str, n: int, seed: int, **state: Any) -> tuple[dict[str, Any], float]:
        # the deployed engine has a 15 s limit per run and 2 parallel runs: a 502 is retried after a pause
        for attempt in range(4):
            t0 = time.time()
            try:
                m = http(self.base, f"/api/variator/cases/{case}/month", {"n": n, "seed": seed, **state})
                return m, (time.time() - t0) * 1000
            except urllib.error.HTTPError as e:
                if e.code not in (502, 503, 504) or attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))
        raise RuntimeError("unreachable")

    # ---------------------------------------------------------------- the primary measure
    def stability(self, seeds: int, n: int) -> None:
        for case in CASES:
            orders, pbest, ms = [], [], []
            for s in range(1, seeds + 1):
                m, t = self.month(case, n, 1000 + s)
                o = order_of(m)
                orders.append(o)
                pbest.append(next(x["pBest"] for x in m["ranking"]["options"] if x["option"] == o[0]))
                ms.append(t)
            winners = Counter(o[0] for o in orders)
            mode, count = winners.most_common(1)[0]
            top2 = Counter(frozenset(o[:2]) for o in orders).most_common(1)[0][1]
            ref = max(orders, key=lambda o: sum(o == p for p in orders))
            taus = [kendall_tau(o, ref) for o in orders]
            share = count / len(orders)
            self.add("stability", "winner_agreement", share, case_id=case, n=n, target=0.9, passed=share >= 0.9,
                     ms=statistics.mean(ms), note=json.dumps({"winner": mode, "winners": dict(winners), "seeds": seeds}))
            self.add("stability", "top2_agreement", top2 / len(orders), case_id=case, n=n, target=0.9, passed=top2 / len(orders) >= 0.9)
            self.add("stability", "kendall_tau", statistics.mean(taus), case_id=case, n=n, ci_low=min(taus), ci_high=max(taus))
            self.add("stability", "winner_pbest", statistics.mean(pbest), case_id=case, n=n,
                     ci_low=min(pbest), ci_high=max(pbest), note="share of futures in which the winner is the best option")

    def determinism(self, n: int, k: int = 5, flush: int = 34) -> None:
        """The same seed computed twice, with the server cache (32 states) flushed in between by other states, so
        the second answer is computed again and not read from the cache."""
        for case in CASES:
            first = {s: self.month(case, n, 4300 + s)[0]["ranking"] for s in range(k)}
            for s in range(flush):
                self.month(case, 125, 9000 + s)  # other states push the first ones out of the cache
            same = sum(json.dumps(first[s], sort_keys=True) == json.dumps(self.month(case, n, 4300 + s)[0]["ranking"], sort_keys=True)
                       for s in range(k))
            self.add("determinism", "same_seed_same_ranking", same / k, case_id=case, n=n, target=1, passed=same == k,
                     note=f"{k} seeds, recomputed after {flush} other states flushed the cache")

    def convergence(self, seeds: int) -> None:
        for case in CASES:
            for n in (125, 250, 500, 1000):
                winners = Counter(order_of(self.month(case, n, 2000 + s)[0])[0] for s in range(1, seeds + 1))
                self.share("convergence", "winner_agreement", winners.most_common(1)[0][1], seeds, None, case_id=case, n=n,
                           note=json.dumps(dict(winners)))

    # ---------------------------------------------------------------- the logic of the choice
    def iia(self, n: int) -> None:
        """Remove one option that does not win -> the winner and the order of the others stay (no rank reversal)."""
        for case in CASES:
            base = order_of(self.month(case, n, 4243)[0])
            kept = order_kept = 0
            for x in base[1:]:
                o = order_of(self.month(case, n, 4243, forks=[f for f in base if f != x])[0])
                kept += o[0] == base[0]
                order_kept += o == [f for f in base if f != x]
            k = len(base) - 1
            self.add("iia", "winner_kept", kept / k, case_id=case, n=n, target=1, passed=kept == k, note=f"{k} removals")
            self.add("iia", "order_kept", order_kept / k, case_id=case, n=n, target=1, passed=order_kept == k)

    def weights(self, n: int) -> None:
        """Raise the weight of one criterion step by step -> the option that is best on it never falls in rank."""
        for case in CASES:
            m, _ = self.month(case, n, 4244)
            w = m["ranking"]["weights"]
            ok = 0
            for c in w:
                ranks = []
                for k in (1, 2, 4, 8):
                    o = order_of(self.month(case, n, 4244, edits={f"w|{c}": w[c] * k})[0])
                    ranks.append(o)
                fav = ranks[-1][0]
                pos = [o.index(fav) for o in ranks]
                ok += all(a >= b for a, b in zip(pos, pos[1:]))
                print(f"    {case} {c}: rank of {fav} {[p + 1 for p in pos]}")
            self.add("weights", "monotone_criteria", ok / len(w), case_id=case, n=n, target=1, passed=ok == len(w),
                     note=f"{len(w)} criteria x weight 1, 2, 4, 8")

    def inputs(self, n: int, variants: int, rel: float = 0.05) -> None:
        """Move each uncertain input of the case by up to ±5% (K variants) -> does the same best option win?"""
        import random

        for case in CASES:
            m, _ = self.month(case, n, 4245)
            win, scores = order_of(m)[0], [o["score"] for o in m["ranking"]["options"]]
            # the uncertain inputs of the case config (the engine takes edits only for them)
            cfg = json.loads(open(f"workspace/config/cases/{case}.json", encoding="utf-8").read())
            ps = [{"name": v["name"], **v["dist"]} for v in cfg.get("variables", [])
                  if v["dist"].get("kind") == "pert" and v["dist"]["low"] < v["dist"]["high"]]
            rnd, kept, used, moved = random.Random(7), 0, 0, 0
            for _ in range(variants):
                ed = {f"v|{p['name']}|mode": min(p["high"], max(p["low"], p["mode"] * (1 + rnd.uniform(-rel, rel)))) for p in ps}
                try:
                    v, _ = self.month(case, n, 4245, edits=ed)
                except urllib.error.HTTPError as e:
                    if e.code != 400:
                        raise
                    break  # the case takes no input edits: not measurable
                used += 1
                if [o["score"] for o in v["ranking"]["options"]] != scores:
                    moved += 1
                    kept += order_of(v)[0] == win
            # a variant that changes no score proves nothing: such a case is "not measurable", never a pass
            share = kept / moved if moved else None
            self.add("inputs", "winner_kept_5pct", share, case_id=case, n=n, target=0.8, passed=None if share is None else share >= 0.8,
                     note=json.dumps({"variants": used, "variants_that_moved_scores": moved, "inputs": len(ps), "winner": win, "move": rel,
                                      **({} if share is not None else {"skipped": "the engine of this case takes no input edits"})}))

    def regression(self) -> None:
        """The automated tests of the engine, the actors, the quality store and the API: the pass share."""
        dirs = ["tests/future", "tests/actors", "tests/quality", "tests/web", "tests/it_services"]
        t0 = time.time()
        r = subprocess.run([sys.executable, "-m", "pytest", *dirs, "-q", "--tb=no", "-p", "no:cacheprovider"], capture_output=True, text=True)
        p = int((re.search(r"(\d+) passed", r.stdout) or [0, 0])[1])
        f = int((re.search(r"(\d+) failed", r.stdout) or [0, 0])[1]) + int((re.search(r"(\d+) error", r.stdout) or [0, 0])[1])
        self.add("regression", "tests_passing", p / (p + f) if p + f else None, n=p + f, target=1, passed=f == 0 and p > 0,
                 ms=(time.time() - t0) * 1000, note=", ".join(dirs))

    # ---------------------------------------------------------------- the trust of the inputs and the answers
    def recovery(self, history: str | None = None) -> None:
        """The deployed recovery result, or, with a history directory, learn on it here and check against its truth."""
        if history:
            from variator.actors.learn import learn
            from variator.actors.recovery import load_truth, recovery

            r = recovery(learn(history, None), load_truth(history))["summary"]
            self.add("recovery", "coverage", r["coverage"], case_id=f"company:{history}", target=r.get("target_coverage", 0.9),
                     passed=r.get("meets_target"), n=r.get("estimate_rows_enough_data"), note="local history")
            return
        r = http(self.base, "/api/variator/artifacts/actors/recovery.json")["summary"]
        self.add("recovery", "coverage", r["coverage"], target=r.get("target_coverage", 0.9), passed=r.get("meets_target"),
                 n=r.get("estimate_rows_enough_data"), note="hidden behaviour parameters inside the learned p1-p99 range")
        self.add("recovery", "coverage_all_rows", r.get("coverage_all_estimate_rows"), n=r.get("estimate_rows_all"))

    def org(self) -> str:
        return http(self.base, "/api/kb/organizations")[0]["id"]

    def dossier(self, org: str, e: str) -> dict[str, Any]:
        """A dossier from the cache of this run ({} for an id that is not a record)."""
        cache = self.__dict__.setdefault("_dossiers", {})
        if e not in cache:
            cache[e] = self.entity(org, e)
        return cache[e]

    def causal(self, history: str | None, limit: int = 300) -> None:
        """Chain defects over the chains of every meeting page and decision of the corpus (and what they lead to):
        a meeting's bases after a decision made at it, cycles, a cause dated after its effect, and steps that only
        refer (reply, part of, refers to) instead of cause."""
        import gzip
        from pathlib import Path

        org, starts = self.org(), list(CAUSAL_START)
        tl = Path(history or ".variator/quality/history") / "delivery" / "fixtures" / "timeline.jsonl.gz"
        if tl.is_file():
            with gzip.open(tl, "rt", encoding="utf-8") as f:
                for line in f:
                    x = json.loads(line)
                    if x.get("object_type") == "page" and "confluence" in str(x.get("source") or x.get("event_id")):
                        starts.append(f"confluence.page-{x['external_id']}")
                    elif x.get("object_type") == "decision":
                        starts.append(f"sim.{x['external_id']}")
        seen, todo, chains, direct, late = set(), list(dict.fromkeys(starts)), [], 0, []
        while todo and len(seen) < limit:
            e = todo.pop(0)
            if e in seen:
                continue
            seen.add(e)
            d = self.dossier(org, e)
            if not d:
                continue
            chains += d.get("chains", [])
            for x in d.get("why", []):
                direct += 1
                # a cause known only after the latest version of its effect cannot explain it (1 minute of slack)
                if x["other"].get("date") and d.get("updated") and x["other"]["date"][:16] > d["updated"][:16]:
                    late.append(f"{e} <- {x['other']['id']}")
            todo += [x["other"]["id"] for x in d.get("why", []) + d.get("effects", [])
                     if x["predicate"] in ("decided_in", "implements", "informed_by", "based_on")]
        note = f"{len(seen)} records walked from {len(set(starts))} meeting pages and decisions"
        bad = [c for c in chains if re.search(r"decided at.*informed by", c)]
        self.share("causal", "chains_without_meeting_bases", len(chains) - len(bad), len(chains), 1, note=note)
        cyc = [c for c in chains if len(ids := re.findall(r"\(([\w.:-]+)@\d+\)", c)) != len(set(ids))]
        self.share("causal", "chains_without_cycles", len(chains) - len(cyc), len(chains), 1)
        self.share("causal", "causes_not_after_effect", direct - len(late), direct, 1,
                   note=json.dumps({"examples": late[:5]}))
        steps = re.findall(r"—([a-z ]+)→", " ".join(chains))
        weak = [s for s in steps if s in ("replies to", "part of", "refers to", "relates to", "mentions")]
        self.share("causal", "steps_that_cause", len(steps) - len(weak), len(steps), None,
                   note=json.dumps(dict(Counter(steps).most_common(12))))

    def causal_gold(self, history: str | None, per_class: int = 20) -> None:
        """Direct «why» links against the corpus generator's gold links (precision, recall per kind of link) and
        the roots of the «why» evaluation cases (does a chain of the record reach the known root?)."""
        import random
        from pathlib import Path

        org, root = self.org(), Path(history or ".variator/quality/history") / "delivery"
        gold: dict[str, set[str]] = {}
        by_kind: dict[str, list[tuple[str, str]]] = {}
        for line in open(root / "gold_links.jsonl", encoding="utf-8"):
            g = json.loads(line)
            gold.setdefault(g["effect"], set()).add(g["cause"])
            by_kind.setdefault(g["expressed_as"], []).append((g["effect"], g["cause"]))
        rnd = random.Random(11)
        sample = {k: rnd.sample(v, min(per_class, len(v))) for k, v in sorted(by_kind.items())}
        found_all = total_all = 0
        missing: list[str] = []
        for kind, links in sample.items():
            # a gold link whose record is not in the knowledge base is data drift of the load, not a miss of the walk
            here = [(e, c) for e, c in links if self.dossier(org, e) and self.dossier(org, c)]
            missing += [f"{e} <- {c}" for e, c in links if (e, c) not in here]
            found = sum(c in {x["other"]["id"] for x in self.dossier(org, e).get("why", [])} for e, c in here)
            found_all, total_all = found_all + found, total_all + len(here)
            self.share("causal_gold", f"recall_{kind}", found, len(here), None)
        self.share("causal_gold", "recall", found_all, total_all, 0.9, note=f"{per_class} gold links per kind, {len(sample)} kinds")
        self.share("causal_gold", "gold_records_in_kb", total_all, total_all + len(missing), 1,
                   note=json.dumps({"missing": missing[:10]}))
        # precision: every direct cause the knowledge base shows for the sampled effects, declared and inferred apart.
        # Only causes from sources that the gold covers count: the generator writes no gold for meeting pages.
        effects = {e for links in sample.values() for e, _ in links}
        covered = {x.split(".")[0] for links in by_kind.values() for _, x in links}
        for inferred in (False, True):
            shown = [(e, x["other"]["id"]) for e in effects for x in self.dossier(org, e).get("why", []) if x["inferred"] is inferred]
            inside = [(e, c) for e, c in shown if c.split(".")[0] in covered]
            ok = sum(c in gold.get(e, ()) for e, c in inside)
            self.share("causal_gold", "precision_inferred" if inferred else "precision_declared", ok, len(inside),
                       0.8 if inferred else 0.95, note=json.dumps({"effects": len(effects), "causes_without_gold": len(shown) - len(inside),
                                                                   "wrong": [f"{e} <- {c}" for e, c in inside if c not in gold.get(e, ())][:8]}))
        # roots of the «why» cases: the record asked about is the evidence that is not a root
        hit = cases = 0
        misses = []
        for line in open(root / "cases.jsonl", encoding="utf-8"):
            c = json.loads(line)
            if c["category"] != "why" or not c.get("roots"):
                continue
            roots = {r["entity_id"] for r in c["roots"]}
            start = next((ev["entity_id"] for ev in c["evidence"] if ev["entity_id"] not in roots), None)
            if not start:
                continue
            d = self.dossier(org, start)
            text = " ".join(d.get("chains", [])) + " " + " ".join(x["other"]["id"] for x in d.get("why", []))
            cases += 1
            if any(r in text for r in roots):
                hit += 1
            else:
                misses.append(c["id"])
        self.share("causal_gold", "why_cases_root_found", hit, cases, 0.9, note=json.dumps({"missed": misses}))

    def share(self, test: str, metric: str, k: int, total: int, target: float | None, note: str | None = None, **kw: Any) -> None:
        """k of n as a share with its 95% Wilson interval (``n`` of the row: the sample size, or ``kw['n']``)."""
        if not total:
            self.add(test, metric, None, target=target, note=note or "nothing to measure", **{"n": 0, **kw})
            return
        n, p, z = total, k / total, 1.96
        mid, half = (p + z * z / (2 * n)) / (1 + z * z / n), z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5 / (1 + z * z / n)
        self.add(test, metric, p, ci_low=max(0.0, mid - half), ci_high=min(1.0, mid + half), target=target,
                 passed=None if target is None else p >= target, note=note, **{"n": total, **kw})

    def questions(self, k: int) -> tuple[str, ...]:
        """«Why …?» questions made from the titles of decisions, tasks and meetings found from the known records."""
        if k <= len(QUESTIONS):
            return QUESTIONS[:k]
        org, seen, todo, titles = self.org(), set(), list(CAUSAL_START), []
        while todo and len(titles) < k and len(seen) < 4 * k:
            e = todo.pop(0)
            if e in seen:
                continue
            seen.add(e)
            try:
                d = http(self.base, f"/api/kb/{org}/entity/{urllib.request.quote(e)}")
            except Exception:
                continue
            if d.get("kind") in ("Task", "Decision", "Meeting") and d.get("title"):
                titles.append(d["title"])
            links = d.get("why", []) + d.get("effects", []) + [{"other": x} for g in d.get("related") or [] for x in g["items"]]
            todo += [x["other"]["id"] for x in links]
        return QUESTIONS + tuple(f"Why: {t}?" for t in titles[: k - len(QUESTIONS)])

    def entity(self, org: str, e: str) -> dict[str, Any]:
        """A dossier, or {} for an id that is not a record (a text that only looks like one)."""
        try:
            return http(self.base, f"/api/kb/{org}/entity/{urllib.request.quote(e)}")
        except urllib.error.HTTPError as x:
            if x.code == 404:
                return {}
            raise

    def grounded(self, k: int = 3) -> None:
        org = self.org()
        found = total = strict = false_hits = checked = 0
        num = re.compile(r"(?<![\w@.-])\$?\d[\d,]*(?:\.\d+)?%?")
        qs = self.questions(k)
        for q in qs:
            a = http(self.base, f"/api/kb/{org}/ask", {"question": q}, timeout=180)
            # the system's own numbers are not claims about the company: the knowledge cut, graph scores, time zones
            text = " ".join(l for l in a["answer"].splitlines() if not l.startswith("Knowledge cut"))
            text = re.sub(r"score \d[\d.]*|UTC[+-]\d+|\[[^\]]*\]|\(\w[\w.-]*@\d+\)|\d{4}-\d{2}-\d{2}[\dT:+ ]*", " ", text)
            # the sources: the cited records and every record the answer names by id
            ids = {c["id"] for c in a["citations"]} | set(re.findall(r"\b([a-z]+\.[A-Za-z0-9._-]*[A-Za-z0-9])(?:@\d+)?[)\];,]", a["answer"]))
            sources = " ".join(json.dumps(self.dossier(org, e), ensure_ascii=False) for e in sorted(ids)[:25]).replace(",", "")
            for x in {m.group(0).strip("$%").replace(",", "") for m in num.finditer(text)}:
                if not x or x in {"1", "2", "3"}:
                    continue  # list numbers and steps
                total += 1
                found += x in sources
            # strict, per answer line: the same value (a whole number, «12» never inside «2025») in the title, facts or
            # text of a record named on that line; a number on a line that names no record has no source
            def own(e: str) -> set[float]:
                d = self.dossier(org, e)
                body = json.dumps([d.get("title"), d.get("facts"), d.get("excerpt")], ensure_ascii=False).replace(",", "")
                return {float(v) for v in re.findall(r"(?<![\d.])\d+(?:\.\d+)?(?![\d])", body)}
            asked = {m.group(0).replace(",", "") for m in num.finditer(q)}  # the question's own numbers
            named: set[str] = set()
            for line in a["answer"].splitlines():
                line = re.sub(r"</?mark>", "", line)
                if line.startswith(("Knowledge cut", "Time:", "Understood:")):
                    continue  # the system's reading of the question, not a claim
                # a fragment line that names no record belongs to the record of the line above it
                named = set(re.findall(r"\b([a-z]+\.[A-Za-z0-9._-]*[A-Za-z0-9])(?:@\d+)?[)\];,]", line)) or named
                line = re.sub(r"\b\d{1,2}:\d{2}\b", " ", line)  # clock times
                clean = re.sub(r"score \d[\d.]*|UTC[+-]\d+|\[[^\]]*\]|\(\w[\w.-]*@\d+\)|\([a-z]+\.[^)]*\)|\d{4}-\d{2}-\d{2}[\dT:+ ]*", " ", line)
                xs = {m.group(0).strip("$%").replace(",", "") for m in num.finditer(clean)} - {"", "1", "2", "3"} - asked
                if not xs:
                    continue
                values = set().union(*(own(e) for e in sorted(named)[:10])) if named else set()
                for x in xs:
                    checked += 1
                    hit = float(x) in values
                    strict += hit
                    # negative control: a nearby value that the line does not state must not be «found» as well
                    false_hits += round(float(x) * 1.1 + 1, len(x.split(".")[1]) if "." in x else 0) in values
                    if not hit:
                        print(f"    not a value of the records on its line: {x!r} ({q}) | {line[:100]}")
        share = found / total if total else None
        self.add("grounded", "numbers_in_cited_sources", share, n=total, target=0.95, passed=share is not None and share >= 0.95,
                 note=f"{len(qs)} questions; lenient: a part of any text of a cited or named record")
        self.share("grounded", "numbers_strict", strict, checked, 0.95,
                   note=f"{len(qs)} questions; per answer line: the same value in a record named on that line")
        self.share("grounded", "control_false_match", false_hits, checked, None,
                   note="share of changed values (x1.1+1) that the strict rule would accept: the noise floor of numbers_strict")

    def answers(self, history: str | None, limit: int = 0) -> None:
        """The Ask answers against the known facts of the corpus evaluation cases (one of the «any» strings of every
        fact in the answer); a «negative» case passes when the answer says that nothing is known. The «acl» cases
        need the principals of the corpus, which the workspace roles do not map: they are counted as not run."""
        from pathlib import Path

        org = self.org()
        path = Path(history or ".variator/quality/history") / "delivery" / "cases.jsonl"
        cases = [json.loads(line) for line in open(path, encoding="utf-8")]
        run = [c for c in cases if c["category"] != "acl"][: limit or None]
        nothing = re.compile(r"no (record|information|data|evidence|match|result)|not (found|known|in the)|nothing|cannot find|could not find|unknown", re.I)
        per: dict[str, list[bool]] = {}
        fails: list[str] = []
        shown: list[tuple[dict[str, Any], str]] = []

        def holds(case: dict[str, Any], text: str) -> bool:
            # every fact in the answer, and the answer names an evidence record of the case (a bare word such as
            # «overdue» or a name in a list of people passes by chance otherwise)
            named = any(e["entity_id"].lower() in text for e in case.get("evidence", []))
            return named and all(any(str(s).lower() in text for s in f["any"]) for f in case["answer_facts"])

        for c in run:
            body: dict[str, Any] = {"question": c["question"]}
            if c.get("known_at"):
                body["asOf"] = c["known_at"][:10]
            try:
                a = http(self.base, f"/api/kb/{org}/ask", body, timeout=180)
            except urllib.error.HTTPError as e:
                per.setdefault(c["category"], []).append(False)
                fails.append(f"{c['id']}: HTTP {e.code}")
                continue
            # what the page shows: the answer text and the «why» panel (the chain nodes with their titles and statements)
            text = (a["answer"] + " " + json.dumps(a.get("why") or "", ensure_ascii=False)).lower()
            if c.get("expect_not_found"):
                ok = bool(nothing.search(a["answer"]))
            else:
                ok = holds(c, text)
                shown.append((c, text))
            per.setdefault(c["category"], []).append(ok)
            if not ok:
                fails.append(f"{c['id']} {c['category']}: {c['question'][:60]}")
        for cat, xs in sorted(per.items()):
            self.share("answers", f"correct_{cat}", sum(xs), len(xs), None)
        allx = [x for xs in per.values() for x in xs]
        self.share("answers", "correct", sum(allx), len(allx), 0.8,
                   note=json.dumps({"not_run_acl": len(cases) - len([c for c in cases if c["category"] != "acl"]), "failed": fails[:25]}))
        macro = [sum(xs) / len(xs) for xs in per.values() if xs]
        self.add("answers", "correct_macro", sum(macro) / len(macro) if macro else None, n=len(macro),
                 note="mean of the category shares: each category counts the same")
        # negative control: the facts and evidence of the next case of the same category must not pass on this answer
        fake = tried = 0
        for c, text in shown:
            same = [x for x, _ in shown if x["category"] == c["category"]]
            other = same[(same.index(c) + 1) % len(same)]
            if other is c or (other["answer_facts"], other["evidence"]) == (c["answer_facts"], c["evidence"]):
                continue
            tried += 1
            fake += holds(other, text)
        self.share("answers", "control_false_pass", fake, tried, None, note="another case's facts passing on this answer: the noise floor")
        for f in fails:
            print(f"    wrong: {f}")

    def sanity(self) -> None:
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/it_services/test_sanity_laws.py", "-q", "--tb=no", "-p", "no:cacheprovider"],
                           capture_output=True, text=True)
        p = int((re.search(r"(\d+) passed", r.stdout) or [0, 0])[1])
        f = int((re.search(r"(\d+) failed", r.stdout) or [0, 0])[1])
        self.add("sanity", "laws_holding", p / (p + f) if p + f else None, n=p + f, target=1, passed=f == 0 and p > 0)

    def calibration(self, history: str | None, cuts: int, n: int) -> None:
        """Rolling back-test: for each cut k = 1..cuts months before the end of the history, learn on the rows before
        the cut, forecast «keep the plan» to the end, and count the held-out month ends inside P10-P90 (ideal 80%)
        and inside P25-P75 through the PIT (ideal 50%)."""
        from pathlib import Path

        from variator.actors.history import load_history
        from variator.future.backtest import months_before, run_backtest

        root = Path(history) if history else None
        def add(*args: Any, **kw: Any) -> None:  # every row of a back-test names its company
            self.add(*args, case_id=f"company:{history}" if history else None, **kw)

        if root is None or not (root / "datasets").is_dir():
            add("calibration", "inside_p10_p90", None, target=0.8, note="skipped: no history (--history DIR)")
            return
        as_of = load_history(root, timeline=False).as_of
        rows: list[dict[str, Any]] = []
        for k in range(1, cuts + 1):
            t0 = time.time()
            try:
                doc = run_backtest(root, months_before(as_of, k), k, n, 7 + k)
            except ValueError as e:  # too few rows before an early cut to learn from: the row says so, the run goes on
                add("calibration", "cut_failed", None, seed=7 + k, note=f"cut {k} months: {e}"[:300])
                continue
            rows += [{**r, "cut_months": k} for r in doc["rows"]]
            add("calibration", "inside_p10_p90_cut", doc["coverage_p10_p90"], n=len(doc["rows"]), seed=7 + k,
                     ms=(time.time() - t0) * 1000, note=json.dumps({"cut": doc["cut"], "months": k, "by_metric": doc["by_metric"]}))
        inside = [r["inside_p10_p90"] for r in rows]
        mid = [0.25 <= r["pit"] <= 0.75 for r in rows]
        x = sum(inside) / len(inside) if inside else None
        add("calibration", "inside_p10_p90", x, n=len(rows), target=0.8, passed=x is not None and 0.7 <= x <= 0.9,
                 note=f"{cuts} rolling cuts; metrics cash, ARR, churn; ideal 80%")
        add("calibration", "inside_p25_p75", sum(mid) / len(mid) if mid else None, n=len(rows), target=0.5)
        for metric in sorted({r["metric"] for r in rows}):
            xs = [r["inside_p10_p90"] for r in rows if r["metric"] == metric]
            add("calibration", f"inside_p10_p90_{metric}", sum(xs) / len(xs), n=len(xs), target=0.8)
            # sharpness: a wide range is always right; the width and the interval score (Gneiting-Raftery,
            # alpha 0.2) per unit of the actual value show how much the range says
            rs = [r for r in rows if r["metric"] == metric and r["actual"]]
            width = [(r["p90"] - r["p10"]) / abs(r["actual"]) for r in rs]
            score = [((r["p90"] - r["p10"]) + 10 * max(0, r["p10"] - r["actual"]) + 10 * max(0, r["actual"] - r["p90"])) / abs(r["actual"]) for r in rs]
            if rs:
                add("sharpness", f"width_rel_{metric}", statistics.median(width), n=len(rs), note="median (P90-P10)/|actual|")
                add("sharpness", f"interval_score_rel_{metric}", statistics.median(score), n=len(rs),
                         note="median interval score / |actual|; lower is better")
        for h in sorted({r["month"] for r in rows}):
            xs = [r["inside_p10_p90"] for r in rows if r["month"] == h]
            add("calibration", f"inside_p10_p90_h{h}", sum(xs) / len(xs), n=len(xs), target=0.8,
                     note=f"forecast horizon {h} month(s)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:5173/workspace")
    ap.add_argument("--tests", default="stability,determinism,convergence,iia,weights,inputs,recovery,causal,causal_gold,answers,grounded,sanity,regression,calibration")
    ap.add_argument("--variants", type=int, default=20, help="case variants of the inputs test (each input moved by up to 5%%)")
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--n", type=int, default=500, help="scenarios per run (the deployed engine allows up to 2000 within 15 s)")
    ap.add_argument("--history", default=None, help="a generated history directory for the calibration back-test")
    ap.add_argument("--cuts", type=int, default=6, help="rolling cuts of the back-test (months before the end)")
    ap.add_argument("--questions", type=int, default=3, help="Ask questions of the grounded test (more: made from records)")
    a = ap.parse_args(argv)
    run = Run(QualityStore(), a.base.rstrip("/"))
    print(f"quality run {run.run_id} at {run.sha or 'no git'}")
    for t in a.tests.split(","):
        t = t.strip()
        try:
            {"stability": lambda: run.stability(a.seeds, a.n), "determinism": lambda: run.determinism(a.n),
             "convergence": lambda: run.convergence(max(20, a.seeds // 2)), "iia": lambda: run.iia(a.n),
             "weights": lambda: run.weights(a.n), "inputs": lambda: run.inputs(a.n, a.variants), "regression": run.regression, "recovery": lambda: run.recovery(a.history), "causal": lambda: run.causal(a.history), "causal_gold": lambda: run.causal_gold(a.history), "answers": lambda: run.answers(a.history),
             "grounded": lambda: run.grounded(a.questions), "sanity": run.sanity, "calibration": lambda: run.calibration(a.history, a.cuts, 1000)}[t]()
        except Exception as e:  # one failing test never stops the run; the row says why
            run.add(t, "error", None, note=f"{type(e).__name__}: {e}"[:300])
    print(f"done: {run.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
