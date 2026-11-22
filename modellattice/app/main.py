"""ModelLattice API entrypoint.

Shape per v0.2 Section 6.1: one modular monolith, modules kept behind their
own routers/services so any of them can later be extracted into its own
service without changing the external contracts in app/schemas.
"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import catalog, dashboard, feedback, recommend
from app.config import get_settings
from app.version import get_build_version

settings = get_settings()

app = FastAPI(
    title="ModelLattice API",
    description="Explainable, evidence-backed AI model decision intelligence.",
    version=get_build_version(),
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(catalog.router)
app.include_router(recommend.router)
app.include_router(feedback.router)
app.include_router(dashboard.router)


@app.get("/health")
def health():
    return {"status": "ok", "environment": settings.environment, "build_version": get_build_version()}
