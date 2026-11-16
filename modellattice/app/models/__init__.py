"""SQLAlchemy models for the ModelLattice identity spine, evidence, pricing,
relationships, decisions, evaluations and feedback (UMFR v0).

Table layout follows v0.2 Section 7 (Core Data Model, MVP) of the concept
document. Status/type fields are plain strings validated by Python enums at
the application layer rather than native DB enum types, in keeping with the
v0.1 doctrine of metadata-driven, evolvable taxonomies (Section 3): adding a
new evidence type or relationship kind should not require a migration.
"""
from app.models.catalog import Deployment, Model, Offering, Provider
from app.models.decision import Policy, RecommendationLog, TaskProfile
from app.models.evaluation import EvalResult, EvalRun
from app.models.feedback import FeedbackEvent
from app.models.measurement import AttributeDefinition, EvidenceSource, Measurement
from app.models.pricing import PricePoint
from app.models.relationship import Relationship

__all__ = [
    "Provider",
    "Model",
    "Offering",
    "Deployment",
    "AttributeDefinition",
    "Measurement",
    "EvidenceSource",
    "PricePoint",
    "Relationship",
    "TaskProfile",
    "Policy",
    "RecommendationLog",
    "EvalRun",
    "EvalResult",
    "FeedbackEvent",
]
