"""Domain plugins: discovery, contract and mounting.

A plugin is a ``Plugin`` object. Sources, in this order:

1. Entry points in the group ``variator.plugins`` (installed packages).
2. ``VARIATOR_PLUGINS``: comma-separated ``module:attribute`` (not installed code).
3. Remote MCP servers: ``VARIATOR_REMOTE_MCP`` = JSON ``{"namespace": "url | path"}``.
   They are mounted through ``fastmcp.server.create_proxy``; their tools appear as
   ``<namespace>_<tool>``.

A local plugin adds criteria (new axes), rule packs, case templates, scenario presets per
template (``domain.Scenario``), timelines per template (``domain.Timeline``) and optional
domain tools (its own ``FastMCP`` server, mounted with the plugin name as namespace).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from variator.domain import Criterion, DecisionModel, Scenario, Timeline
from variator.rules import RulePack, load_pack

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "variator.plugins"
ENV_PLUGINS = "VARIATOR_PLUGINS"
ENV_REMOTE = "VARIATOR_REMOTE_MCP"

Template = Callable[[], DecisionModel]
Extension = Callable[[DecisionModel], DecisionModel]


@dataclass
class Plugin:
    name: str  # namespace of its tools, snake_case
    description: str
    criteria: list[Criterion] = field(default_factory=list)
    rule_packs: list[RulePack] = field(default_factory=list)
    templates: dict[str, Template] = field(default_factory=dict)
    extend: Extension | None = None  # adds this plugin's axes to another model
    server: FastMCP | None = None
    data_dir: Path | None = None  # demo inputs
    scenarios: dict[str, list[Scenario]] = field(default_factory=dict)  # template -> presets
    timelines: dict[str, Timeline] = field(default_factory=dict)  # template -> timeline

    def pack(self, name: str) -> RulePack:
        for pack in self.rule_packs:
            if pack.pack == name:
                return pack
        raise KeyError(f"plugin {self.name!r} has no rule pack {name!r}")

    def card(self) -> dict[str, Any]:
        """Compact description for the `plugins` tool."""
        return {
            "name": self.name,
            "description": self.description,
            "criteria": [c.name for c in self.criteria],
            "templates": sorted(self.templates),
            "scenarios": {t: [s.name for s in items] for t, items in self.scenarios.items()},
            "timelines": sorted(self.timelines),
            "rule_packs": [p.pack for p in self.rule_packs],
            "extends_models": self.extend is not None,
            "tools": self.server is not None,
            "data": sorted(p.name for p in self.data_dir.iterdir())
            if self.data_dir and self.data_dir.is_dir()
            else [],
        }


def load_packs_from_dir(directory: Path, source: str) -> list[RulePack]:
    packs = []
    for path in sorted(directory.glob("*.y*ml")):
        pack = load_pack(path)
        pack.source = source
        packs.append(pack)
    return packs


def _coerce(obj: Any, origin: str) -> Plugin:
    plugin = obj() if callable(obj) and not isinstance(obj, Plugin) else obj
    if not isinstance(plugin, Plugin):
        raise TypeError(f"{origin} did not give a Plugin (got {type(plugin).__name__})")
    return plugin


def discover(spec: str | None = None, *, use_entry_points: bool = True) -> dict[str, Plugin]:
    """Find local plugins; a plugin that fails to load is logged and skipped."""
    found: dict[str, Plugin] = {}
    if use_entry_points:
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                plugin = _coerce(ep.load(), f"entry point {ep.name}")
            except Exception as exc:  # one bad plugin must not stop the server
                log.warning("plugin %s not loaded: %s", ep.name, exc)
                continue
            found.setdefault(plugin.name, plugin)
    for item in filter(
        None, (s.strip() for s in (spec if spec is not None else os.environ.get(ENV_PLUGINS, "")).split(","))
    ):
        module_name, _, attr = item.partition(":")
        try:
            module = importlib.import_module(module_name)
            plugin = _coerce(getattr(module, attr or "plugin"), item)
        except Exception as exc:
            log.warning("plugin %s not loaded: %s", item, exc)
            continue
        found[plugin.name] = plugin
    return found


def remote_servers(spec: str | None = None) -> dict[str, str]:
    raw = spec if spec is not None else os.environ.get(ENV_REMOTE, "")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{ENV_REMOTE} must be a JSON object {{namespace: url_or_path}}")
    return {str(k): str(v) for k, v in data.items()}


def mount_all(server: FastMCP, plugins: dict[str, Plugin], remotes: dict[str, str]) -> list[str]:
    """Mount plugin tool servers and remote proxies; return notes for failures."""
    from fastmcp.server import create_proxy

    notes = []
    for plugin in plugins.values():
        if plugin.server is not None:
            server.mount(plugin.server, namespace=plugin.name)
    for namespace, target in remotes.items():
        try:
            proxy_target: Any = Path(target) if not target.startswith(("http://", "https://")) else target
            server.mount(create_proxy(proxy_target), namespace=namespace)
        except Exception as exc:
            notes.append(f"remote MCP {namespace!r} not mounted: {exc}")
    return notes
