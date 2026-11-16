"""Real task bank for task_profile "code-edit@1" (v0.2 Section 9): each
task is a genuinely buggy small Python module plus a real pytest test
file that fails against the buggy version and passes against a correct
fix. The candidate model is asked to return the corrected file; the
scorer actually executes pytest against it in a subprocess (see
app/evals/scorers.py::score_code_edit) - "Deterministic test execution"
per the concept document, not a simulated score.
"""
from pathlib import Path

TASK_PROFILE_KEY = "code-edit"
TASK_PROFILE_VERSION = 1

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "code_edit_v1"

SYSTEM_PROMPT = (
    "You fix bugs in small Python files. You will be given a buggy file "
    "and a short description of the expected behavior. Respond with ONLY "
    "the complete corrected file content as plain Python source - no "
    "markdown code fences, no explanation, no extra text."
)

_TASK_DESCRIPTIONS = {
    "task_001": "The divide(a, b) function should return a / b, and should "
    "raise ZeroDivisionError when b is 0.",
    "task_002": "The is_palindrome(s) function should return True if s reads "
    "the same forwards and backwards, ignoring case, spaces and punctuation.",
    "task_003": "The fibonacci(n) function should return the n-th Fibonacci "
    "number (0-indexed: fibonacci(0) == 0, fibonacci(1) == 1).",
}


def _load_tasks() -> list[dict]:
    tasks = []
    for task_id, description in _TASK_DESCRIPTIONS.items():
        starter = (DATA_DIR / f"{task_id}_starter.py").read_text(encoding="utf-8")
        test_code = (DATA_DIR / f"{task_id}_test.py").read_text(encoding="utf-8")
        tasks.append(
            {
                "id": task_id,
                "description": description,
                "starter_code": starter,
                "test_code": test_code,
                "prompt": f"{description}\n\nFile to fix:\n\n{starter}",
            }
        )
    return tasks


TASKS = _load_tasks()
