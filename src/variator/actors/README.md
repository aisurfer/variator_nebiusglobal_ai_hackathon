# variator.actors

Learn the company from its history: actors, habits with uncertainty, cohorts, the risk register and facts for actor cards. Code and statistics only, no LLM. Design: Plancy `Docs/variator_integration/` 09 §2, 04 §1.4, 10 FR5-FR6.

| Need | Use |
|---|---|
| Run | `python -m variator.actors.cli learn --history DIR --out DIR [--as-of YYYY-MM-DD] [--require-coverage]` |
| In memory | `learn.learn(history_dir, as_of)` -> `Learned` (`documents()`, `habits_doc()`) |
| Loader (as-of cut) | `history.load_history`: typed tables, rows at or after T dropped, snapshot columns never read |
| Estimators | `stats.rate`, `hazard`, `share`, `lognormal`, `censored_normal`, `pool_normal`, `pool_poisson`, `pool_rate`, `covariate_hazard` |
| Habits | `people.learn_people`, `clients.learn_clients`, `company.learn_company`, `nps.learn_nps` (v2) |
| Risks, facts | `risks.build_register`, `facts.build_facts` |
| Recovery check | `recovery.recovery(learned, truth)` (only module that reads `truth.json`) |
| Artifact set | `artifacts.write_set` -> `manifest.json`, `actors.json`, `habits.json`, `cohorts.json`, `risk_register.json`, `recovery.json`, `facts/<actor>.json` |
| Actor cards (S6) | `variator.actors.cards` |
| Tests | `tests/actors` (synthetic estimators; as-of differential test and recovery on the real history, skipped without it) |

Details (parameter format, source tags, pooling rules): `docs/knowledge/company-learning.md`.
