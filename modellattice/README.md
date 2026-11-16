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
                  estimate_cost, get_alternatives, report_outcome,
                  check_regression), calling the same service layer as
                  the REST API.
app/monitoring/regression.py  Latency and error-rate regression
  detection (v0.2 Section 12, week 10) from two real evidence sources
  (eval history, UMFR feedback), kept as independent axes - a judge
  scoring a candidate poorly is a quality result, never counted as a
  reliability error. GET /v1/offerings/{id}/regression. Returns
  insufficient_data rather than a verdict when either window is too thin.
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
  openai_pricing.py  Direct-from-OpenAI pricing - a real web source, not
                    an API: fetches https://developers.openai.com/api/docs/pricing
                    directly over plain HTTP (the pricing table is
                    embedded as JSON inside server-rendered HTML, no
                    browser needed) and cross-checks it against
                    OpenRouter's resale pricing for the same models.
                    Only parses the one row shape ([input, cached,
                    output]) empirically confirmed against known
                    pricing; skips richer rows rather than guess their
                    column order. Never invents a Model from a raw
                    OpenAI slug - only attaches a new Offering to a
                    Model it can already find via the existing
                    "openai/{slug}" Offering from the openrouter
                    connector, so it can't repeat the free/batch
                    duplicate-identity bug fixed earlier in this
                    project. An unmatched slug is skipped and counted,
                    not guessed.
app/relationships/infer.py  UMRG v0 relationship inference: naming-
  convention tiers (Pro/Mini/Nano/Lite suffix, Anthropic's Haiku/Sonnet/
  Opus line) and evidence-derived substitution (two offerings evaluated
  on the same task profile with close scores). Both upsert by
  (type, source_id, target_id), so re-runs update rather than duplicate.
  Deliberately does NOT invent cross-developer "A substitutes B" claims
  with no evaluation behind them.
app/services/alternatives.py  Bidirectional relationship traversal
  (v0.1 Section 6.3), shared by GET /v1/alternatives and the MCP
  get_alternatives tool - an edge A->B is traversable from B's side too,
  using the independently-worded reverse_profile.
scripts/derive_relationships.py  CLI to run both inference methods.
scripts/repair_model_identity.py  One-time (idempotent) cleanup for a real
  data bug found after the first live connector runs: OpenRouter's
  ":free"/":batch" commercial-tier variants (100 of 468 offerings) had
  each gotten their OWN Model row instead of being a second Offering of
  the existing Model - see the file's docstring and
  tests/test_openrouter_identity.py. Fixed going forward in
  app/connectors/openrouter.py; this script merges what was already
  ingested before the fix (run once if you seeded a catalog before this
  commit; a clean run_connectors afterward creates no duplicates).
migrations/       Alembic migrations (Postgres is the system of record).
app/evals/
  tasks/            Real task banks, one module per profile:
                    structured_extraction_v1.py (5 JSON-extraction tasks),
                    code_edit_v1.py (3 genuinely buggy Python files + real
                    pytest tests from app/evals/data/code_edit_v1/),
                    tool_use_agent_v1.py (3 function-calling tasks with a
                    real OpenAI-compatible `tools` schema),
                    rag_qa_v1.py / summarization_v1.py (judge-scored;
                    each also builds its own judge rubric prompt).
  scorers.py        Deterministic scoring for the first three profiles:
                    JSON schema + field-level F1, an actual pytest
                    subprocess run (sandboxed temp dir, timeout, isolated
                    Python), and an exact/synonym/commutative-aware check
                    of the real function call emitted. Plus
                    score_via_judge() for the other two, which only
                    parses an already-obtained judge completion - the
                    HTTP call itself lives in model_client.py.
  model_client.py   Real OpenRouter chat-completions client (text and,
                    for tool-use-agent, function-calling). Refuses to run
                    (NoLiveKeyError) rather than reuse an unrelated API
                    key already present in the environment.
  stats.py          Confidence interval over an eval run's scores.
  runner.py         Ties it together per profile kind (text/code/tools/
                    judge): weekly + per-run budget caps (covering BOTH
                    the candidate's and, where used, the judge's call
                    cost), EvalRun/EvalResult writes, aggregate
                    task_quality_score Measurement with CI and S2/S3
                    maturity. Raises JudgeRequiredError rather than let a
                    judge-scored profile fall back to self-grading.
app/connectors/registry.py  The one list of registered connectors, and
  run_connector()/run_all_connectors() - fetch+ingest with the error
  captured in the result rather than raised, so one connector's failure
  (network, provider outage, a changed page format) never blocks the
  others. Shared by the CLI and the scheduler below; adding a new
  connector (e.g. a future direct-from-provider one) is one dict entry.
app/scheduler.py  Periodic refresh: runs every registered connector,
  then re-derives relationships, on a fixed interval
  (CONNECTOR_REFRESH_INTERVAL_HOURS, default 6). Deliberately does NOT
  schedule the evaluation runner - that spends real money per call and
  stays a deliberate, explicit action with its own budget caps, never
  something a timer triggers unattended.
scripts/
  seed.py           Illustrative dev data: 3 providers, 4 offerings,
                    pricing, one eval per offering, relationships.
  run_connectors.py  CLI to run a connector once and print an ingest summary.
  scheduler_daemon.py  Long-running process wrapping app/scheduler.py -
                    run it as its own service, separate from the web app.
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

# A second, independent real source: OpenAI's own pricing page, fetched
# directly (not via OpenRouter) and cross-checked against it:
./.venv/Scripts/python.exe -m scripts.run_connectors openai_pricing

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

