"""Code metrics without an LLM and without Docker: churn, complexity, import graph.

* ``git_churn``: commits and changed lines per file (``git log --numstat``).
* ``file_complexity``: NLOC and cyclomatic complexity per file (``lizard``, many languages).
* ``import_graph``: Python module imports (``ast``), internal edges only.
* ``component_metrics``: the three above, grouped by component (path prefixes).
* ``architecture_report``: package fan-in/fan-out, instability, cycles, hot spots.

The source code never leaves this process; callers get numbers and paths only.

Cache: the two reports are kept per git commit (``commit_cache``) in ``.cache/code_metrics``
(env ``VARIATOR_CODE_CACHE``: a folder or ``off``). A repository with uncommitted
changes is not cached. The key also has the date, because churn counts the last N days.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import os
import subprocess
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".cache", ".variator"}
CODE_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".kt",
    ".cs",
    ".go",
    ".rb",
    ".php",
    ".cpp",
    ".c",
    ".h",
    ".swift",
    ".scala",
    ".rs",
}
TOP = 10
ENV_CACHE = "VARIATOR_CODE_CACHE"
DEFAULT_CACHE = Path(".cache") / "code_metrics"


def _git_out(repo: Path, *args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def commit_cache(repo: Path, kind: str, params: Mapping[str, Any], compute: Callable[[], Any]) -> Any:
    """Return the cached result for the clean HEAD commit of ``repo``, else compute and keep it."""
    setting = os.environ.get(ENV_CACHE, "")
    head = _git_out(repo, "rev-parse", "HEAD") if setting.lower() != "off" else None
    if not head or _git_out(repo, "status", "--porcelain") != "":
        return compute()  # no git, no commit yet, or uncommitted changes
    raw = json.dumps(
        [kind, str(repo.resolve()), head.strip(), dt.date.today().isoformat(), params],
        sort_keys=True,
        default=str,
    )
    path = Path(setting or DEFAULT_CACHE) / f"{hashlib.sha256(raw.encode()).hexdigest()[:24]}.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass  # damaged entry: compute again
    result = compute()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result), encoding="utf-8")
    return result


def code_files(repo: Path, prefixes: Sequence[str] = ("",)) -> list[str]:
    """Repository-relative POSIX paths of code files under ``prefixes``."""
    found = []
    for path in repo.rglob("*"):
        if path.suffix not in CODE_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(repo)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        posix = rel.as_posix()
        if any(posix.startswith(p) for p in prefixes):
            found.append(posix)
    return sorted(found)


def git_churn(repo: Path, *, since_days: int = 365) -> dict[str, dict[str, int]]:
    """Path -> {commits, added, deleted} for the last ``since_days`` days. Empty if not a git repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", f"--since={since_days}.days", "--numstat", "--format=@%H"],
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            stdin=subprocess.DEVNULL,  # a stdio MCP server must not share its stdin pipe
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    churn: dict[str, dict[str, int]] = defaultdict(lambda: {"commits": 0, "added": 0, "deleted": 0})
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if "=>" in path:  # renames: keep the new path
            path = path.split("=>")[-1].strip(" }")
        entry = churn[path]
        entry["commits"] += 1
        entry["added"] += int(added) if added.isdigit() else 0
        entry["deleted"] += int(deleted) if deleted.isdigit() else 0
    return dict(churn)


@dataclass(frozen=True)
class FileComplexity:
    nloc: int
    functions: int
    ccn_avg: float
    ccn_max: int


def file_complexity(repo: Path, files: Iterable[str]) -> dict[str, FileComplexity]:
    import lizard

    result = {}
    for rel in files:
        info = lizard.analyze_file(str(repo / rel))
        ccns = [f.cyclomatic_complexity for f in info.function_list]
        result[rel] = FileComplexity(
            nloc=info.nloc,
            functions=len(ccns),
            ccn_avg=round(sum(ccns) / len(ccns), 2) if ccns else 0.0,
            ccn_max=max(ccns, default=0),
        )
    return result


