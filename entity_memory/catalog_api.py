"""Operator inspection endpoints; no projection writes or inference on GET."""

from uuid import UUID

from aios_app.entity_memory.catalog import catalog_trig
from aios_app.entity_memory.catalog_service import EntityCatalogService
from fastapi import APIRouter, HTTPException, Query, Response


def catalog_router(db):
    router = APIRouter()

    async def respond(read, format):
        try:
            catalog = await read
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=503, detail="Catalog read timed out; state is unknown"
            ) from exc
        if format == "trig":
            return Response(catalog_trig(catalog), media_type="application/trig")
        return catalog

    @router.get("/world/{world_id}/entity-catalog")
    async def world_catalog(
        world_id: UUID,
        timeline_id: UUID,
        head_node_id: UUID,
        format: str = Query(default="json", pattern="^(json|trig)$"),
    ):
        return await respond(
            EntityCatalogService(db).world(world_id, timeline_id, head_node_id), format
        )

    @router.get("/instance/{instance_id}/entity-catalog")
    async def character_catalog(
        instance_id: UUID, format: str = Query(default="json", pattern="^(json|trig)$")
    ):
        return await respond(EntityCatalogService(db).character(instance_id), format)

    return router
