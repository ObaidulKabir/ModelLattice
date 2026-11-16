"""Deterministic scorers (v0.1 Section 9.1 channel D: "Controlled
comparative evaluation" / v0.2 Section 9: "Deterministic test execution",
"Deterministic comparison with gold labels"). No LLM judge involved - a
candidate completion is scored by running real code or comparing real
values, never by asking another model whether it "looks right".
"""
import dataclasses
import json
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


@dataclasses.dataclass
class ScoreResult:
    score: float  # 0.0-1.0
    passed: bool
    detail: dict


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.DOTALL)
    return match.group(1) if match else text


# ---------------------------------------------------------------------
# structured-extraction: JSON parse + schema check + field-level F1
# ---------------------------------------------------------------------

def _check_type(value, expected_type: str) -> bool:
    if value is None:
        return True  # absence is handled separately via "required"
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "bool":
        return isinstance(value, bool)
    return True


def _values_match(predicted, gold) -> bool:
    if predicted is None or gold is None:
        return predicted == gold
    if isinstance(gold, str):
        return isinstance(predicted, str) and predicted.strip().lower() == gold.strip().lower()
    if isinstance(gold, (int, float)):
        try:
            return abs(float(predicted) - float(gold)) < 0.01
        except (TypeError, ValueError):
            return False
    return predicted == gold


def score_structured_extraction(completion: str, schema: dict, gold: dict) -> ScoreResult:
    raw = _strip_code_fences(completion)
    try:
        predicted = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ScoreResult(score=0.0, passed=False, detail={"error": "invalid_json", "raw": raw[:500]})

    if not isinstance(predicted, dict):
        return ScoreResult(score=0.0, passed=False, detail={"error": "not_a_json_object", "raw": raw[:500]})

    schema_violations = []
    for field, spec in schema.items():
        present = field in predicted and predicted[field] is not None
        if spec.get("required") and not present:
            schema_violations.append(f"missing required field: {field}")
        elif present and not _check_type(predicted[field], spec["type"]):
            schema_violations.append(f"wrong type for {field}: expected {spec['type']}")
    schema_valid = not schema_violations

    tp = fp = fn = 0
    for field in schema:
        gold_val = gold.get(field)
        pred_val = predicted.get(field)
        gold_present = gold_val is not None
        pred_present = field in predicted and pred_val is not None
        if gold_present and pred_present and _values_match(pred_val, gold_val):
            tp += 1
        elif pred_present and (not gold_present or not _values_match(pred_val, gold_val)):
            fp += 1
        elif gold_present and not pred_present:
            fn += 1
        # both absent: true negative, not counted in F1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    return ScoreResult(
        score=f1,
        passed=schema_valid and f1 >= 0.99,
        detail={
            "schema_valid": schema_valid,
            "schema_violations": schema_violations,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "predicted": predicted,
        },
    )


# ---------------------------------------------------------------------
# code-edit: real pytest execution in an isolated subprocess
# ---------------------------------------------------------------------

def score_code_edit(completion: str, test_code: str, scratch_root: Path | None = None, timeout_s: int = 10) -> ScoreResult:
    """Writes the candidate's corrected module and the task's real pytest
    file into a fresh, empty temp directory and actually runs pytest
    against it as a subprocess - genuine dynamic execution, not a
    simulated pass/fail. Untrusted model output: isolated cwd, a short
    timeout, and Python's isolated mode (-I) with no bytecode cache."""
    candidate_code = _strip_code_fences(completion)

    base = scratch_root or Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="modellattice_eval_", dir=str(base)))
    try:
        (run_dir / "solution.py").write_text(candidate_code, encoding="utf-8")
        (run_dir / "test_solution.py").write_text(test_code, encoding="utf-8")

        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "pytest", "-q", "--no-header", "test_solution.py"],
                cwd=str(run_dir),
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return ScoreResult(score=0.0, passed=False, detail={"error": "timeout", "timeout_s": timeout_s})

        passed = proc.returncode == 0
        tail = (proc.stdout or "")[-1500:]
        return ScoreResult(
            score=1.0 if passed else 0.0,
            passed=passed,
            detail={"returncode": proc.returncode, "stdout_tail": tail, "stderr_tail": (proc.stderr or "")[-500:]},
        )
    finally:
        _cleanup(run_dir)


