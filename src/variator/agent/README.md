# variator.agent

Nemotron tool loop over the variator-core MCP tools (in-memory client).

| Need | Use |
|---|---|
| Run | `run_agent(question, server=, llm=, tools=CORE_TOOLS, extra_tools=, stats=)`; CLI `variator-agent "question"` |
| Token limits | only the exposed tools; tool replies cut to `MAX_TOOL_CHARS` |
