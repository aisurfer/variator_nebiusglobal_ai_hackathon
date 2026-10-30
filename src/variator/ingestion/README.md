# variator.ingestion

Raw company inputs -> grounded `Fact`s and a `DecisionBrief`.

| Need | Use |
|---|---|
| CSV profile, no LLM | `profile_csv` -> `TableProfile` (`to_prompt`, `grounding_numbers`) |
| Facts from a profile or text (Lightning) | `facts_from_profile`, `facts_from_text` |
| Facts from charts and scans (Super VL) | `read_chart`, `facts_from_image` |
| Parse the question (Lightning) | `parse_question` -> `DecisionBrief` |

Details, grounding rules, assumptions: `docs/knowledge/ingestion.md`.
