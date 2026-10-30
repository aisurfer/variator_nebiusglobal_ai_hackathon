"""Prompts for the write -> run -> check -> fix code generation loop.

The system prompt is stable across calls so Token Factory's implicit prefix
cache can reuse it; per-decision content goes into the user turn.
"""

from __future__ import annotations

CODEGEN_SYSTEM = """\
You are a senior quantitative analyst who writes Monte Carlo simulations in Python.
You write one complete, self-contained program that will run unattended in an
isolated Linux sandbox. The program is your only deliverable.

Hard constraints for the program:
- Python 3.12. Only the standard library and NumPy are installed. Do not import scipy,
  pandas, numba or anything else.
- There is no network access. Do not call input(), open sockets or download anything.
- The working directory contains decision.json (the decision model) and config.json
  ({"n_scenarios": int, "seed": int}). Read both from the current directory.
- Use numpy.random.default_rng(seed) and vectorised NumPy over all scenarios; no
  Python loops over scenarios. Stay well within the time limit given in the task.
- Write the result to output.json in the current directory with json.dump, using
  plain Python floats (no NaN or Infinity), exactly in the required schema.
- Print a short human-readable summary to stdout. Exit with status 0 on success; let
  real errors raise so the traceback is visible.

Reply with a single fenced ```python code block containing the whole program and
nothing else after it.
"""

DECISION_SEMANTICS = """\
How to read decision.json:
- variables[].distribution.kind: fixed(value); normal(mean, std); lognormal(mean, std of
  the variable itself, not of its log); uniform(low, high); triangular(low, mode, high);
  pert(low, mode, high) = beta-PERT with lambda 4; bernoulli(p) -> 0/1.
- options[].formula: arithmetic over variable names giving the metric per scenario.
  min/max of arrays mean elementwise numpy.minimum/numpy.maximum; where = numpy.where;
  clip, exp, log, sqrt, abs are the NumPy versions.
- options[].overrides: option-specific distributions replacing the variable's default.
- correlations: rank correlation rho between two variables (use a Gaussian copula).
- risks: each scenario the event fires with `probability`; when it fires the target
  variable is shifted: multiply -> variable * (1 + magnitude), add -> variable + magnitude,
  magnitude drawn from its distribution. options=null means all options are exposed.
- metric.higher_is_better tells which direction is good; prob_loss is P(metric < 0).
"""

CODEGEN_TASK = """\
Decision question: {question}
Metric: {metric_name} ({metric_unit})

What the simulation must capture beyond the standard model:
{task}

{semantics}
decision.json:
```json
{decision_json}
```

Simulate n_scenarios = {n_scenarios} with seed = {seed} (both also in config.json).
Time limit: the sandbox kills the program after {timeout_s} seconds.

Required output.json (JSON object):
{{
  "n_scenarios": <int>,
  "options": {{
{option_lines}
  }},
  "notes": "<one or two sentences on modelling choices>"
}}
Every option name above must appear exactly once, and no other names. For each option,
p5 <= p50 <= p95 are percentiles of the metric across scenarios, mean is its average,
and prob_loss is the share of scenarios with metric < 0 (between 0 and 1).
"""

OPTION_LINE = (
    '    "{name}": {{"mean": <float>, "p5": <float>, "p50": <float>, "p95": <float>, "prob_loss": <float>}}'
)

CODEGEN_FIX = """\
Attempt {attempt} failed: {failure}

{details}

Fix the program. Keep what already works, address the cause of this failure, and reply
with the complete corrected program in a single ```python code block.
"""

NO_CODE_FAILURE = "your reply contained no Python code block."