def _cleanup(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------
# tool-use-agent: deterministic check of the actual tool call emitted
# ---------------------------------------------------------------------

_OPERATION_SYNONYMS = {
    "add": {"add", "addition", "+", "sum"},
    "subtract": {"subtract", "subtraction", "-", "minus"},
    "multiply": {"multiply", "multiplication", "*", "x", "times"},
    "divide": {"divide", "division", "/"},
}
_COMMUTATIVE_ARG_PAIRS = {("add", "a", "b"), ("multiply", "a", "b")}


def _arg_matches(key: str, actual, expected, operation: str | None) -> bool:
    if key == "operation":
        syn = _OPERATION_SYNONYMS.get(str(expected).lower(), {str(expected).lower()})
        return str(actual).strip().lower() in syn
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return abs(float(actual) - float(expected)) < 1e-6
        except (TypeError, ValueError):
            return False
    return str(actual).strip().lower() == str(expected).strip().lower()


def score_tool_use(tool_calls: list[dict] | None, expected_tool: str, expected_args: dict) -> ScoreResult:
    if not tool_calls:
        return ScoreResult(score=0.0, passed=False, detail={"error": "no_tool_call_emitted"})

    call = tool_calls[0]  # only the first call matters for these single-step tasks
    if call.get("name") != expected_tool:
        return ScoreResult(
            score=0.0, passed=False,
            detail={"error": "wrong_tool", "called": call.get("name"), "expected": expected_tool},
        )

    actual_args = call.get("arguments") or {}
    operation = actual_args.get("operation")
    matched = sum(
        1 for k, v in expected_args.items() if k in actual_args and _arg_matches(k, actual_args[k], v, operation)
    )

    # Addition/multiplication are commutative - swapped a/b still counts as
    # a full match rather than being penalized for argument order.
    if matched < len(expected_args) and "a" in expected_args and "b" in expected_args:
        if (operation, "a", "b") in _COMMUTATIVE_ARG_PAIRS or any(
            (expected_args.get("operation"), "a", "b") == pair for pair in _COMMUTATIVE_ARG_PAIRS
        ):
            swapped_ok = _arg_matches("a", actual_args.get("a"), expected_args.get("b"), operation) and _arg_matches(
                "b", actual_args.get("b"), expected_args.get("a"), operation
            )
            if swapped_ok:
                matched = len(expected_args)

    score = matched / len(expected_args) if expected_args else 1.0
    return ScoreResult(
        score=score, passed=score >= 0.999,
        detail={"called": call.get("name"), "actual_args": actual_args, "expected_args": expected_args, "matched": matched, "total": len(expected_args)},
    )


# ---------------------------------------------------------------------
# rag-qa / summarization: LLM-judge rubric scoring (the only scorer here
# that is NOT fully deterministic - see runner.py for the cross-provider
# judge requirement, v0.2 Section 9).
# ---------------------------------------------------------------------

JUDGE_SYSTEM_PROMPT = (
    "You are a strict, impartial grading assistant. You will be given a grading "
    "rubric and a candidate answer. Respond with ONLY a single JSON object: "
    '{"score": <number 0.0-1.0>, "reasoning": "<one short sentence>"} - no '
    "markdown fences, no other text."
)


def score_via_judge(judge_completion_text: str) -> ScoreResult:
    """Parses the judge model's own completion (already obtained by the
    caller via a judge ModelClient) into a ScoreResult. Kept separate from
    the HTTP call so this function is itself deterministic and testable
    with a canned judge response."""
    raw = _strip_code_fences(judge_completion_text)
    try:
        parsed = json.loads(raw)
        score = float(parsed["score"])
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        return ScoreResult(score=0.0, passed=False, detail={"error": "invalid_judge_response", "raw": raw[:500]})

    score = max(0.0, min(1.0, score))
    return ScoreResult(score=score, passed=score >= 0.7, detail={"reasoning": parsed.get("reasoning"), "judge_raw": raw[:1000]})
