"""Real task bank for task_profile "structured-extraction@1" (v0.2 Section
9). Each task is a short source text, a simple field schema, and a gold
answer. The scorer (app/evals/scorers.py) never sees the gold answer
except at scoring time, after the candidate model's completion is in
hand - this is an ordinary offline benchmark, not a leak.

`schema` fields use a deliberately small, dependency-free format rather
than full JSON Schema (metadata-driven, v0.1 Section 3): each entry is
{"type": "string"|"number"|"bool", "required": bool}. A real
implementation could swap in `jsonschema` later without changing the
TaskProfile contract.
"""

TASK_PROFILE_KEY = "structured-extraction"
TASK_PROFILE_VERSION = 1

SCHEMA = {
    "vendor": {"type": "string", "required": True},
    "invoice_number": {"type": "string", "required": True},
    "total_amount": {"type": "number", "required": True},
    "currency": {"type": "string", "required": True},
    "due_date": {"type": "string", "required": False},
}

SYSTEM_PROMPT = (
    "You extract structured data from short business documents. "
    "Respond with ONLY a single JSON object matching this schema (no "
    "markdown fences, no commentary): "
    '{"vendor": string, "invoice_number": string, "total_amount": number, '
    '"currency": string, "due_date": string or null}'
)

TASKS = [
    {
        "id": "extract_001",
        "text": (
            "Northwind Supply Co.\nInvoice #NW-10293\n"
            "Amount due: USD 482.50\nPayment due by 2026-12-01.\n"
            "Thank you for your business."
        ),
        "gold": {
            "vendor": "Northwind Supply Co.",
            "invoice_number": "NW-10293",
            "total_amount": 482.50,
            "currency": "USD",
            "due_date": "2026-12-01",
        },
    },
    {
        "id": "extract_002",
        "text": (
            "BILL TO: Acme Corp\nFROM: Blue Ridge Materials\n"
            "Invoice No. BR-77281\nTotal: EUR 1,204.00\n"
            "Please remit within 30 days."
        ),
        "gold": {
            "vendor": "Blue Ridge Materials",
            "invoice_number": "BR-77281",
            "total_amount": 1204.00,
            "currency": "EUR",
            "due_date": None,
        },
    },
    {
        "id": "extract_003",
        "text": (
            "Receipt - Falcon Logistics\nRef: FL-55120-A\n"
            "Charge: 95.00 GBP\nDue on receipt."
        ),
        "gold": {
            "vendor": "Falcon Logistics",
            "invoice_number": "FL-55120-A",
            "total_amount": 95.00,
            "currency": "GBP",
            "due_date": None,
        },
    },
    {
        "id": "extract_004",
        "text": (
            "Statement from Cedar & Pine LLC\nInvoice Number: CP-0042\n"
            "Amount: $3,150.75 USD\nDue date: 2027-01-15"
        ),
        "gold": {
            "vendor": "Cedar & Pine LLC",
            "invoice_number": "CP-0042",
            "total_amount": 3150.75,
            "currency": "USD",
            "due_date": "2027-01-15",
        },
    },
    {
        "id": "extract_005",
        "text": (
            "Harbor Freight Services\nInvoice: HFS-998\n"
            "Total due: 610.20 CAD, payable within 14 days."
        ),
        "gold": {
            "vendor": "Harbor Freight Services",
            "invoice_number": "HFS-998",
            "total_amount": 610.20,
            "currency": "CAD",
            "due_date": None,
        },
    },
]
