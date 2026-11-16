"""Real task bank for task_profile "summarization@1" (v0.2 Section 9:
rubric score covering coverage, faithfulness, concision; "LLM judge with
rubric, calibrated against humans"). Reference-free: there is no single
correct summary, so the judge grades the candidate's summary against the
source document directly, not against a gold summary.
"""

TASK_PROFILE_KEY = "summarization"
TASK_PROFILE_VERSION = 1

SYSTEM_PROMPT = "Summarize the following document in {max_words} words or fewer."

TASKS = [
    {
        "id": "summ_001",
        "max_words": 40,
        "document": (
            "The city council voted 6-2 on Tuesday to approve a new bus rapid transit "
            "line connecting the eastern suburbs to downtown. Construction is expected "
            "to begin in March and take 18 months. The project will cost an estimated "
            "$140 million, funded mostly through a federal transit grant, with the "
            "remainder coming from a small increase in the local vehicle registration fee. "
            "Supporters say the line will cut commute times by up to 25 minutes during "
            "peak hours. Two council members who voted against the measure cited concerns "
            "about construction disruption to local businesses along the planned route."
        ),
    },
    {
        "id": "summ_002",
        "max_words": 35,
        "document": (
            "A new study tracking 2,400 participants over eight years found that adults "
            "who walked at least 7,000 steps a day had a 50% lower risk of early death "
            "compared to those who walked fewer than 4,000 steps daily. The benefit "
            "leveled off beyond about 10,000 steps, suggesting there is no additional "
            "gain from walking much further. Researchers noted the study was observational "
            "and could not prove walking alone caused the lower risk, since more active "
            "participants may also have had other healthier habits."
        ),
    },
    {
        "id": "summ_003",
        "max_words": 30,
        "document": (
            "Harbor Foods recalled three batches of its frozen vegetable medley after "
            "routine testing found possible listeria contamination. No illnesses have "
            "been reported. The affected batches were sold in supermarkets across four "
            "states between September 1 and September 20. Customers who bought the "
            "product are advised to check the lot number printed on the bag and, if it "
            "matches, discard it or return it for a full refund."
        ),
    },
]


def build_judge_prompt(task: dict, candidate_summary: str) -> str:
    word_count = len(candidate_summary.split())
    return (
        f"Source document:\n{task['document']}\n\n"
        f"Word limit: {task['max_words']} words. Candidate summary word count: {word_count}.\n\n"
        f"Candidate summary to grade: {candidate_summary}\n\n"
        "Grading rubric (average these three, each 0.0-1.0, into one overall score):\n"
        "- Coverage: does it include the document's main points?\n"
        "- Faithfulness: does it avoid claims not supported by the document?\n"
        "- Concision: does it stay within the word limit and avoid padding "
        "(score 0.0 for concision if it clearly exceeds the word limit)?"
    )
