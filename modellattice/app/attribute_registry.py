"""The governed UMIM attribute registry (v0.1 Section 3, "metadata-driven
design"): a new attribute is a data row here, not a schema migration.

Single source of truth, used by both scripts/seed.py (fresh setups) and
app/connectors/registry.py (so a connector added after initial seeding -
or a new attribute added to an existing connector - never silently fails
to register its own attribute_definition row).
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.measurement import AttributeDefinition

ATTRIBUTE_DEFINITIONS = [
    # D02 Modality & I/O
    dict(key="supports_vision_input", dimension="D02", label="Accepts image input", value_type="bool"),
    dict(key="supports_audio_input", dimension="D02", label="Accepts audio input", value_type="bool"),
    dict(key="supports_audio_output", dimension="D02", label="Produces audio output", value_type="bool"),
    dict(key="supports_video_input", dimension="D02", label="Accepts video input", value_type="bool"),
    dict(key="max_context_tokens", dimension="D02", label="Maximum context window", value_type="number", unit="tokens"),
    # D03 Cognitive Capabilities
    dict(key="intelligence_index", dimension="D03", label="Relative reasoning/intelligence score (third-party: Artificial Analysis)", value_type="number"),
    # D04 Task Competencies
    dict(key="coding_index", dimension="D04", label="Relative coding competency score (third-party: Artificial Analysis)", value_type="number"),
    # D06 Interaction & Agency
    dict(key="supports_tool_calling", dimension="D06", label="Supports function/tool calling", value_type="bool"),
    dict(key="supports_json_schema_output", dimension="D06", label="Supports constrained JSON schema output", value_type="bool"),
    dict(key="agentic_index", dimension="D06", label="Relative agentic-task competency score (third-party: Artificial Analysis)", value_type="number"),
    # D07 Quality & Accuracy (contextual to a task profile, see Measurement.context)
    dict(key="task_quality_score", dimension="D07", label="Task quality score (0-1, contextual to a task profile)", value_type="number"),
    # D08 Performance & Efficiency
    dict(key="p95_latency_ms", dimension="D08", label="P95 end-to-end latency", value_type="number", unit="ms"),
    # D11 Security & Governance
    dict(key="no_training_on_inputs_attr", dimension="D11", label="Provider confirms no training on customer inputs", value_type="bool"),
]


def ensure_attribute_definitions(db: Session) -> int:
    """Idempotently inserts any attribute_definition row not already on
    file. Returns the count actually created. Cheap enough to call at the
    start of every connector run (app/connectors/registry.py) rather than
    only during initial seeding."""
    existing_keys = set(db.execute(select(AttributeDefinition.key)).scalars().all())
    created = 0
    for attr in ATTRIBUTE_DEFINITIONS:
        if attr["key"] not in existing_keys:
            db.add(AttributeDefinition(**attr))
            created += 1
    if created:
        db.commit()
    return created
