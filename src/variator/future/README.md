# variator.future

The future from the learned habits (WP18): the month Monte Carlo on actors (E1) and the week tree (E2). Reads `variator.actors` habits, never the generator truth.

| Need | Use |
|---|---|
| Run case B | `python -m variator.future.cli run --case company_best_year --n 1000 --seed 7 --out DIR` |
| Run case A | `python -m variator.future.cli run --case delivery_best_way --out DIR` |
| Backtest | `python -m variator.future.cli backtest --months 6 --out DIR` |
| Parameters, state at T | `params.Habits`, `company.company_params`, `state.build_world` |
| Case B engine | `company.CompanyModel.run` (month), `.step` (month or week) |
| Forks, hypotheses | `forks.company_forks`, `company_layers`, `compile_knobs` |
| Page output | `export.month_json`, `build_cube` -> `to_tensor_run`, `decide` |
| Week tree, day replay | `tree.build_trees`, `tree.replay(model, fork, seed, keys)` |
| Case A | `delivery.simulate`, `delivery.run_cli` |
| Draft case B (Ultra) | `python -m variator.future.design generate --questions F --out DIR`, `design.to_case_config` |
| Tests | `tests/future` (skip without the actors and the history) |

Output contract (`month.json`, `week-tree.json`, the replay for WP19) and model notes: `docs/knowledge/future-engine.md`.
