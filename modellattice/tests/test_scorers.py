"""Proves the deterministic scorers actually work, not just that they run.

score_code_edit genuinely executes pytest in a subprocess against the
real task files in app/evals/data/code_edit_v1/ - these tests assert that
the ORIGINAL buggy starter fails and a correct fix passes, for all three
tasks, which only holds if the subprocess execution and the bugs
themselves are both real.
"""
import time

from app.evals.scorers import score_code_edit, score_structured_extraction
from app.evals.tasks import code_edit_v1, structured_extraction_v1

GOLDEN_FIXES = {
    "task_001": "def divide(a, b):\n    return a / b\n",
    "task_002": (
        "def is_palindrome(s):\n"
        "    cleaned = ''.join(ch.lower() for ch in s if ch.isalnum())\n"
        "    return cleaned == cleaned[::-1]\n"
    ),
    "task_003": "def fibonacci(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n",
}


def test_code_edit_buggy_starter_fails_its_own_test():
    for task in code_edit_v1.TASKS:
        result = score_code_edit(task["starter_code"], task["test_code"])
        assert result.passed is False, f"{task['id']}: starter should fail (bug is real)"
        assert result.score == 0.0


def test_code_edit_golden_fix_passes():
    for task in code_edit_v1.TASKS:
        fix = GOLDEN_FIXES[task["id"]]
        result = score_code_edit(fix, task["test_code"])
        assert result.passed is True, f"{task['id']}: golden fix should pass: {result.detail}"
        assert result.score == 1.0


def test_code_edit_strips_markdown_fences():
    fix = GOLDEN_FIXES["task_001"]
    fenced = f"```python\n{fix}\n```"
    task = next(t for t in code_edit_v1.TASKS if t["id"] == "task_001")
    result = score_code_edit(fenced, task["test_code"])
    assert result.passed is True


def test_code_edit_timeout_is_enforced():
    task = next(t for t in code_edit_v1.TASKS if t["id"] == "task_001")
    infinite_loop = "def divide(a, b):\n    while True:\n        pass\n"
    started = time.monotonic()
    result = score_code_edit(infinite_loop, task["test_code"], timeout_s=2)
    elapsed = time.monotonic() - started
    assert result.passed is False
    assert result.detail.get("error") == "timeout"
    assert elapsed < 5  # didn't hang


def test_structured_extraction_perfect_match():
    task = structured_extraction_v1.TASKS[0]
    import json

    completion = json.dumps(task["gold"])
    result = score_structured_extraction(completion, structured_extraction_v1.SCHEMA, task["gold"])
    assert result.passed is True
    assert result.score == 1.0
    assert result.detail["schema_valid"] is True


def test_structured_extraction_partial_match_scores_partial_credit():
    gold = {"vendor": "Acme", "invoice_number": "A1", "total_amount": 100.0, "currency": "USD", "due_date": None}
    predicted = {"vendor": "Acme", "invoice_number": "WRONG", "total_amount": 100.0, "currency": "USD"}
    import json

    result = score_structured_extraction(json.dumps(predicted), structured_extraction_v1.SCHEMA, gold)
    assert result.detail["schema_valid"] is True
    assert abs(result.detail["precision"] - 0.75) < 1e-6
    assert abs(result.detail["recall"] - 1.0) < 1e-6
    assert abs(result.score - 0.857142857) < 1e-6
    assert result.passed is False  # f1 below the 0.99 pass threshold


def test_structured_extraction_invalid_json_scores_zero():
    result = score_structured_extraction("not json at all", structured_extraction_v1.SCHEMA, {"vendor": "Acme"})
    assert result.score == 0.0
    assert result.passed is False
    assert result.detail["error"] == "invalid_json"


def test_structured_extraction_non_object_json():
    result = score_structured_extraction("[1, 2, 3]", structured_extraction_v1.SCHEMA, {"vendor": "Acme"})
    assert result.detail["error"] == "not_a_json_object"


def test_structured_extraction_missing_required_field_fails_schema():
    gold = structured_extraction_v1.TASKS[0]["gold"]
    predicted = dict(gold)
    del predicted["vendor"]
    import json

    result = score_structured_extraction(json.dumps(predicted), structured_extraction_v1.SCHEMA, gold)
    assert result.detail["schema_valid"] is False
    assert "missing required field: vendor" in result.detail["schema_violations"]
