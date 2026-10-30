# variator.mcda

Multi-criteria ranking over the tensor `X[s, o, k]`. NumPy only; a weight change needs no LLM.

| Need | Use |
|---|---|
| Ranking | `rank(run, weights, risk_aversion=0.25)` -> `Ranking` (score, TOPSIS, P(best), Pareto, robustness) |
| Parts | `normalize`, `weighted_scores`, `topsis`, `pareto_mask`, `weight_robustness`, `tensor_stats`, `decision_matrix` |
| Meaningful differences | `Criterion.tie_within` (or `tie_from` a variable); `snap` makes smaller gaps ties in the score and P(best); `feasible_key`; `with_ties(run, ties)` for runs from `metrics.cube` |

Details: `docs/knowledge/platform.md`.
