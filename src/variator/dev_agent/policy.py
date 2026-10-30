"""Load the provider-neutral project workflow for the Nemotron developer agent."""

from __future__ import annotations

from pathlib import Path

POLICY_PATHS = (
    "docs/instructions/nemotron/policy.md",
    "docs/instructions/nemotron/skills/checkpoint.md",
    "docs/instructions/nemotron/skills/playground-page.md",
    "docs/instructions/nemotron/skills/variator-builder/SKILL.md",
    "docs/instructions/nemotron/skills/variator-builder/avoid.md",
    "docs/instructions/nemotron/skills/variator-builder/cases.md",
    "docs/instructions/nemotron/skills/variator-builder/dashboards.md",
    "docs/instructions/nemotron/skills/variator-builder/gates.md",
    "docs/instructions/nemotron/skills/variator-builder/module.md",
    "docs/instructions/nemotron/skills/variator-builder/pages.md",
    "docs/instructions/nemotron/skills/variator-builder/platform.md",
    "docs/instructions/nemotron/skills/variator-builder/product.md",
    "docs/instructions/nemotron/skills/variator-builder/refresh.md",
    "docs/instructions/nemotron/skills/variator-builder/workflow.md",
    "docs/instructions/nemotron/skills/variator-builder/writing.md",
    "docs/instructions/nemotron/agents/page-reviewer.md",
    "docs/instructions/nemotron/agents/test-runner.md",
)


def load_policy(workspace: Path) -> str:
    """Return the complete local workflow without reading global configuration."""
    sections: list[str] = []
    for relative in POLICY_PATHS:
        path = workspace / relative
        if path.is_file():
            sections.append(f"\n\n## Instruction: {relative}\n\n{path.read_text(encoding='utf-8')}")
    if not sections:
        raise FileNotFoundError("no Nemotron workflow files found")
    return "".join(sections)
