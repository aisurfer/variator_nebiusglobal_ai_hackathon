"""Short guides served as MCP resources (``variator://guide/{topic}``).

A client loads a guide only when it needs one, so the default context stays small.
"""

GUIDES = {
    "flow": """\
1 plugins_list
2 ingest(source) | ingest_inline(data) -> ds*; quality(ds)
3 research_business(business, geography, max_queries) -> research* (optional external context)
4 case_from_template(plugin, template, extend=[..]) | case_from_question(question, datasets, research)
5 rules_add(case, yaml | plugin+pack); case_patch(case, ops)
6 simulate(case) -> run*
7 rank(run, weights) | best_path(run) | rag(run, limits) | tunnel(run, x, y)
8 simplify_case(case) if low_quality is not empty; then simulate again
9 report(run) -> HTML; explain(run) only if the user asks for text
""",
    "rules": """\
pack: <name>
rules:
  - id: <id>
    kind: effect | check | flag
    if: <formula>                      # names = case variables and derived values
    then: {set: {<var>: <formula>}} | {fail: <msg>} | {flag: red|amber|green, criterion: <name>}
    elif: [{if: <formula>, then: <action>}]
    else: <action>
    options: [<option>]                # optional scope
Formula: + - * / ** % // < <= > >= == != and or `a if c else b`
  min max abs exp log sqrt clip where minimum maximum
Order: effects (pack order) -> checks, flags. First true branch wins.
`set` needs an existing variable (add a fixed helper variable with case_patch).
Derived values follow the effects: a rule that sets a variable changes every derived value that uses it.
""",
    "patch": """\
{"op": "set_variable", "value": {name, description, unit, distribution, source}}
{"op": "set_option" | "set_risk" | "set_criterion", "value": {...}}
{"op": "set_derived", "value": {name, formula, description, unit}}   # named formula, use it anywhere
{"op": "remove_<variable|option|risk|criterion|derived>", "name": ...}
{"op": "set_distribution", "name": ..., "distribution": {"kind": "pert", "low": .., "mode": .., "high": ..}}
{"op": "set_formula", "option": ..., "criterion": <optional>, "formula": ...}
{"op": "set_weight", "criterion": ..., "weight": ...}
{"op": "set_correlation", "a": ..., "b": ..., "rho": ...}     # rho 0 removes
{"op": "set_question" | "add_assumption", "value": ...}
Distribution kinds: fixed(value) normal(mean,std) lognormal(mean,std) uniform(low,high)
  triangular(low,mode,high) pert(low,mode,high) bernoulli(p)
""",
}
