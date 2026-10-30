from __future__ import annotations

import json
import math

import pytest

from variator.llm import ULTRA
from variator.sandboxes import (
    CodegenAttempt,
    OutputValidationError,
    extract_code,
    generate_and_run,
    prompts,
    validate_output,
)

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/work/main.py", line 7, in <module>\n'
    "    margin = revenue / volume\n"
    "ZeroDivisionError: division by zero\n"
)
BROKEN = "import numpy as np\nvolume = 0\nmargin = 1 / volume\n"
FIXED = "import json\nimport numpy as np\njson.dump({}, open('output.json', 'w'))\n"


def fenced(code: str, lang: str = "python") -> str:
    return f"Here is the program.\n\n```{lang}\n{code}```\n"


def good_output(decision, **overrides) -> dict:
    stats = {"mean": 1.0e6, "p5": -2.0e5, "p50": 9.0e5, "p95": 2.1e6, "prob_loss": 0.12}
    options = {o.name: dict(stats) for o in decision.options}
    options.update(overrides)
    return {"n_scenarios": 20000, "options": options, "notes": "copula for correlations"}


# -- code extraction -----------------------------------------------------------


def test_extract_python_block_from_prose():
    assert extract_code(fenced("print(1)\n")) == "print(1)\n"


def test_extract_prefers_python_tag_and_longest_block():
    reply = (
        "```bash\npip install numpy\n```\n"
        "```python\nimport numpy\n```\n"
        "```python\nimport numpy as np\nprint(np.zeros(3))\n```\n"
        '```json\n{"a": 1}\n```'
    )
    assert extract_code(reply) == "import numpy as np\nprint(np.zeros(3))\n"


def test_extract_untagged_block_crlf_and_think():
    reply = "<think>let me plan\n```python\nwrong()\n```</think>\r\nSure:\r\n```\r\nprint('ok')\r\n```\r\n"
    assert extract_code(reply) == "print('ok')\n"


def test_extract_unterminated_block():
    code = "import numpy as np\nx = np.ones(2)\n"
    assert extract_code("```python\n" + code) == code


def test_extract_bare_code_and_failures():
    assert extract_code("import json\nprint(json.dumps({}))") == "import json\nprint(json.dumps({}))\n"
    assert extract_code("I am sorry, I cannot help with that.") is None
    assert extract_code('```json\n{"options": {}}\n```') is None


def test_extract_keeps_inner_backticks_in_strings():
    code = 'doc = "use ``code`` here"\nprint(doc)\n'
    assert extract_code(fenced(code)) == code


# -- output validation ---------------------------------------------------------


def test_validate_output_accepts_complete_output(decision):
    output = validate_output(good_output(decision), decision)
    assert set(output.options) == {o.name for o in decision.options}
    assert output.options["outsource_3pl"].prob_loss == pytest.approx(0.12)


def test_validate_output_rejects_missing_and_unknown_options(decision):
    raw = good_output(decision)
    raw["options"].pop("expand_current")
    raw["options"]["build_moon_base"] = raw["options"]["outsource_3pl"]
    with pytest.raises(OutputValidationError, match="missing options.*expand_current.*unknown options"):
        validate_output(raw, decision)


@pytest.mark.parametrize(
    ("bad_stats", "fragment"),
    [
        ({"mean": 1, "p5": 5, "p50": 3, "p95": 9, "prob_loss": 0.1}, "p5 <= p50 <= p95"),
        ({"mean": 1, "p5": 1, "p50": 2, "p95": 3, "prob_loss": 1.5}, "prob_loss"),
        ({"mean": math.nan, "p5": 1, "p50": 2, "p95": 3, "prob_loss": 0.1}, "mean"),
        ({"mean": 1, "p5": 1, "p95": 3, "prob_loss": 0.1}, "p50"),
    ],
)
def test_validate_output_checks_each_option(decision, bad_stats, fragment):
    raw = json.loads(json.dumps(good_output(decision, second_warehouse=bad_stats)))
    with pytest.raises(OutputValidationError, match=fragment):
        validate_output(raw, decision)


# -- the write -> run -> check -> fix loop -------------------------------------


def test_first_attempt_fails_with_traceback_then_fixed(fake_tf, fake_sbx, decision):
    fake_tf.add_reply(content=fenced(BROKEN)).add_reply(content=fenced(FIXED))
    fake_sbx.add_run(exit_code=1, stderr=TRACEBACK)
    fake_sbx.add_run(stdout="done\n", output=good_output(decision))
    seen: list[CodegenAttempt] = []

    result = generate_and_run(
        fake_tf.client(), fake_sbx.client(), decision, task="Add seasonality.", on_attempt=seen.append
    )

    assert result.success and result.iterations == 2
    assert result.code == FIXED and result.model == ULTRA.model_id
    assert set(result.output.options) == {o.name for o in decision.options}
    first, second = result.attempts
    assert (first.failure, first.exit_code, first.code) == ("exit", 1, BROKEN)
    assert "ZeroDivisionError" in first.stderr and first.operation_id
    assert second.ok and second.output_json == good_output(decision)
    assert seen == result.attempts

    # the traceback went back to the model together with the failing program
    fix_request = fake_tf.requests[1]
    assert fix_request["model"] == ULTRA.model_id
    assert fix_request["messages"][-2] == {"role": "assistant", "content": f"```python\n{BROKEN}```"}
    assert "ZeroDivisionError: division by zero" in fix_request["messages"][-1]["content"]
    assert "exited with status 1" in fix_request["messages"][-1]["content"]

    # every run is offline
    assert [s["networking"] for s in fake_sbx.spawned] == [{"enabled": False}] * 2


