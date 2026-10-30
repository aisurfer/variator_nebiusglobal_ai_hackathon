import numpy as np
import pytest

from variator.domain import FORMULA_FUNCTIONS
from variator.simulation import FormulaError, compile_formula
from variator.simulation.formula import _ARITY, _FUNCTIONS

A = np.array([-2.0, -0.5, 0.0, 1.5, 3.0])
B = np.array([1.0, 2.0, -3.0, 0.5, 4.0])
ENV = {"a": A, "b": B}


def run(formula: str, env: dict | None = None) -> np.ndarray:
    return compile_formula(formula).evaluate(env or ENV, np, size=len(A))


def test_compiler_implements_exactly_the_whitelisted_functions():
    assert set(_ARITY) == FORMULA_FUNCTIONS
    assert set(_FUNCTIONS) == FORMULA_FUNCTIONS


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("a + b", A + B),
        ("a - b", A - B),
        ("a * b", A * B),
        ("a / b", A / B),
        ("b ** 2", B**2),
        ("2 ** b", 2.0**B),
        ("a % b", A % B),
        ("a // b", A // B),
        ("-a", -A),
        ("+a", A),
        ("-(a + 1) * 3", -(A + 1) * 3),
        ("3", np.full(5, 3.0)),
    ],
)
def test_arithmetic(formula, expected):
    np.testing.assert_allclose(run(formula), expected)


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("a > b", A > B),
        ("a >= 0", A >= 0),
        ("a < b", A < B),
        ("a <= 0", A <= 0),
        ("a == 0", A == 0),
        ("a != 0", A != 0),
        ("-1 < a < 2", (A > -1) & (A < 2)),
        ("a > 0 and b > 0", (A > 0) & (B > 0)),
        ("a > 0 or b > 0", (A > 0) | (B > 0)),
        ("a > 2 or b > 3 or a == 0", (A > 2) | (B > 3) | (A == 0)),
    ],
)
def test_comparisons_and_logic_give_zero_one(formula, expected):
    np.testing.assert_array_equal(run(formula), expected.astype(float))


def test_booleans_count_as_numbers_in_arithmetic():
    # NumPy would compute bool + bool as logical OR; formulas must count instead.
    np.testing.assert_array_equal(run("(a < 5) + (b < 5)"), np.full(5, 2.0))
    np.testing.assert_array_equal(run("-(a > 0)"), -(A > 0).astype(float))
    np.testing.assert_allclose(run("1000 * (a > 0) - b"), 1000 * (A > 0) - B)


def test_conditional_expression_is_where():
    np.testing.assert_allclose(run("a if a > b else b"), np.where(A > B, A, B))
    # Both branches are evaluated; a division by zero in the unused branch is harmless.
    np.testing.assert_allclose(run("1 / a if a != 0 else 0"), np.where(A != 0, 1 / np.where(A == 0, 1, A), 0))


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("min(a, b)", np.minimum(A, B)),
        ("max(a, b, 2)", np.maximum(np.maximum(A, B), 2)),
        ("min(a, b, 0, 1)", np.minimum.reduce([A, B, np.zeros(5), np.ones(5)])),
        ("minimum(a, b)", np.minimum(A, B)),
        ("maximum(a, 0)", np.maximum(A, 0)),
        ("abs(a)", np.abs(A)),
        ("exp(a)", np.exp(A)),
        ("log(abs(b))", np.log(np.abs(B))),
        ("sqrt(abs(a))", np.sqrt(np.abs(A))),
        ("clip(a, -1, 2)", np.clip(A, -1, 2)),
        ("clip(a, b, 10)", np.minimum(np.maximum(A, B), 10)),
        ("where(a > 0, a, b)", np.where(A > 0, A, B)),
        ("where(a, 1, 2)", np.where(A != 0, 1.0, 2.0)),
    ],
)
def test_functions(formula, expected):
    np.testing.assert_allclose(run(formula), expected)


def test_scalars_broadcast_against_arrays_and_constants():
    np.testing.assert_allclose(run("exp(0) + a"), A + 1)
    out = compile_formula("2 * 3").evaluate({}, np, size=4)
    np.testing.assert_array_equal(out, np.full(4, 6.0))
    assert out.dtype == np.float64


def test_names_are_reported():
    assert compile_formula("min(a, b) + a * 2").names == frozenset({"a", "b"})


@pytest.mark.parametrize(
    "formula",
    [
        "__import__('os').system('echo hacked')",
        "open('/etc/passwd')",
        "a.real",
        "a[0]",
        "lambda: 1",
        "(lambda x: x)(a)",
        "[a, b]",
        "(a, b)",
        "'text'",
        "not a",
        "a if b",
        "a; b",
        "eval('1')",
        "min(a, b, key=abs)",
        "min",
        "sum(a)",
        "a := 1",
        "x" * 6000,
        "-" * 2000 + "a",
        "abs(" * 150 + "a" + ")" * 150,
        "a" + " + a" * 300,
    ],
)
def test_rejects_unsafe_or_invalid_formulas(formula):
    with pytest.raises(FormulaError):
        compile_formula(formula)


@pytest.mark.parametrize(
    "formula",
    ["min(a)", "max(a)", "minimum(a, b, 1)", "abs(a, b)", "clip(a, 1)", "where(a, b)", "exp()", "sqrt(a, 2)"],
)
def test_rejects_wrong_arity(formula):
    with pytest.raises(FormulaError, match="arguments"):
        compile_formula(formula)


def test_missing_variable_is_a_clear_error():
    with pytest.raises(FormulaError, match="needs values"):
        compile_formula("a + c").evaluate(ENV, np)


def test_invalid_math_gives_nan_or_inf_without_raising():
    out = run("log(a) + sqrt(a)")
    assert np.isnan(out[0]) and np.isinf(out[2]) and np.isfinite(out[3])
    assert np.isinf(run("1 / a")[2])
    assert np.isnan(run("a % 0")).all()


def test_constant_overflow_becomes_inf_instead_of_hanging():
    out = compile_formula("9 ** 9 ** 9").evaluate({}, np, size=1)
    assert np.isinf(out[0])


def test_redundant_parentheses_are_fine():
    np.testing.assert_allclose(run("(" * 50 + "a" + ")" * 50), A)
