# ModelLattice

Explainable, evidence-backed AI model decision intelligence. Give it a task
and your constraints; get ranked, explainable model recommendations with
cost estimates, fallbacks and links to the evidence behind each answer.

This is the week 1-4 foundation described in
`../ModelLattice_Concept_and_MVP_v0.2.docx` (Section 12, "12-Week Delivery
Plan"): the identity spine, attribute registry, pricing, a first UMNE v0
decision engine, UMFR v0 feedback ingest, and both the REST API and the MCP
server that expose them.

## What's here

```
app/
  models/        SQLAlchemy models: identity spine, UMIM measurements,
                  pricing, UMRG relationships, task profiles/policies,
                  eval runs, UMFR feedback, the recommendation audit log.
  schemas/        Pydantic request/response contracts.
  services/
    cost.py        Cost-per-1k-tasks estimation from current pricing.
    recommender.py UMNE v0: hard filters -> Pareto ranking -> policy order.
  api/            FastAPI routers: catalog, recommend, feedback.
  main.py         App entrypoint.
mcp_server/
  server.py       MCP tools (find_models, recommend_model, compare_models,
                  estimate_cost, get_alternatives, report_outcome), calling
                  the same service layer as the REST API.
app/connectors/
  base.py         Shared pipeline: identity resolution, price history
                  (never overwritten - old row closed, new row opened),
                  evidence-backed attribute measurements, change detection
                  (a model dropped from a run is marked deprecated, not
                  deleted; reappearing reactivates it).
  openrouter.py   Real connector: OpenRouter's public, keyless /models API
                  (~470 offerings across ~60 developers, with live pricing
                  and feature flags). No API key to manage.
  developer_map.py  Id-prefix -> developer display name (governed data).
migrations/       Alembic migrations (Postgres is the system of record).
app/evals/
  tasks/            Real task banks: structured_extraction_v1.py (5 JSON-
                    extraction tasks with gold answers) and code_edit_v1.py
                    (loads 3 genuinely buggy Python files + real pytest
                    tests from app/evals/data/code_edit_v1/).
  scorers.py        Deterministic scoring: JSON schema + field-level F1
                    for extraction; an actual pytest subprocess run
                    (sandboxed temp dir, timeout, isolated Python) for
                    code-edit - never a simulated pass/fail.
  model_client.py   Real OpenRouter chat-completions client. Refuses to
                    run (NoLiveKeyError) rather than reuse an unrelated
                    API key already present in the environment.
  stats.py          Confidence interval over an eval run's scores.
  runner.py         Ties it together: weekly + per-run budget caps,
                    EvalRun/EvalResult writes, aggregate task_quality_score
                    Measurement with CI and S2/S3 maturity.
scripts/
  seed.py           Illustrative dev data: 3 providers, 4 offerings,
                    pricing, one eval per offering, relationships.
  run_connectors.py  CLI to run a connector and print an ingest summary.
  run_eval.py        CLI to run a REAL evaluation against a live offering.
tests/            pytest suite against an in-memory SQLite DB (23 tests).
```

## Quickstart

```bash
docker compose up -d                       # Postgres on localhost:5455
python3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
cp .env.example .env                       # adjust DATABASE_URL if needed

./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe -m scripts.seed

# Real catalog data from a public, keyless API - no credentials needed:
./.venv/Scripts/python.exe -m scripts.run_connectors openrouter

./.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8008
# -> http://127.0.0.1:8008/docs
```

Re-running `scripts.run_connectors` is safe and idempotent: unchanged
offerings/prices/measurements write nothing new, changed prices close the
old `price_point` row and open a new one (full history preserved), and any
offering that disappears from the listing is marked `deprecated` (not
deleted) - reappearing later reactivates it. See `tests/test_connectors.py`
for the behavior this guarantees.

### Running a real evaluation

```bash
# Requires OPENROUTER_API_KEY in .env - the runner refuses to run without
# one rather than fabricate a quality score (see app/evals/model_client.py).
./.venv/Scripts/python.exe -m scripts.run_eval \
  --task-profile structured-extraction --provider-model-name "openai/gpt-6.1-terra"

./.venv/Scripts/python.exe -m scripts.run_eval \
  --task-profile code-edit --provider-model-name "anthropic/claude-fable-5.2"
```

Both task profiles are scored deterministically (no LLM judge, no
subjective "looks right" call): structured-extraction by JSON-schema
validity + field-level F1 against a gold answer, code-edit by actually
running the candidate's corrected file against a real pytest suite in an
isolated temp directory. Every call is weighed against a per-run budget
cap (`EVAL_MAX_RUN_BUDGET_USD`, default $2) and a rolling 7-day cap
(`EVAL_WEEKLY_BUDGET_USD`, default $250) computed from `eval_run.spent_usd`
- the run is refused outright if it could exceed either, before any model
is called. Deliberately never reuses `OPENAI_API_KEY` or any other key
that happens to be set in a shared environment; it looks only for
`OPENROUTER_API_KEY`, since OpenRouter is the one connector this MVP has.

`scripts.run_eval` only works against `OpenRouter`-hosted offerings and
only for `structured-extraction` and `code-edit` (the two deterministic
profiles implemented so far - `rag-qa`, `tool-use-agent` and
`summarization` need an LLM-judge scorer, a week 6 item).

Run the MCP server (stdio) instead of/alongside the REST API:

```bash
./.venv/Scripts/python.exe -m mcp_server.server
```

Run tests (no Postgres/Docker required - uses in-memory SQLite):

```bash
./.venv/Scripts/python.exe -m pytest
```

## Try it

```bash
curl -s -X POST http://127.0.0.1:8008/v1/recommend \
  -H "Content-Type: application/json" \
  -d '{
    "task_profile": "structured-extraction@1",
    "policy": "balanced",
    "constraints": {
      "max_cost_per_1k_tasks_usd": 4.00,
      "data_residency": ["EU"],
      "no_training_on_inputs": true,
      "requires": ["json_schema_output"]
    }
  }'
```

This is the exact example from the concept document (Section 8.2). Against
the seed data it excludes the three ineligible offerings with specific
reasons (cost, residency, missing feature) and returns the one eligible
offering, ranked, with cost, quality and evidence links.

Other endpoints: `GET /v1/models`, `GET /v1/offerings`, `GET /v1/compare`,
`GET /v1/alternatives/{offering_id}`, `POST /v1/estimate-cost`,
`POST /v1/feedback` (requires header `X-API-Key: dev-local-key`, see
`.env.example`).

## Design decisions carried over from the concept doc

- **Unknown is not unsupported.** Every `Measurement` has a `status`
  (`supported|unsupported|unknown|not_evaluated|not_applicable|conflicting`).
  A candidate with no quality measurement on file is still recommended,
  never silently scored zero (see `test_unknown_quality_does_not_exclude_candidate`).
- **Hard constraints are never rescued by a high score.** `_check_hard_constraints`
  runs before any ranking; see `recommender.py`.
- **Forward/reverse relationship profiles are independent columns** on
  `Relationship` - substitution A->B is never assumed to hold B->A.
- **PostgreSQL only, for now** (Decision C10). `app/db.py` tolerates SQLite
  for the test suite, but production config must use `postgresql+psycopg`.
- **Metadata-only feedback by default** (Decision C9). `FeedbackIn` has no
  field for raw prompts/responses.
- **Every recommendation is logged** (`RecommendationLog`), including from
  the MCP server, so bias toward any one customer or provider is auditable.

## What's deliberately NOT here yet

Per the MVP scope in the concept doc (Section 5.2): no survey engine, no
blind pairwise comparisons, no multi-hop path traversal, no hysteresis/
switching-cost modelling, no cross-tenant aggregation, no scheduled/
automated connector or eval runs (run manually or via your own cron for
now), no LLM-judge scorer yet (so `rag-qa`, `tool-use-agent` and
`summarization` are defined in the concept doc but have no task bank
here), no auth beyond a single shared API key, and only one connector
(OpenRouter) rather than direct-from-provider connectors, so no
per-region Deployment rows or training-data-policy evidence yet. These
are the weeks 6-12 and post-MVP items in Section 12 of the concept
document.

No live evaluation has actually been run in this environment - no
OPENROUTER_API_KEY is configured here, and the runner is built to refuse
rather than guess. The task banks, scorers and aggregation are verified
by `tests/test_scorers.py` and `tests/test_eval_runner.py` using a
fixture model client, not a real provider call.
