# variator.domain

Shared contracts. Change with care: every module depends on them.

| Need | Use |
|---|---|
| Decision model | `DecisionModel` (variables, options, criteria, derived, risks, correlations), `DecisionOption`, `Variable`, `Distribution` |
| Criteria and bands | `Criterion`, `RagThresholds`, `RagColor`, `CORE_CRITERIA` |
| Named formulas | `Derived`; compile with `expr.compile_with_derived` (`DecisionModel.expand` is display only) |
| Evidence and results | `Fact`, `SimulationResult`, `OptimizationResult` |
| Example | `examples.warehouse_decision()` |
