"""Real task bank for task_profile "rag-qa@1" (v0.1 Section 9.1 channel D
/ v0.2 Section 9: "Answer correctness and groundedness", "LLM judge,
calibrated against human-labelled samples"). The candidate answers a
question from a supplied passage; a separate judge model (never the
candidate itself - see runner.py) rates correctness against the gold
answer AND groundedness against the passage.
"""

TASK_PROFILE_KEY = "rag-qa"
TASK_PROFILE_VERSION = 1

SYSTEM_PROMPT = (
    "Answer the user's question using ONLY the supplied passage. If the passage "
    "does not contain the answer, say so plainly. Be concise: one or two sentences."
)

TASKS = [
    {
        "id": "ragqa_001",
        "passage": (
            "The Lighthouse Project was founded in 2019 by Priya Nair. It focuses on "
            "distributing water filtration kits to rural clinics. By the end of 2025 "
            "it had delivered kits to 1,140 clinics across 12 countries, and in 2026 "
            "it added a second program providing solar lamps to the same clinics."
        ),
        "question": "In how many countries had the Lighthouse Project delivered kits by the end of 2025?",
        "gold_answer": "12 countries.",
    },
    {
        "id": "ragqa_002",
        "passage": (
            "Mount Esker Pass sits at 3,420 meters and is closed to vehicle traffic "
            "from November through April due to snow. Hikers can still cross on foot "
            "with proper gear during this period, though the ranger station recommends "
            "against it without a guide."
        ),
        "question": "Can cars cross Mount Esker Pass in January?",
        "gold_answer": "No, the pass is closed to vehicle traffic from November through April, which includes January.",
    },
    {
        "id": "ragqa_003",
        "passage": (
            "Orinthal Robotics shipped 4,200 units of its warehouse picking robot in "
            "Q3. That was a 15% increase over Q2. The company's CFO attributed the "
            "growth to a new financing option introduced in July."
        ),
        "question": "What color are Orinthal Robotics' warehouse robots?",
        "gold_answer": "The passage does not say what color the robots are.",
    },
]


def build_judge_prompt(task: dict, candidate_answer: str) -> str:
    return (
        f"Passage:\n{task['passage']}\n\n"
        f"Question: {task['question']}\n"
        f"Reference (gold) answer: {task['gold_answer']}\n\n"
        f"Candidate answer to grade: {candidate_answer}\n\n"
        "Grading rubric: score 1.0 if the candidate answer is factually correct "
        "relative to the reference answer AND is fully supported by the passage "
        "(no claims beyond what the passage states). Score 0.0 if it contradicts "
        "the reference answer, invents information not in the passage, or fails "
        "to recognize that the passage lacks the answer when the reference says so. "
        "Partial credit (e.g. 0.5) for an answer that is correct but adds unsupported "
        "detail, or is vague but not wrong."
    )
