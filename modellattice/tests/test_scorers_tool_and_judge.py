"""Tests for the remaining three task profiles' scorers: tool-use-agent
(fully deterministic, no judge) and the judge-response parser shared by
rag-qa and summarization.
"""
from app.evals.scorers import score_tool_use, score_via_judge
from app.evals.tasks import tool_use_agent_v1


def test_tool_use_exact_match_scores_full():
    task = tool_use_agent_v1.TASKS[0]  # get_weather(city="Paris")
    tool_calls = [{"name": "get_weather", "arguments": {"city": "Paris"}}]
    result = score_tool_use(tool_calls, task["expected_tool"], task["expected_args"])
    assert result.score == 1.0
    assert result.passed is True


def test_tool_use_wrong_tool_scores_zero():
    task = tool_use_agent_v1.TASKS[0]
    tool_calls = [{"name": "calculate", "arguments": {"a": 1, "b": 2, "operation": "add"}}]
    result = score_tool_use(tool_calls, task["expected_tool"], task["expected_args"])
    assert result.score == 0.0
    assert result.detail["error"] == "wrong_tool"


def test_tool_use_no_call_emitted_scores_zero():
    task = tool_use_agent_v1.TASKS[0]
    result = score_tool_use(None, task["expected_tool"], task["expected_args"])
    assert result.score == 0.0
    assert result.detail["error"] == "no_tool_call_emitted"


def test_tool_use_operation_synonym_accepted():
    task = tool_use_agent_v1.TASKS[1]  # calculate(a=23, b=47, operation="multiply")
    tool_calls = [{"name": "calculate", "arguments": {"a": 23, "b": 47, "operation": "*"}}]
    result = score_tool_use(tool_calls, task["expected_tool"], task["expected_args"])
    assert result.score == 1.0


def test_tool_use_commutative_args_swapped_still_matches():
    task = tool_use_agent_v1.TASKS[1]
    tool_calls = [{"name": "calculate", "arguments": {"a": 47, "b": 23, "operation": "multiply"}}]
    result = score_tool_use(tool_calls, task["expected_tool"], task["expected_args"])
    assert result.score == 1.0


def test_tool_use_partial_argument_match_gives_partial_credit():
    task = tool_use_agent_v1.TASKS[2]  # convert_currency(amount=100, from=USD, to=EUR)
    tool_calls = [{"name": "convert_currency", "arguments": {"amount": 100, "from_currency": "USD", "to_currency": "GBP"}}]
    result = score_tool_use(tool_calls, task["expected_tool"], task["expected_args"])
    assert 0.0 < result.score < 1.0
    assert result.detail["matched"] == 2
    assert result.detail["total"] == 3


def test_judge_response_parsed_correctly():
    result = score_via_judge('{"score": 0.85, "reasoning": "mostly correct"}')
    assert result.score == 0.85
    assert result.passed is True
    assert result.detail["reasoning"] == "mostly correct"


def test_judge_response_strips_markdown_fences():
    result = score_via_judge('```json\n{"score": 1.0, "reasoning": "perfect"}\n```')
    assert result.score == 1.0


def test_judge_response_clamped_to_valid_range():
    result = score_via_judge('{"score": 1.5, "reasoning": "overconfident"}')
    assert result.score == 1.0


def test_judge_response_invalid_json_scores_zero():
    result = score_via_judge("not json")
    assert result.score == 0.0
    assert result.detail["error"] == "invalid_judge_response"
