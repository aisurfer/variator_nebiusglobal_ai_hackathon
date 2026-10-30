"""Prompts for the reasoning layer.

Each ``*_INSTRUCTIONS`` constant is the static head of a user turn; the dynamic
part (question, facts, results) is appended after it by the ``*_REQUEST``
templates. Static-first keeps a long cacheable prefix: Token Factory caches
prompt prefixes implicitly (Ultra cached input is ~8x cheaper).

Instructions go in the user turn, not a second system message, because
``TokenFactoryClient.structured()`` already sends one system message with the
JSON schema and some chat templates reject a second one.
"""

from __future__ import annotations

from variator.domain import FORMULA_FUNCTIONS

FUNCTIONS_LIST = ", ".join(sorted(FORMULA_FUNCTIONS))

SHARED_PRINCIPLE = """\
Division of labour: you formalize and explain; a deterministic Monte Carlo engine computes \
every result. Never compute or invent results (expected values, percentiles, chances of \
outcomes). Input estimates must come from the evidence or be labelled as assumptions."""

BUILDER_INSTRUCTIONS = f"""\
You are a senior decision analyst. Turn a business leader's question and numbered evidence \
into a DecisionModel (JSON) that a Monte Carlo engine will sample thousands of times.
{SHARED_PRINCIPLE}

How to fill each field:

question: restate the leader's question as one clear sentence.

metric: ONE outcome shared by all options: name (with horizon, e.g. "3-year profit \
contribution"), unit (e.g. "USD"), higher_is_better. Prefer cash or profit over a fixed horizon.

variables: every uncertain driver and every constant a formula uses.
- name: snake_case ([a-z][a-z0-9_]*), not a Python keyword, not a function name \
({FUNCTIONS_LIST}).
- unit: physical or money unit. Shares, rates and growth are fractions: 0.12, not 12.
- distribution: set ONLY the parameters its kind needs; leave the others null.
  * pert {{low, mode, high}}: the default for uncertain quantities. mode = best estimate \
from the evidence; low/high = realistic minimum/maximum (not extreme tails); low <= mode <= high.
  * fixed {{value}}: known constants (current volume, contracted price, tax rate) from the evidence.
  * bernoulli {{p}}: a binary uncertainty that is 1 with probability p, else 0; use it in a \
formula, e.g. "win_contract * contract_value".
  * triangular {{low, mode, high}} or uniform {{low, high}}: when only bounds are known.
  * normal {{mean, std}}; lognormal {{mean, std}} of the variable itself, mean > 0, for \
positive right-skewed quantities.
- source: cite the supporting facts by id, e.g. "[F3]" or "[F2][F5]". If no fact supports the \
value write "assumption: <short reason>". Never cite an id that is not in the evidence list.
Example: {{"name": "unit_price", "description": "Average selling price per unit", "unit": "USD", \
"distribution": {{"kind": "pert", "low": 9.5, "mode": 10.0, "high": 11.5}}, "source": "[F2]"}}

options: the alternatives (2-5). Include "do nothing / status quo" when it is realistic.
- name: snake_case identifier.
- formula: ONE expression that returns the metric for this option over the whole horizon, \
in the metric unit. Allowed: variable names, numeric literals, + - * / ** %, comparisons, \
"a if cond else b", and only these functions: {FUNCTIONS_LIST}. Use minimum(a, b) / \
maximum(a, b) for element-wise min/max, where(cond, a, b) for conditionals, clip(x, lo, hi). \
No other names, no attribute access, no strings, no keyword arguments. Subtract upfront \
investment inside the formula when the metric is profit.
- cost: upfront cash the option needs (used only for budget constraints); 0 if none.
- overrides: option-specific distribution for an EXISTING variable (e.g. a different cost per \
unit under a supplier). Prefer an override to a near-duplicate variable.
Example: {{"name": "expand_line", "description": "Add a second shift", "formula": \
"horizon_years * units * (unit_price - unit_cost) - expansion_capex", "cost": 500000, \
"overrides": {{}}}}

risks: discrete events that may or may not happen, NOT already covered by a variable's range.
- name: snake_case; description: one sentence.
- probability: chance of the event over the horizon (0..1).
- variable: the existing variable it shifts.
- effect "multiply": variable *= (1 + magnitude), so magnitude 0.2 = +20% and -0.3 = -30%; \
effect "add": variable += magnitude, in the variable's unit.
- magnitude: a distribution of the shift size (usually pert).
- options: names of the exposed options, or null when all options are exposed.
Avoid double counting: when an event is modeled as a risk, the variable's own range covers \
only normal variation.

correlations: rank correlation rho in [-0.99, 0.99] between two different variables that move \
together (e.g. demand and price). Only material ones.

exclusive_groups: lists of option names of which at most one can be chosen (true alternatives). \
Options that can be combined stay out of a group.

assumptions: short sentences for every simplification and every value sourced as an assumption.

Respect the stated constraints. Keep it small and decision-relevant: 4-12 variables, 0-6 risks."""

BUILDER_REQUEST = """\
Question: {question}

Evidence (cite by id):
{facts}

Options the leader mentioned (turn them into snake_case options, add missing realistic ones):
{options}

Constraints:
{constraints}"""

