import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict

from app.schemas.recommend import ConnectionOut


class ProviderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    type: str


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    family: str
    version: str | None
    modalities: list
    developer_id: uuid.UUID | None


class OfferingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    model_id: uuid.UUID
    provider_id: uuid.UUID
    provider_model_name: str
    api_features: dict
    limits: dict
    lifecycle: str

    # denormalized for convenience in list views
    provider_name: str | None = None
    model_family: str | None = None
    model_version: str | None = None
    connection: ConnectionOut | None = None


class PricePointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    price_type: str
    unit: str
    amount: float | None  # null unless state == "known" - never a sentinel standing in for "we don't know"
    state: str  # PriceState: "known" | "variable"
    currency: str
    effective_from: dt.datetime
    effective_to: dt.datetime | None
