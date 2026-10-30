# variator.expr

Base layer: the safe formula compiler (no eval). `rules` and `simulation` use it.

| Need | Use |
|---|---|
| Plain formula | `compile_formula(text)` -> `.names`, `.evaluate(env, xp, size)` |
| Formula with derived values | `compile_with_derived(text, derived)` (each value one time, dependency order) |

Never expand derived values as text: deep chains grow exponentially (`docs/knowledge/pitfalls.md`).
