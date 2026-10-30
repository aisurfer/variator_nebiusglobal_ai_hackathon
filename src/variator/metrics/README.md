# variator.metrics

Metric layer of the simulator: results in the Plancy UI shape and the metric cube. Design: `04-ANSWERS.md` §2.2-2.6.

| Need | Use |
|---|---|
| Workspace KB JSON | `results.KbBrief`, `KbMetricResult` (`workspace/src/kb/types.ts`); one role `ceo`; camelCase |
| Plancy GenUI result JSON | `results.MetricResult`, `LineageNode`, `RecordRef` (`src/genui/contracts.ts`) |
| Status from bands | `status_of(value, direction, bands)` (the rule of Plancy `statusOf`) |
| Cube | `MetricCube(run, options, metrics, periods, values[S,O,K,T], status, checks, inputs)` |
| Storage | `save_cube(cube, dir)` / `load_cube(dir)`: `arrays.npz` + `cube.json` (`CubeFormat` interface) |
| Bridge to the decision layer | `to_tensor_run(cube, criteria, period)` -> `TensorRun` for `mcda.rank`, `best_path`, `rag` |

Bridge rules:
- A criterion is `(metric_id, "higher"/"lower", weight)` or `CriterionSpec` (with a fold over periods).
- Checks fail a (scenario, option) when they fail in any period up to `period` (`checks="through"`).
- Unknown (NaN) values raise; `unknown="infeasible"` keeps them out of P(best).
- Inputs become probes `in:<name>`; first failures become `first_fail:<check>`.