def test_gives_up_after_max_iterations(fake_tf, fake_sbx, decision):
    for i in range(3):
        fake_tf.add_reply(content=fenced(f"raise SystemExit({i + 1})\n"))
        fake_sbx.add_run(exit_code=i + 1, stderr=f"error {i + 1}\n")
    result = generate_and_run(fake_tf.client(), fake_sbx.client(), decision, task="x", max_iterations=3)
    assert not result.success and result.output is None and result.aborted is None
    assert [a.exit_code for a in result.attempts] == [1, 2, 3]
    assert result.code == "raise SystemExit(3)\n"
    assert len(fake_tf.requests) == 3 and fake_tf.pending == 0
    assert fake_sbx.pending_runs == 0


def test_schema_error_is_fed_back(fake_tf, fake_sbx, decision):
    incomplete = good_output(decision)
    incomplete["options"].pop("outsource_3pl")
    fake_tf.add_reply(content=fenced(FIXED)).add_reply(content=fenced(FIXED + "# all options\n"))
    fake_sbx.add_run(output=incomplete).add_run(output=good_output(decision))
    result = generate_and_run(fake_tf.client(), fake_sbx.client(), decision, task="x")
    assert result.success and result.attempts[0].failure == "schema"
    feedback = fake_tf.requests[1]["messages"][-1]["content"]
    assert "does not match the required schema" in feedback and "outsource_3pl" in feedback


def test_missing_output_is_fed_back(fake_tf, fake_sbx, decision):
    fake_tf.add_reply(content=fenced("print('no file')\n")).add_reply(content=fenced(FIXED))
    fake_sbx.add_run(stdout="no file\n").add_run(output=good_output(decision))
    result = generate_and_run(fake_tf.client(), fake_sbx.client(), decision, task="x")
    assert result.attempts[0].failure == "output"
    assert "did not write output.json" in fake_tf.requests[1]["messages"][-1]["content"]


def test_timeout_is_fed_back(fake_tf, fake_sbx, decision):
    fake_tf.add_reply(content=fenced("while True: pass\n")).add_reply(content=fenced(FIXED))
    fake_sbx.add_run(exit_code=None, signal=9, timed_out=True).add_run(output=good_output(decision))
    result = generate_and_run(fake_tf.client(), fake_sbx.client(), decision, task="x", timeout_s=45)
    assert result.success and result.attempts[0].failure == "timeout"
    assert "45-second time limit" in fake_tf.requests[1]["messages"][-1]["content"]
    assert fake_sbx.spawned[0]["timeout"] == 45


def test_syntax_error_is_caught_before_the_sandbox(fake_tf, fake_sbx, decision):
    fake_tf.add_reply(content=fenced("def broken(:\n    pass\n")).add_reply(content=fenced(FIXED))
    fake_sbx.add_run(output=good_output(decision))
    result = generate_and_run(fake_tf.client(), fake_sbx.client(), decision, task="x")
    assert result.success and result.attempts[0].failure == "syntax"
    assert len(fake_sbx.spawned) == 1
    assert "SyntaxError" in fake_tf.requests[1]["messages"][-1]["content"]


def test_reply_without_code_is_fed_back(fake_tf, fake_sbx, decision):
    fake_tf.add_reply(content="I would model this with a copula.").add_reply(content=fenced(FIXED))
    fake_sbx.add_run(output=good_output(decision))
    result = generate_and_run(fake_tf.client(), fake_sbx.client(), decision, task="x")
    assert result.attempts[0].failure == "no_code" and result.attempts[0].code is None
    assert fake_tf.requests[1]["messages"][-1]["content"].startswith(
        "Attempt 1 failed: " + prompts.NO_CODE_FAILURE
    )


def test_sandbox_failure_aborts_with_transcript(fake_tf, fake_sbx, decision):
    fake_tf.add_reply(content=fenced(FIXED)).add_reply(content=fenced(FIXED))
    fake_sbx.add_run(status="FAILED", error="no capacity")
    result = generate_and_run(fake_tf.client(), fake_sbx.client(), decision, task="x")
    assert not result.success and "no capacity" in result.aborted
    assert [a.failure for a in result.attempts] == ["sandbox"]
    assert fake_tf.pending == 1  # no second LLM call


def test_prompt_and_inputs_describe_the_decision(fake_tf, fake_sbx, decision):
    fake_tf.add_reply(content=fenced(FIXED))
    fake_sbx.add_run(output=good_output(decision))
    generate_and_run(
        fake_tf.client(),
        fake_sbx.client(),
        decision,
        task="Model a 6-month permit delay.",
        n_scenarios=5000,
        seed=7,
        image="tag:custom:1",
    )
    system, user = fake_tf.requests[0]["messages"]
    assert system == {"role": "system", "content": prompts.CODEGEN_SYSTEM}
    for fragment in ("Model a 6-month permit delay.", "n_scenarios = 5000", "seed = 7", decision.question):
        assert fragment in user["content"]
    for option in decision.options:
        assert f'"{option.name}": {{"mean"' in user["content"]
    assert json.loads(fake_sbx.uploaded("/work/decision.json")) == json.loads(
        decision.model_dump_json(exclude_none=True)
    )
    assert json.loads(fake_sbx.uploaded("/work/config.json")) == {"n_scenarios": 5000, "seed": 7}
    assert fake_sbx.spawned[0]["image"] == "tag:custom:1"


def test_max_iterations_must_be_positive(fake_tf, fake_sbx, decision):
    with pytest.raises(ValueError):
        generate_and_run(fake_tf.client(), fake_sbx.client(), decision, task="x", max_iterations=0)
