# variator.quality

Data quality score and Occam's razor.

| Need | Use |
|---|---|
| Quality of a column | `score_series(values, origin, age_days)` -> Q in [0, 1] |
| Simplest trend (AIC) | `select_trend(y)` |
| Distribution from data | `fit_distribution(values, q)` |
| Simplify a model | `swing_shares(model)` -> `simplify(model, quality, swing_shares=, keep=)` |

Details: `docs/knowledge/platform.md`.
| Platform quality runs (table `quality_runs`) | `QualityStore` (`runs.py`); run `scripts/quality_run.py`, report `scripts/quality_report.py` -> `reports/quality/slide10.json` |
