"""Shared column helpers and controlled vocabularies.

These Python enums are the MVP's narrow, stable core (v0.1 Section 3.1).
They are intentionally NOT bound to native database enum types: adding a new
measurement status or evidence type should be a code + data change, never a
migration that locks the schema to today's taxonomy.
"""
import enum
import uuid

from sqlalchemy import String
from sqlalchemy.orm import mapped_column
from sqlalchemy.types import Uuid


def uuid_pk():
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def fk_uuid(nullable: bool = False):
    return mapped_column(Uuid, nullable=nullable)


class ProviderType(str, enum.Enum):
    DEVELOPER = "developer"
    HOST = "host"
    BOTH = "both"


class OfferingLifecycle(str, enum.Enum):
    PREVIEW = "preview"
    GENERALLY_AVAILABLE = "generally_available"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class MeasurementStatus(str, enum.Enum):
    """v0.1 Section 5.1 / v0.2 Section 7: missing evidence is a status, never
    a silent zero score."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    NOT_EVALUATED = "not_evaluated"
    NOT_APPLICABLE = "not_applicable"
    CONFLICTING = "conflicting"


class MaturityLevel(str, enum.Enum):
    """v0.1 Section 5.1 capability maturity scale, S0-S5."""

    S0_UNSUPPORTED = "S0"
    S1_ADVERTISED = "S1"
    S2_VERIFIED = "S2"
    S3_EVALUATED = "S3"
    S4_PRODUCTION_PROVEN = "S4"
    S5_ADVANCED = "S5"


class EvidenceType(str, enum.Enum):
    OFFICIAL_DOC = "official_doc"
    PLATFORM_EVAL = "platform_eval"
    TELEMETRY = "telemetry"
    THIRD_PARTY = "third_party"
    # Computed by the platform itself from other evidence already on file
    # (e.g. a naming-convention heuristic, or a comparison of two
    # measurements) - distinct from PLATFORM_EVAL, which is a model
    # actually being called. Lower default confidence than a real eval.
    DERIVED_INFERENCE = "derived_inference"


class PriceType(str, enum.Enum):
    INPUT_TOKEN = "input_token"
    OUTPUT_TOKEN = "output_token"
    CACHED_INPUT_TOKEN = "cached_input_token"
    REASONING_TOKEN = "reasoning_token"
    IMAGE = "image"
    AUDIO_SECOND = "audio_second"
    FINE_TUNING = "fine_tuning"
    HOSTING = "hosting"


class RelationshipType(str, enum.Enum):
    """v0.1 Section 6.2. Complementarity/compatibility are defined but
    deferred past the MVP (v0.2 Section 5.2)."""

    LINEAGE = "lineage"
    SUBSTITUTION = "substitution"
    SPECIALIZATION = "specialization"
    TIER_UP = "tier_up"
    TIER_DOWN = "tier_down"
    PROVIDER_DEPLOYMENT = "provider_deployment"
    COMPLEMENTARITY = "complementarity"
    COMPATIBILITY = "compatibility"


class SubjectType(str, enum.Enum):
    MODEL = "model"
    OFFERING = "offering"
    DEPLOYMENT = "deployment"


class FeedbackSourceType(str, enum.Enum):
    RUNTIME_TELEMETRY = "runtime_telemetry"
    HUMAN_REVIEW = "human_review"
    SURVEY = "survey"
    CONTROLLED_EVAL = "controlled_eval"
    OTEL_TRACE = "otel_trace"


def short_str(length: int = 64):
    return mapped_column(String(length))
