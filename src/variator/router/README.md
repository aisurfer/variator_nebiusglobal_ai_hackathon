# variator.router

Typed decisions (choice, score, noul) at the cheapest tier that is sure enough (confidence 0.8).

| Need | Use |
|---|---|
| Resolve | `Resolver(backends, cache=DecisionCache(dir)).decide(TypedQuestion(...), local=match_choice)` |
| T1 backend | `NemotronBackend` (Lightning, reasoning off); `TypeSafeBackend` is a stub |

Order: T0 local -> disk cache -> backends; no confident tier -> best answer with `escalate=True`.
