"""ASGI entrypoint overlay for the idempotent ingest implementation.

The legacy application module owns the full API surface. This module replaces
only POST /ingest so exact active SillyTavern replays can return before DAG,
runtime-cursor, HUD-dirty, and downstream pipeline side effects.
"""

from aios_app.main import app, db
from aios_app.ingest_api import ingest_message
from aios_app.models import IngestIn, IngestOut


# Remove the original POST /ingest route while retaining every other route from
# the established application module. Starlette resolves routes in registration
# order, so leaving the old route registered would keep calling it first.
app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (
        getattr(route, "path", None) == "/ingest"
        and "POST" in (getattr(route, "methods", None) or set())
    )
]


@app.post("/ingest", response_model=IngestOut)
async def ingest(req: IngestIn) -> IngestOut:
    return await ingest_message(db, req)
