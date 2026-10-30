"""Nemotron loop for project development tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from variator.dev_agent.policy import load_policy
from variator.dev_agent.workspace import (
    ApplyPatchParams,
    DeveloperWorkspace,
    FindFilesParams,
    ReadFileParams,
    RunCommandParams,
)
from variator.llm import TokenFactoryClient, Tool, ToolLoopResult

SYSTEM_PREFIX = """You are the VARIATOR developer agent. Work only inside the supplied workspace.
Use tools before making factual claims about files or commands. Apply changes only through apply_patch.
Do not request network access. Do not use credentials. Do not create, delete, rename, commit, push, or publish
unless the user explicitly asks and the corresponding tool capability is enabled. Follow the loaded project policy.
"""


@dataclass(frozen=True)
class DevAgentResult:
    """The final reply and audited tool calls of one developer-agent run."""

    answer: str
    tool_loop: ToolLoopResult


def run_dev_agent(
    request: str,
    *,
    workspace: Path,
    llm: TokenFactoryClient,
    model: str = "super",
    max_steps: int = 12,
    allow_git_write: bool = False,
) -> DevAgentResult:
    """Run Nemotron against the project policy and a bounded local workspace."""
    tools = DeveloperWorkspace(workspace, allow_git_write=allow_git_write)
    policy = load_policy(tools.root)
    result = llm.run_tools(
        model,
        [{"role": "system", "content": SYSTEM_PREFIX + policy}, {"role": "user", "content": request}],
        [
            Tool("read_file", "Read a UTF-8 workspace file with line numbers.", ReadFileParams, tools.read_file),
            Tool("find_files", "List workspace files that match a glob pattern.", FindFilesParams, tools.find_files),
            Tool("apply_patch", "Apply one checked unified Git patch in the workspace.", ApplyPatchParams, tools.apply_patch),
            Tool("run_command", "Run one allowlisted command inside the workspace.", RunCommandParams, tools.run_command),
        ],
        max_steps=max_steps,
        reasoning=False,
        max_tokens=4_000,
    )
    return DevAgentResult(answer=result.final.content, tool_loop=result)
