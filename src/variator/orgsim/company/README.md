# variator.orgsim.company

One year of history of the MVP company (Larchwood Systems): code and numbers only, no LLM. Design: Plancy `Docs/variator_integration/` 09 §2, 04 §1.2.1 and §1.6, 10 §3.

| Need | Use |
|---|---|
| Spec and hidden truth (Pydantic, JSON) | `spec.default_spec()`, `CompanySpec.load/dump`, `Truth` (the `company_model` defaults) |
| Habits of actors | `habits.person_habits`, `habits.client_habits`, `habits.expand_cohort` |
| The day loop | `engine.Engine.run` -> `processes.{people, clients, sales, ops, support, nps, work}` |
| NPS (v2) | `processes.nps.Nps`: quarterly survey, `nps_response` records; NPS -> churn (`k_churn_nps`) |
| Monitoring (v2) | `processes.monitoring.Monitoring`: `monitoring_daily` per service, `sla_report` |
| Money after the loop | `processes.finance.Finance` (ARR scale, opening cash), `processes.reports.Reports` |
| Files | `render.Renderer` (ids, cut at T, lines, gold links, directory), `cases.build_cases` |
| Run | `generate_history(spec, anchor, seed)`, `write_history(h, out)`; CLI `python -m variator.orgsim.company.cli generate --anchor YYYY-MM-DD --seed N --out DIR` |
| Day level after T | `forward.sim.ForwardRun`; run `scripts/mvp/branch_day.py` |

Output: `delivery/`, `datasets/`, `truth.json` (hidden), `summary.json`.

Details (output files, v2 datasets, stream and delivery rules): `docs/knowledge/company-history.md`.
