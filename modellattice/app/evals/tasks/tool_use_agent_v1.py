"""Real task bank for task_profile "tool-use-agent@1" (v0.1 Section 9.1 /
v0.2 Section 9: "Deterministic environment checks"). No LLM judge: the
candidate is given a real OpenAI-compatible function-calling `tools`
schema and a natural-language request, and is scored by comparing the
FIRST tool call it actually emits against the expected call - function
name and arguments - via app/evals/scorers.py::score_tool_use.
"""

TASK_PROFILE_KEY = "tool-use-agent"
TASK_PROFILE_VERSION = 1

SYSTEM_PROMPT = (
    "You are an assistant with access to tools. For the user's request, call "
    "exactly the one tool needed to fulfill it. Do not explain, just call the tool."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "City name"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Perform a basic arithmetic operation on two numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                    "operation": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]},
                },
                "required": ["a", "b", "operation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convert_currency",
            "description": "Convert an amount from one currency to another.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number"},
                    "from_currency": {"type": "string", "description": "3-letter currency code"},
                    "to_currency": {"type": "string", "description": "3-letter currency code"},
                },
                "required": ["amount", "from_currency", "to_currency"],
            },
        },
    },
]

TASKS = [
    {
        "id": "tool_001",
        "prompt": "What's the weather like in Paris right now?",
        "expected_tool": "get_weather",
        "expected_args": {"city": "Paris"},
    },
    {
        "id": "tool_002",
        "prompt": "What is 23 times 47?",
        "expected_tool": "calculate",
        "expected_args": {"a": 23, "b": 47, "operation": "multiply"},
    },
    {
        "id": "tool_003",
        "prompt": "Convert 100 US dollars to euros.",
        "expected_tool": "convert_currency",
        "expected_args": {"amount": 100, "from_currency": "USD", "to_currency": "EUR"},
    },
]
