# variator.timeline

The future on a month axis: can the milestones be met, and with which resources, how much and when. Contract: `domain.Timeline` (items + levers); a month is a model variable.

| Need | Use |
|---|---|
| P(met), forecast bands, inputs in conflict | `evaluate(model, timeline, rules, events=, moves=)` -> `TimelineResult` |
| What to do, how much, when | `solve_plan(model, timeline, option, rules)` -> `Plan` (steps, P with the plan, `met`) |
| Proposed milestone months (80% met) | `suggest(result, timeline, option)` -> `{month variable: month}` |
| Placed events, moved markers | `apply(model, timeline, events, moves)`; `domain.with_values` |
| Per-scenario values | `simulation.simulate_tensor(probes={name: formula})` |

Example timeline: `variator_it_services.timeline.DELIVERY_TIMELINE`; page `it-delivery-simulator.html`; parity `check_playground.py [--timeline-case]`.
