# variator.narrative

Management layer over a generated company history: post-mortems, weekly syncs, leadership reviews, QBRs, escalations and decision rationales. Code plans all facts; headless Nemotron writers write only the prose; a checker rejects invented facts. Plan: Plancy `Docs/variator_integration/12-MVP-PROGRESS.md` WP12.

| Need | Use |
|---|---|
| Run (resumable) | `python -m variator.narrative.cli {plan,write,check,apply} --history DIR --work DIR`; `apply` also takes `--out DIR` |
| Read the base history | `base.Base` (timeline by object, directory, calendar, datasets; read-only) |
| Triggers and facts | `planner.plan(base)` -> artifacts with facts packets |
| Facts packet | `facts.Packet` (`for_writer()`; one display form per value) |
| Writers | `writers.Writers.run` (`nemotron -p --model lightning`; requests, checks, retries) |
| Fact checker | `check.check_artifact` -> problems (empty: accepted) |
| Lines and gold links | `render.render` (payload formats and ids of `orgsim.company.render`) |
| Merge | `apply.apply` -> `<out>/delivery`, `datasets/`, `truth.json`, `summary.json` |
| Tests | `tests/narrative` (fake writer, no LLM; plan and apply tests skip without the base history) |

Details (steps, triggers, checker and writer rules): `docs/knowledge/company-history.md`.
