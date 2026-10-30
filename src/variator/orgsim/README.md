# variator.orgsim

Simulator foundations, free of case processes. Design: Plancy `Docs/variator_integration/` 09 §2, §4, §13; 04 §1.6.

| Need | Use |
|---|---|
| CRN streams | `Streams(seed).rng(*parts)`, label `s17:churn:c-42:time` |
| E ~ Exp(1) | `Streams.unit_exponentials(process, ids, purpose, S)` |
| Event period | `first_passage(cumulative_hazard(h), E)`; `arrival_counts` |
| Keys | `slot_key("hire", 3)`, `child_key(p, j)` |
| Calendar | `BusinessCalendar`, `PeriodAxis(T, grain, n)`, `RelativeDates(T)` |
| Events | `Event`, `to_timeline_line`, `to_canonical_record` |
| Delivery | `write_delivery`, `verify_delivery`, `sample.write_sample_delivery` |
| Time to risk | `risk.first_failure_period`, `p_failed_by`, `any_time` |
| Week tree | `grow_tree(root, step, levels=, k=, max_nodes=, seed=)` |

Rules: never reuse another component's label; never put the fork in a label; give dates as offsets from T.