def _module_name(rel: str, source_roots: Sequence[str]) -> str:
    path = rel.removesuffix(".py")
    for root in source_roots:
        if root and path.startswith(root.rstrip("/") + "/"):
            path = path[len(root.rstrip("/")) + 1 :]
            break
    parts = path.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def import_graph(
    repo: Path, files: Iterable[str], *, source_roots: Sequence[str] = ("src",)
) -> dict[str, set[str]]:
    """Module -> internal modules it imports (Python files only)."""
    files = [f for f in files if f.endswith(".py")]
    modules = {_module_name(f, source_roots): f for f in files}
    graph: dict[str, set[str]] = {m: set() for m in modules}
    for module, rel in modules.items():
        try:
            tree = ast.parse((repo / rel).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        package = module if rel.endswith("__init__.py") else module.rpartition(".")[0]
        for node in ast.walk(tree):
            hits: set[str | None] = set()
            if isinstance(node, ast.Import):
                hits = {_longest_known(a.name, modules) for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    anchor = package.split(".")[: max(len(package.split(".")) - node.level + 1, 0)]
                    base = ".".join([*anchor, base] if base else anchor)
                base_hit = _longest_known(base, modules)
                for alias in node.names:
                    # `from pkg import module` links the module; `from pkg import name` links pkg
                    sub = f"{base}.{alias.name}" if base else alias.name
                    hits.add(sub if sub in modules else base_hit)
            for hit in hits:
                if hit and hit != module:
                    graph[module].add(hit)
    return graph


def _longest_known(name: str, modules: Mapping[str, str]) -> str | None:
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in modules:
            return candidate
    return None


def package_graph(graph: Mapping[str, set[str]], depth: int = 2) -> dict[str, set[str]]:
    """Collapse modules to packages of ``depth`` name parts (``variator.rules``)."""

    def pkg(m: str) -> str:
        return ".".join(m.split(".")[:depth])

    out: dict[str, set[str]] = defaultdict(set)
    for src, targets in graph.items():
        out[pkg(src)]  # keep isolated packages
        for t in targets:
            if pkg(t) != pkg(src):
                out[pkg(src)].add(pkg(t))
    return dict(out)


def strongly_connected(graph: Mapping[str, set[str]]) -> list[list[str]]:
    """Tarjan's algorithm (iterative); returns only cycles (components with 2+ nodes)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    cycles: list[list[str]] = []
    counter = 0
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    cycles.append(sorted(component))
    return cycles


def component_metrics(
    repo: Path,
    components: Mapping[str, Sequence[str]],
    *,
    since_days: int = 365,
    source_roots: Sequence[str] = ("src",),
) -> dict[str, dict[str, Any]]:
    """Per component (name -> path prefixes): size, complexity, churn and coupling."""
    params = {"components": dict(components), "since_days": since_days, "source_roots": list(source_roots)}
    return commit_cache(
        repo, "components", params, lambda: _component_metrics(repo, components, since_days, source_roots)
    )


def _component_metrics(
    repo: Path, components: Mapping[str, Sequence[str]], since_days: int, source_roots: Sequence[str]
) -> dict[str, dict[str, Any]]:
    churn = git_churn(repo, since_days=since_days)
    all_files = code_files(repo, [p for prefixes in components.values() for p in prefixes])
    complexity = file_complexity(repo, all_files)
    graph = import_graph(repo, all_files, source_roots=source_roots)
    module_of = {f: _module_name(f, source_roots) for f in all_files}
    owner = {}
    for name, prefixes in components.items():
        for f in all_files:
            if any(f.startswith(p) for p in prefixes):
                owner.setdefault(f, name)
    module_owner = {module_of[f]: c for f, c in owner.items()}
    fan_out: dict[str, set[str]] = defaultdict(set)
    fan_in: dict[str, set[str]] = defaultdict(set)
    for src, targets in graph.items():
        for t in targets:
            a, b = module_owner.get(src), module_owner.get(t)
            if a and b and a != b:
                fan_out[a].add(b)
                fan_in[b].add(a)
    out: dict[str, dict[str, Any]] = {}
    for name in components:
        files = [f for f, c in owner.items() if c == name]
        cx = [complexity[f] for f in files]
        functions = sum(c.functions for c in cx)
        ce, ca = len(fan_out[name]), len(fan_in[name])
        out[name] = {
            "files": len(files),
            "nloc": sum(c.nloc for c in cx),
            "ccn_avg": round(sum(c.ccn_avg * c.functions for c in cx) / functions, 2) if functions else 0.0,
            "ccn_max": max((c.ccn_max for c in cx), default=0),
            "commits": sum(churn.get(f, {}).get("commits", 0) for f in files),
            "changed_lines": sum(
                churn.get(f, {}).get("added", 0) + churn.get(f, {}).get("deleted", 0) for f in files
            ),
            "fan_in": ca,
            "fan_out": ce,
            "instability": round(ce / (ca + ce), 3) if ca + ce else 0.0,
        }
    return out


def architecture_report(
    repo: Path,
    *,
    prefixes: Sequence[str] = ("src/",),
    source_roots: Sequence[str] = ("src",),
    depth: int = 2,
    since_days: int = 365,
) -> dict[str, Any]:
    """Package-level structure: coupling, instability, cycles and hot spots (churn x complexity)."""
    params = {"prefixes": list(prefixes), "roots": list(source_roots), "depth": depth, "days": since_days}
    return commit_cache(
        repo, "architecture", params, lambda: _architecture(repo, prefixes, source_roots, depth, since_days)
    )


def _architecture(
    repo: Path, prefixes: Sequence[str], source_roots: Sequence[str], depth: int, since_days: int
) -> dict[str, Any]:
    files = code_files(repo, prefixes)
    graph = import_graph(repo, files, source_roots=source_roots)
    packages = package_graph(graph, depth)
    fan_in: dict[str, int] = defaultdict(int)
    for targets in packages.values():
        for t in targets:
            fan_in[t] += 1
    churn = git_churn(repo, since_days=since_days)
    complexity = file_complexity(repo, files)
    hot = sorted(
        ((f, churn.get(f, {}).get("commits", 0) * complexity[f].ccn_max) for f in files),
        key=lambda kv: kv[1],
        reverse=True,
    )
    return {
        "packages": {
            p: {
                "fan_out": len(t),
                "fan_in": fan_in[p],
                "instability": round(len(t) / (len(t) + fan_in[p]), 3) if (len(t) + fan_in[p]) else 0.0,
                "depends_on": sorted(t),
            }
            for p, t in sorted(packages.items())
        },
        "cycles": strongly_connected(packages),
        "hot_spots": [
            {"file": f, "score": s, "ccn_max": complexity[f].ccn_max} for f, s in hot[:TOP] if s > 0
        ],
        "files": len(files),
        "nloc": sum(c.nloc for c in complexity.values()),
    }
