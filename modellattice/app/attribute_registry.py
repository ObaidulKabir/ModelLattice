"""The governed UMIM attribute registry (v0.1 Section 3, "metadata-driven
design"): a new attribute is a data row here, not a schema migration.

Single source of truth, used by both scripts/seed.py (fresh setups) and
app/connectors/registry.py (so a connector added after initial seeding -
or a new attribute added to an existing connector - never silently fails
to register its own attribute_definition row).
"""
import dataclasses

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.measurement import AttributeDefinition
from app.models.types import SubjectType

ATTRIBUTE_DEFINITIONS = [
    # D01 Identity & Architecture (third-party: Epoch AI's "Notable AI
    # models" dataset - app/connectors/epoch_ai.py)
    dict(key="parameter_count", dimension="D01", label="Parameter count (third-party: Epoch AI)", value_type="number", unit="parameters"),
    dict(key="training_compute_flop", dimension="D01", label="Training compute (third-party: Epoch AI)", value_type="number", unit="FLOP"),
    dict(key="open_weights", dimension="D01", label="Model weights are openly released (third-party: Epoch AI)", value_type="bool"),
    dict(key="model_accessibility", dimension="D01", label="Access model (API access / open weights / hosted-only / unreleased - third-party: Epoch AI)", value_type="enum"),
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


@dataclasses.dataclass(frozen=True)
class RankableDimension:
    """A numeric UMIM attribute that can be used to rank/sort models in
    app/services/ranking.py - deliberately a SUBSET of ATTRIBUTE_DEFINITIONS
    above: boolean/enum attributes (open_weights, model_accessibility,
    supports_*) stay hard-constraint filters only, never a ranking axis,
    since there's no meaningful "better" direction for an enum. Separate
    from AttributeDefinition/ensure_attribute_definitions on purpose - this
    is ranking metadata, not schema data, and is never written to the DB.
    """

    key: str
    subject_type: str  # SubjectType.MODEL.value or SubjectType.OFFERING.value - where the Measurement row actually lives
    higher_is_better: bool
    label: str
    unit: str | None = None
    # "log" for dimensions spanning orders of magnitude (e.g. training
    # compute 1e22-1e26): linear min-max would squash almost every model
    # to ~0. ranking.py applies log10 before normalizing these.
    scale: str = "linear"


# subject_type mirrors exactly where each connector writes the Measurement:
# app/connectors/epoch_ai.py writes parameter_count/training_compute_flop at
# the Model level (true regardless of which host serves it); every other
# dimension here defaults to Offering level via
# app/connectors/base.py::write_measurement_if_changed.
RANKABLE_DIMENSIONS: dict[str, RankableDimension] = {
    "parameter_count": RankableDimension("parameter_count", SubjectType.MODEL.value, True, "Parameter count", "parameters", "log"),
    "training_compute_flop": RankableDimension("training_compute_flop", SubjectType.MODEL.value, True, "Training compute", "FLOP", "log"),
    "max_context_tokens": RankableDimension("max_context_tokens", SubjectType.OFFERING.value, True, "Max context window", "tokens", "log"),
    "intelligence_index": RankableDimension("intelligence_index", SubjectType.OFFERING.value, True, "Intelligence index"),
    "coding_index": RankableDimension("coding_index", SubjectType.OFFERING.value, True, "Coding index"),
    "agentic_index": RankableDimension("agentic_index", SubjectType.OFFERING.value, True, "Agentic index"),
    "task_quality_score": RankableDimension("task_quality_score", SubjectType.OFFERING.value, True, "Task quality score"),
    # The only lower-is-better dimension - app/services/ranking.py must flip
    # the normalization direction for this one, never assume "higher wins".
    "p95_latency_ms": RankableDimension("p95_latency_ms", SubjectType.OFFERING.value, False, "P95 latency", "ms"),
}


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
