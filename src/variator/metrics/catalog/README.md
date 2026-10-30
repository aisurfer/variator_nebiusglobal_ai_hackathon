# variator.metrics.catalog

Metric catalogue of the CEO brief (the one MVP role sees all) over the company history. Code only, no LLM.
Design: Plancy `Docs/variator_integration/` 10 §2, FR14, FR16; 04 part 2.

| Need | Use |
|---|---|
| Load a history (as-of aware) | `datasets.load_history(dir)`; rows known at or after T are dropped (`dropped_after_t`) |
| Definitions, brief layout | `registry.CATALOG`, `BY_ID`, `brief_layout()` (top row, areas) |
| Parameters (targets, bands, windows, options) | `registry.default_params()`, `params.merge_params(base, override)` |
| One metric as of a date | `engine.run` (value only), `engine.evaluate` -> `KbMetricResult` + `MetricResult` |
| All metrics, artifact set | `artifacts.compute_all`, `artifacts.write_set`; checks `artifacts.as_of_violations` |
| Sanity against the generator | `sanity.sanity(data, evals)` (`summary.json`, `truth.json`) |
| KB entity ids | `ids.sim_id`, `jira_id`, `mr_id`, `mail_id`, `person_id`, `org_id` (rules of `kb/app/kb/connectors`) |
| CLI | `python -m variator.metrics.catalog.cli compute --history DIR --out DIR [--params FILE] [--as-of DATE]` |
| Default parameters as a file | `python -m variator.metrics.catalog.cli params --out FILE` |

Tests: `tests/metrics/catalog/` (synthetic history in `conftest.py`; the real run skips without a history).

Details (as-of rule, unknown values, money, KB ids, `MetricRef`): `docs/knowledge/company-learning.md`.
