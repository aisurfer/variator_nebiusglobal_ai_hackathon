# variator.simulation

Monte Carlo for a `DecisionModel` on NumPy or CuPy. Formulas: `variator.expr`.

| Need | Use |
|---|---|
| One metric, tornado, matrix for cuOpt | `simulate` -> `SimulationRun` |
| All criteria + rules -> `X[s, o, k]` | `simulate_tensor` -> `TensorRun` |
| Backend | `get_backend("auto" \| "numpy" \| "cupy")` |
| Distributions, copula | `ppf`, `quantile`, `build_copula` |
| Sobol (4-8x fewer scenarios) | `sampling="sobol"`; `sampling.draw_uniforms` |

Method, speed, NaN policy, GPU assumptions: `docs/knowledge/simulation.md`.