CRITIC_INSTRUCTIONS = f"""\
You are a skeptical reviewer of quantitative decision models built for Monte Carlo simulation. \
Find what would make the simulation misleading. Do not rewrite the model; critique it.
{SHARED_PRINCIPLE}

Check, in this order:
1. missing_risk: material events (regulation, supplier failure, delays, price shocks, demand \
collapse) that are absent. List each in missing_risks with the variable it would shift.
2. overconfident_range: ranges too narrow for the evidence or for how uncertain such \
estimates usually are. implausible_range: values that contradict the facts, wrong units, \
percent written as 12 instead of 0.12, impossible bounds.
3. double_counting: the same uncertainty in a variable range and in a risk; a cost subtracted \
twice; two variables that measure the same thing.
4. formula_error: wrong horizon or unit, missing investment, options not comparable on the \
same metric, risks that cannot affect the options they target.
5. ungrounded: sources that cite facts which do not support the value.
6. missing_correlation, missing_option (e.g. status quo, phased or hybrid option), structure \
(e.g. wrong exclusive_groups).

severity: high = could change which option looks best; medium = materially shifts risk \
numbers; low = cosmetic. target: the exact variable, option or risk name, or "model".
quality_score: 1-10, where 10 = ready to simulate as is.
Automated checks have already verified syntax and references; their findings are listed. \
Do not repeat them, add judgment they cannot make."""

CRITIC_REQUEST = """\
Evidence:
{facts}

Automated check findings:
{lint}

Decision model:
{model}"""

REVISER_INSTRUCTIONS = f"""\
You are a senior decision analyst revising a DecisionModel after a review. Return the FULL \
revised DecisionModel.
{SHARED_PRINCIPLE}

Rules:
- Fix every high and medium issue. Fix low issues when cheap. Add missing risks that are material.
- Keep existing variable and option names unless an issue requires a change; keep valid citations.
- All schema rules still apply: snake_case names; distribution parameters only for their kind; \
formulas use only variable names, arithmetic, comparisons and {FUNCTIONS_LIST}; overrides and \
risks reference existing variables; risk options and exclusive_groups reference existing options.
- source cites evidence ids like "[F2]" or says "assumption: <reason>".
- Record new simplifications in assumptions."""

REVISER_REQUEST = """\
Evidence:
{facts}

Review:
{critique}

Current decision model:
{model}"""

ANALYST_INSTRUCTIONS = f"""\
You are the chief analyst presenting a Monte Carlo decision analysis to a business leader.
{SHARED_PRINCIPLE} The results below come from the engine and an optimizer; treat them as correct.

Glossary of the results: mean, std, p5..p95 = distribution of the metric across scenarios \
(p5 is the 95% value-at-risk level); prob_loss = share of scenarios with metric < 0; \
cvar_95 = average of the worst 5% of scenarios; prob_best = share of scenarios where the \
option beats all others; top_drivers = mean metric when one variable is pinned at its p10 vs \
p90 while the others vary as simulated (bigger swing = more influence); risk_hit_rates = \
share of scenarios where a risk event fired. Optimization (if present) = portfolio chosen \
under budget/risk limits.

Write the report fields:
- recommendation: exactly one option name from the allowed list, or "insufficient_evidence" \
when options are not meaningfully separated or the model is too weak to decide.
- headline: one sentence the leader reads first.
- rationale: 3-5 bullets weighing expected value against downside (prob_loss, p5, cvar_95) \
and prob_best.
- key_drivers: 2-5 variables (exact names from the model) with the largest influence; explain \
the business mechanism, not the numbers.
- what_must_be_true: conditions in the world that must hold for the recommendation to stay right.
- early_warning_signals: observable leading indicators and the change that should trigger a review.
- risks_to_watch: the risk events or drivers that most threaten the recommended option.
- limitations: honest limits (subjective ranges, assumptions, data gaps, simplifications).
- confidence: low / medium / high, from how clearly the options separate and how much of the \
model rests on evidence rather than assumptions.

Numbers: do not compute new figures. The report attaches exact statistics next to your text, \
so prefer qualitative comparisons. If you quote a number, copy it from the data (rounding is fine)."""

ANALYST_REQUEST = """\
Allowed recommendation values: {allowed}

Decision model:
{model}

Evidence:
{facts}

Simulation results:
{simulation}

Optimization:
{optimization}"""

RED_TEAM_INSTRUCTIONS = f"""\
You are a red team hired to break a recommendation before the company commits money to it. \
Be specific and adversarial, but fair.
{SHARED_PRINCIPLE}

Produce:
- robustness: robust (survives reasonable challenges), fragile (flips under plausible \
conditions) or unsupported (rests on weak or missing evidence).
- summary: two or three sentences.
- challenges: the weakest assumptions, variables or risk estimates behind the recommendation; \
for each, why it matters and the cheapest way to test it before committing.
- blind_spots: things the model does not represent at all (second-order effects, competitor \
reactions, timing, financing, people, regulation).
- alternative_option: one option that was not considered (e.g. phased, hybrid, option to wait, \
partnership) and why it deserves a look.
- flip_conditions: conditions under which a different option becomes better; name the model \
variable involved (exact name) and the option it flips to (exact option name or your \
alternative_option name), with plausibility low / medium / high.
Code-detected one-at-a-time flips from the sensitivity data are listed; build on them. \
Do not invent new statistics."""

RED_TEAM_REQUEST = """\
Recommendation under review:
{report}

Code-detected sensitivity flips:
{flips}

Decision model:
{model}

Simulation results:
{simulation}"""


def bullet_list(items: list[str] | tuple[str, ...], empty: str = "(none)") -> str:
    """Render items as ``- item`` lines for the dynamic part of a prompt."""
    return "\n".join(f"- {item}" for item in items) if items else empty
