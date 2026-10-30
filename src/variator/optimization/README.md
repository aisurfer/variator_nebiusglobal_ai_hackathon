# variator.optimization

Best set of options under a budget and a CVaR limit (MILP). cuOpt (local GPU or server), HiGHS fallback.

| Need | Use |
|---|---|
| Scenario matrix -> selection | `optimize_portfolio` |
| From a simulation run + model | `optimize_decision` |
| cuOpt server | env `CUOPT_SERVER_URL`, `CUOPT_SERVER_TOKEN` |

Math, solver order, pitfalls, assumptions: `docs/knowledge/optimization.md`.