`scripts.run_eval` only works against `OpenRouter`-hosted offerings. All
five task profiles are implemented:

| Profile | Scoring | Needs a judge? |
|---|---|---|
| `structured-extraction` | JSON schema + field-level F1 | No |
| `code-edit` | Real pytest subprocess execution | No |
| `tool-use-agent` | Deterministic check of the actual function call emitted (OpenAI-compatible `tools`) | No |
| `rag-qa` | Correctness + groundedness | Yes - a separate model, never the candidate |
| `summarization` | Coverage/faithfulness/concision rubric | Yes - a separate model, never the candidate |

`rag-qa` and `summarization` require `--judge-provider-model-name` (or
`--judge-offering`): a second offering, in keeping with v0.2 Section 9
("judge models come from a different provider than the candidate where
possible"). `run_evaluation()` refuses with `JudgeRequiredError` if a
judge-scored profile is requested without one - it never lets a model
grade itself.

All five were run live against real free-tier OpenRouter models while
building this (not just the fixture-client tests): `tool-use-agent`
emitted exactly the expected function call and arguments; `rag-qa`'s
candidate answered all three questions correctly and the judge's
reasoning matched; `code-edit`/`structured-extraction` results are in
the PR history. Several free-tier `429`/`403` responses were hit along
the way - all correctly caught and recorded as evidence rather than
crashing or being misread as a model failure.

**A real limitation found live, not a bug**: one reasoning-enabled free
model returned an EMPTY summarization answer after 7-10 seconds of
latency - it spent its entire `max_tokens` budget on internal reasoning
before ever emitting visible text. The judge correctly still scored it
(no unsupported claims, trivially concise, but zero coverage) rather
than erroring. Worth knowing before trusting a low score from a
reasoning-capable model: raising `max_tokens` or disabling that model's
reasoning mode (where the provider supports it) is the fix, not yet
wired into `model_client.py`.

### Keeping the catalog fresh automatically

```bash
./.venv/Scripts/python.exe -m scripts.scheduler_daemon
```

Runs until stopped (Ctrl+C): refreshes every registered connector, then
re-derives relationships, every `CONNECTOR_REFRESH_INTERVAL_HOURS`
(default 6; set in `.env`). Runs once immediately on start by default
(`SCHEDULER_RUN_IMMEDIATELY_ON_START=false` to disable that). One
connector failing - a network error, a provider outage, a page that
changed shape - is logged and skipped; it never stops the others or
crashes the process. Verified live: a real run against the current
OpenRouter catalog picked up a genuine price change since the last
manual run and logged it correctly (see git history).

This is a separate, long-running process from the web app (`app.main`)
on purpose - so scaling API workers never risks the same refresh job
firing twice at once. For production, run it as its own systemd unit,
Windows Task Scheduler entry, or container; not built here, since this
MVP runs everything from one dev machine.

**Deliberately excluded**: the evaluation runner. It spends real money
per call, so it stays `scripts/run_eval.py` - an explicit, one-at-a-time
action with its own budget caps, never something a timer fires on its
own. Scheduling it would need its own deliberate design (which
offerings/profiles to prioritize out of hundreds, how to bound total
spend across a whole run) - a real follow-up, not bundled in here.

### Checking for regressions

```bash
curl "http://127.0.0.1:8008/v1/offerings/<id>/regression?task_profile=structured-extraction@1&window_minutes=60"
```

Compares the most recent window of real evidence (eval history and/or
UMFR feedback, `source=eval|feedback|both`) against the window
immediately before it, on two independent axes: mean latency (D08) and
error rate (D09 - an operational failure, never a low quality score).
With fewer than 3 real samples in either window it reports
`insufficient_data: true` rather than asserting a regression or its
absence. Verified against the real nemotron-3.5-lightning:free eval
history from this session's live runs (see git history) - a genuine,
correctly-computed result once the comparison window was positioned
over the actual data.

### Deriving relationships

```bash
./.venv/Scripts/python.exe -m scripts.derive_relationships
```

Run this after the connector (new offerings may complete a tier family)
or after an eval run (new quality measurements may reveal a close-score
pair). Against the real catalog this currently writes real tier edges
(e.g. GPT-5 <-> GPT-5 Mini <-> GPT-5 Nano, GPT-5 <-> GPT-5 Pro, Claude
Haiku <-> Sonnet <-> Opus per version) at confidence 0.6 (a naming
heuristic, not a provider confirmation), plus substitution edges wherever
two offerings have real measured scores within 0.10 of each other on the
same task profile, at a confidence that scales with how close the scores
are and how much sample size backs them. `GET /v1/alternatives/{id}`
(and the MCP `get_alternatives` tool) surface these from either side of
the edge correctly - the direction and wording differ (forward vs
reverse), never assumed symmetric.

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
switching-cost modelling, no cross-tenant aggregation, no scheduled
evaluations (deliberate - see "Keeping the catalog fresh automatically"
above), no auth beyond a single shared API key, and still no per-region
Deployment rows or training-data-policy evidence for any real offering
(neither OpenRouter's listing nor OpenAI's pricing page publishes
either). These are the weeks 9-12 and post-MVP items in Section 12 of
the concept document.

No live evaluation has actually been run in this environment - no
OPENROUTER_API_KEY is configured here, and the runner is built to refuse
rather than guess. The task banks, scorers and aggregation are verified
by `tests/test_scorers.py` and `tests/test_eval_runner.py` using a
fixture model client, not a real provider call.
