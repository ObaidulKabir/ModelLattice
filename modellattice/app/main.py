"""ModelLattice API entrypoint.

Shape per v0.2 Section 6.1: one modular monolith, modules kept behind their
own routers/services so any of them can later be extracted into its own
service without changing the external contracts in app/schemas.
"""
from fastapi import FastAPI

from app.api import catalog, feedback, recommend
from app.config import get_settings

settings = get_settings()

app = FastAPI(
    title="ModelLattice API",
    description="Explainable, evidence-backed AI model decision intelligence.",
    version="0.1.0",
)

app.include_router(catalog.router)
app.include_router(recommend.router)
app.include_router(feedback.router)


@app.get("/health")
def health():
    return {"status": "ok", "environment": settings.environment}
