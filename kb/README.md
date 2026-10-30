# Knowledge backend (`kb/`)

A copy of the Plancy knowledge backend for the VARIATOR MVP, taken from the Plancy repository
(`magic_button/plancy_dashboard`, directory `kb/`) at commit `5e32db0d96e1e16eee3f3c1a0399b405bb868393`.
The MVP copies the code as it is, without porting it to VARIATOR conventions; the production iteration will
port it properly.

What it does: a bitemporal journal of source records (every fact has an effective time and a time it became
known), projections into a knowledge graph (Memgraph) and hybrid search (OpenSearch, BM25 plus fastembed vectors),
a causal engine (why, effects, impact, stale dependencies, link editor) and `/ask`, which answers questions from the
journal with citations and an explicit time interpretation. `sim/kbsim` is the simulator package that writes and
validates corpora in the formats the connectors read (Jira, Confluence, GitLab, mail, the people directory, and the
structured records of VARIATOR's organisation simulator).

## Layout

| Path | What |
|---|---|
| `app/kb/` | the backend: `api/` (FastAPI), `worker/` (outbox consumers), `cli.py`, `journal/`, `ingest/`, `connectors/`, `search/`, `graph/`, `causal/`, `reasoning/`, `db/migrations/` |
| `config/` | the ontology (`ontology.yaml`) and the sample tenant config (`sources.yaml`, `people.yaml`); a tenant's own config goes to `config/tenants/<tenant>/` |
| `sim/kbsim/` | simulator: world event log, densify planning and writer request files, export to source formats, corpus contract, delivery, intake validation, audit |
| `tests/unit/`, `sim/tests/` | offline tests |
| `tests/integration/` | tests that need PostgreSQL, OpenSearch and Memgraph |

Entry points: `uvicorn kb.api.main:app` (API, `GET /health`), `python -m kb.worker.main` (projection worker),
`python -m kb.cli {migrate,gc,seed,replay,drain,reindex,stats}`. The API runs `migrate()` at startup. The
worker consumers are `search`, `descriptors` and `graph`. The default tenant is `company`.

## The simulation provider `sim`

`app/kb/connectors/sim.py` reads the structured business records of VARIATOR's organisation simulator. A timeline
line of a `sim` source carries the simulator's event envelope (`Event.to_timeline_line`: `run, option, scenario, ts,
process, kind, entity_id, fields, causes, seq`); `external_id` is the `entity_id`, and the entity is
`sim.<entity_id>`. Every event of an object is a new version of one entity, so `fields` is the full state of the
object and `causes` every cause that still holds.

| object_type | kind | object_type | kind |
|---|---|---|---|
| `contract` | Contract | `requisition` | Requisition |
| `invoice` | Invoice | `payroll` | Payroll |
| `payment` | Payment | `incident` | Incident |
| `bank_entry` | BankEntry | `deploy` | Deploy |
| `deal` | Deal | `support_ticket` | SupportTicket |
| `employee_event` | EmployeeEvent | `cloud_bill` | CloudBill |
| `decision` | Decision | `tax_payment` | TaxPayment |

The cause catalog holds what a cause points at when it is not a business record: `hypothesis` (Hypothesis),
`life_event` (LifeEvent), `habit` (Habit), `rule` (Policy), `process` (Process). Any other object type fails and the
journal quarantines the delivery. Declared causes become causal links without text extraction: decision →
`implements`, hypothesis → `based_on`, life_event → `caused_by`, habit → `motivated_by`, rule → `governed_by`, process
→ `caused_by`. A cause id is the `entity_id` of another simulated object (or a knowledge-base entity id such as
`jira.ENG-1`). People (`owner`, `assignee`, `author`, `reporter`, `employee`, `actor`) and organisations (`client`,
`vendor`, `counterparty`, `authority`) are people.yaml ids; `contract`, `settles`, `records`, `deal`, `incident` and
`related` point at other objects. The required fields of each type are `kbsim.contract.SIM_REQUIRED`;
`kbsim.intake validate` checks them (`sim_records`), the cause targets (`dangling`), the people and organisations
(`accounts`) and that each line names a container or carries an acl.

`tests/fixtures/sample-en` is an English delivery written with VARIATOR's code: its orgsim sample (Jira, GitLab, mail)
plus `sim` lines of every object type and every cause type. The timeline is kept unpacked; `materialize()` in
`tests/unit/connectors/sample_en.py` writes the gzipped delivery that `kbsim.intake validate` reads.

## Running the tests

The unit tests need no database, search cluster or network. With an environment that has the dependencies of
`pyproject.toml` (for example `uv sync`):

```sh
cd kb
make test-unit      # backend unit tests
make test-sim       # kbsim tests (they run from sim/, where `tests` is the simulator's own package)
make check-english  # no Cyrillic anywhere in kb/
```

`make test-unit PY=/path/to/python` uses another interpreter. Both targets point every service at a dead port, so
nothing can reach a running stack. The integration tests (`make test-integration`) need the stack from
`deploy/mvp` and `KB_PG_DSN`, `KB_OS_URL` and `KB_MEMGRAPH_URI` pointing at it. Do not point them at a shared
stack: they create and drop their own tenants.

## Configuration

Every setting is a `KB_*` environment variable (`app/kb/settings.py`). Embeddings default to the local
`fastembed` model, which the Dockerfile bakes into the image so the stack runs offline. The LLM is optional: it
is a chat completions API (Nebius Token Factory, `KB_LLM_BASE_URL`, `KB_LLM_MODEL`), and its key comes only from
`KB_LLM_API_KEY` or the docker secret file `KB_LLM_KEY_FILE`. Without a key, `/ask` answers from templates.

## What was removed compared with Plancy

- **Russian.** Plancy processed Russian and English; this copy is English only. The following were removed:
  - the Russian rules of the question router, the query planner and the time parser (month names, relative and
    rolling periods, "as of" phrasing);
  - the surname declension used for grounding;
  - the Russian answer and explanation templates and the Russian LLM prompts;
  - the Russian relationship and property labels of the connectors;
  - the Russian analyzer and the `.ru` sub-fields of the search index (the index mapping version was bumped);
  - the folding of the Cyrillic letter yo into ye;
  - the Russian ontology labels and aliases.

  `kb.lang` is English only, and `tenant.language` accepts only `en`.
- **Data.** The Kedr corpus (`fixtures/kedr`, `sim/tenants/kedr`, `config/tenants/kedr`, `sim/out`) was not copied,
  and neither was the Vector Forge replay fixture (`fixtures/vectorforge`), because it mixes Russian and English. The
  tests that replayed those fixtures were removed; a few connector tests now build English payloads inline, and the
  English delivery `tests/fixtures/sample-en` goes through every registered adapter.
- **Subsystems outside the MVP:**
  - text extraction (`extract/`: implicit references, the rule and LLM extractors, the extract worker);
  - the candidate review API;
  - the XWiki and Liferay connectors;
  - the MCP server;
  - the what-if `causal/simulate` and the `/causal/simulate` route;
  - the agentic tool-calling loop of `/ask` (`mode=agent` is accepted and behaves like `auto`);
  - the `rederive` CLI command.

  From kbsim, the following were not copied: the LLM company engine (director, engine, agents, run, llm, validate,
  step), check_scenario, bench, eval, links, planner_eval and cards. `kbsim.intake` now validates only.
  `kbsim/tenant.py` replaces the engine-bound state loader.
- **Plancy wiring.** The compose file and its Makefile targets are gone (the stack is in `deploy/mvp`).
