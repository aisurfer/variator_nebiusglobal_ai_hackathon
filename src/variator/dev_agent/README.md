# variator.dev_agent

Nemotron developer agent: a tool loop over a tool set bound to one Git workspace (read, find, patch, run).
System prompt: a fixed prefix (workspace only, changes by patch only, no network, no credentials) + the policy.

| Need | Use |
|---|---|
| Run | `run_dev_agent(request, workspace=, llm=, model="super", max_steps=12, allow_git_write=False)` -> `DevAgentResult` (`answer`, `tool_loop`) |
| CLI | `uv run variator-dev-agent "request" [--workspace DIR] [--model M] [--max-steps N] [--allow-git-write]` (or `python -m variator.dev_agent`); workspace default: the current directory; prints JSON (answer, steps, model) |
| Tools | `DeveloperWorkspace`: `read_file`, `find_files` (glob), `apply_patch` (`git apply --check`, then apply), `run_command` |
| Policy | `policy.load_policy(root)`: `policy.md`, skills and agents of `docs/instructions/nemotron/` (`POLICY_PATHS`) |
| LLM | `llm.TokenFactoryClient.run_tools` (env `NEBIUS_API_KEY`), reasoning off |

Limits: paths stay in the root (it needs `.git`); no shell; no delete or rename patches; `run_command` takes
`git` (read subcommands; `add`, `commit` only with `allow_git_write`), `rg` and `uv run`; output cut to 40,000 chars.
