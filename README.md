# VARIATOR

**Test your management decisions before you make them.** VARIATOR builds a digital twin of a company from its own records: mail, tickets, meetings, metrics. A leader asks a question ("Which lever gives us the best year?"). VARIATOR lays out the forks, the world hypotheses and the risks. It simulates thousands of twelve-month futures on the learned behaviour of the company's people and clients, and ranks the options on the leader's criteria. Nemotron designs the case and explains the result; code calculates every number.

Built for the [Nebius x NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/) with **NVIDIA Nemotron on Nebius Token Factory**.

Live demo: https://variator.trijet.tech/

## How it works

```
company records (simulated company: Larchwood Systems)
        |
knowledge base: journal (Postgres) + search (OpenSearch) + graph (Memgraph)      kb/
        |
actors: habits, cohorts and risks learned from the records (as of date T)         src/variator/actors
        |
case: forks, world hypotheses, risks  <-  [Nemotron 3 Ultra] from the question   src/variator/future
        |
month Monte Carlo on actors + week event tree (NumPy)                            src/variator/future
        |
ranking on the leader's criteria, P(best), close calls                          src/variator/mcda, rag
        |
workspace: CEO brief, case pages, Ask, actor cards                              workspace/
```

Nothing after the as-of date T is used: the twin learns from records before T and simulates from T.

## Which model does what

`ROLE_MODEL` in `src/variator/llm/models.py` is the default of every call site.

| Model (Token Factory) | Role |
|---|---|
| `nvidia/Nemotron-3_5-Lightning` | the actor swarm (a year of mail, tickets and notes), extraction, typed decisions |
| `nvidia/nemotron-3-super-120b-a12b` | decision model builder, risks and critique, agent tool loop |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | actor cards from the records, case design, explanation, report, red team |
| `nvidia/Nemotron-3.5-Super-VL` | charts and scanned reports |

## Repository layout

| Path | What |
|---|---|
| `src/variator/` | the engine: company history generator (`orgsim`), actors, metrics, futures and the case drafter (`future`), ranking (`mcda`, `rag`), Token Factory client (`llm`), MCP server and agent, web API (`web`) |
| `plugins/` | domain templates: `variator_it_services` (the IT services company model), `variator_logistics` |
| `kb/` | the knowledge base service (API and worker) and its corpus tools (`kb/sim`) |
| `workspace/` | the web app: React + Vite front end, Express server |
| `scripts/mvp/` | the precompute pipeline (`pipeline.py`) and the case generators |
| `scripts/quality_*.py` | quality runs, including the case re-drafting stability test |
| `data/demo/` | synthetic demo inputs of three generic decision cases |
| `deploy/` | the Docker stack for a local run |
| `tests/`, `workspace/tests/`, `kb/tests/` | tests |

## Run the tests

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 24+.

```bash
uv sync                                    # engine, plugins, dev tools
uv run pytest -q -n auto                   # offline, no keys

cd workspace
npm ci
npm run typecheck && npm test && npm run build

cd ../kb
uv sync
uv run pytest -q tests/unit                # integration tests need the running stack
cd sim && uv run --project .. python -m pytest -q tests
```

Some tests skip without locally generated artifacts (a company history and learned actors) or without API keys; the skip reason says which.

## Run the app locally

Requirements: Docker with Compose, about 10 GB of free disk.

```bash
cp deploy/mvp/.env.example deploy/mvp/.env # optional: ports, domain, login
make -C deploy/mvp up                      # builds and starts the stack: http://127.0.0.1:8088/workspace/
make -C deploy/mvp help                    # every command
```

The stack starts empty. The precompute pipeline fills it: it generates the company history, loads it into the knowledge base, learns the actors, builds the CEO brief, the cases and their futures, and publishes the artifact sets.

```bash
uv sync                                                # the pipeline runs on the project's .venv
export MVP_RELEASE_ROOT=$HOME/.cache/variator/release  # its work and artifacts; the stack mounts the history from here
export NEBIUS_API_KEY=...                              # the Nemotron stages (template: .env.example)
uv run python scripts/mvp/pipeline.py run --dry-run    # what would run, and why
make -C deploy/mvp precompute                          # runs every stale stage
```

The knowledge base load embeds the history on CPU and takes a few hours. The `--llm` stages call Nemotron through Token Factory. Set `MVP_RELEASE_ROOT` before `make -C deploy/mvp up` as well, so the stack reads the same history.

## Other entry points

```bash
uv run variator-mcp                        # MCP server (stdio)
uv run variator-mcp --http 8000            # MCP server over HTTP
uv run variator-agent "Which delivery model for the next release?"
```

## License

[Apache-2.0](LICENSE)
