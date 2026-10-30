# variator.rules

If-then / if-elif-else rule packs in YAML, run on all scenarios at the same time.

| Need | Use |
|---|---|
| Load a pack | `load_pack(path \| yaml text \| dict)` -> `RulePack` (`templates` limits auto-loading) |
| Compile against a model | `compile_rules(packs, variables=..., options=..., criteria=..., derived=...)` |
| Run | `RuleSet.apply(env, option, xp, n)` -> `RuleOutcome` (env, fired, failed, flags) |
| Syntax for clients | resource `variator://guide/rules` |

Kinds: `effect` (set variables), `check` (fail = infeasible), `flag` (RAG color). Details: `docs/knowledge/platform.md`.
