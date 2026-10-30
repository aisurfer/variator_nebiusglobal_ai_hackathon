# variator.reasoning

Nemotron sets up the decision and explains the results; code calculates the numbers.

| Need | Use |
|---|---|
| Question + facts -> `DecisionModel` (Super) | `build_decision_model` |
| Review and fix the model (Super) | `critique_decision_model`, `revise_decision_model` |
| Report and red team (Ultra) | `analyze_results`, `red_team` |
| Checks without an LLM | `lint_decision_model`, `ungrounded_numbers`, `sensitivity_flips` |

Details, guarantees, assumptions: `docs/knowledge/reasoning.md`.
