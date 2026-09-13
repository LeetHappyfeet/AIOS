from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote
from uuid import UUID

from aios_app.db import Database
from aios_app.rdf.fuseki import FusekiClient


TOPOLOGY_GRAPH = "urn:aios:world:topology"


async def ensure_character_root_world(
    db: Database,
    *,
    character_id: str,
) -> UUID:
    """Ensure one stable personal-universe root exists for a character."""
    row = await db.fetchrow(
        """
        SELECT home_world_id
        FROM aios.character_identity
        WHERE character_id=$1
        """,
        character_id,
    )
    if not row:
        raise RuntimeError(f"Unknown character_id '{character_id}'")

    if row["home_world_id"]:
        world = await db.fetchrow(
            "SELECT world_id FROM aios.world WHERE world_id=$1",
            row["home_world_id"],
        )
        if world:
            return world["world_id"]

    world_key = f"char:{character_id}:root"
    created = await db.execute_returning_row(
        """
        INSERT INTO aios.world (
            world_key,
            world_type,
            origin_character_id,
            meta
        )
        VALUES (
            $1,
            'character_root',
            $2,
            jsonb_build_object(
                'source','character_identity',
                'topology_role','personal_universe_root'
            )
        )
        ON CONFLICT (world_key) DO NOTHING
        RETURNING world_id
        """,
        world_key,
        character_id,
    )
    if created:
        root_world_id = created["world_id"]
    else:
        existing = await db.fetchrow(
            "SELECT world_id FROM aios.world WHERE world_key=$1",
            world_key,
        )
        if not existing:
            raise RuntimeError(f"Could not resolve root world for {character_id}")
        root_world_id = existing["world_id"]

    await db.execute(
        """
        UPDATE aios.world
        SET root_world_id=$1,
            origin_character_id=COALESCE(origin_character_id,$2)
        WHERE world_id=$1
        """,
        root_world_id,
        character_id,
    )
    await db.execute(
        """
        UPDATE aios.character_identity
        SET home_world_id=$2,
            updated_at=now()
        WHERE character_id=$1
        """,
        character_id,
        root_world_id,
    )
    return root_world_id


async def latest_source_anchor(
    db: Database,
    *,
    character_id: str,
    session_id: Optional[UUID],
    user_name: Optional[str] = None,
    scope_key: Optional[str] = None,
) -> tuple[Optional[UUID], Optional[UUID]]:
    """Return one exact liminal source coordinate for a runtime identity.

    Source identity is session + character + user + scope. Runtime-world topology
    is intentionally not used as a perception pointer. For compatibility with
    older callers that do not yet pass user/scope explicitly, the newest runtime
    timeline for this character/session supplies the missing identity fields.

    If the resulting source identity is ambiguous, return no binding instead of
    guessing. A runtime timeline can never qualify as a source fallback.
    """
    if not session_id:
        return None, None

    row = await db.fetchrow(
        """
        WITH runtime_identity AS (
            SELECT rt.user_name, rt.scope_key
            FROM aios.timeline rt
            JOIN aios.world rw ON rw.world_id=rt.world_id
            WHERE rt.session_id=$1
              AND rt.character_id=$2
              AND rw.world_key <> 'liminal'
              AND COALESCE(rt.meta->>'world_runtime','false')='true'
            ORDER BY rt.created_at DESC, rt.timeline_id DESC
            LIMIT 1
        ),
        wanted AS (
            SELECT
                COALESCE($3::text, (SELECT user_name FROM runtime_identity)) AS user_name,
                COALESCE($4::text, (SELECT scope_key FROM runtime_identity)) AS scope_key
        ),
        candidates AS (
            SELECT t.timeline_id, n.node_id, t.created_at
            FROM aios.timeline t
            JOIN aios.world w ON w.world_id=t.world_id
            CROSS JOIN wanted i
            LEFT JOIN LATERAL (
                SELECT dn.node_id
                FROM aios.dag_node dn
                WHERE dn.timeline_id=t.timeline_id
                ORDER BY dn.event_id DESC
                LIMIT 1
            ) n ON true
            WHERE t.session_id=$1
              AND t.character_id=$2
              AND w.world_key='liminal'
              AND t.user_name IS NOT DISTINCT FROM i.user_name
              AND t.scope_key IS NOT DISTINCT FROM i.scope_key
        )
        SELECT timeline_id, node_id
        FROM candidates
        WHERE (SELECT count(*) FROM candidates)=1
        LIMIT 1
        """,
        session_id,
        character_id,
        user_name,
        scope_key,
    )
    if not row:
        return None, None
    return row["timeline_id"], row["node_id"]


async def ensure_runtime_branch_world(
    db: Database,
    *,
    character_id: str,
    session_id: Optional[UUID],
    root_world_id: UUID,
) -> dict:
    """Ensure a concrete runtime world beneath the character root.

    Runtime worlds are objective/session topology shared by runtime instances.
    They therefore do not own a per-user source/perception cursor. Source
    timeline/head authority lives only in character_runtime_state.

    The returned anchor fields are deliberately null so activation cannot treat
    historical world anchors as an authorized /char source binding.
    """
    suffix = str(session_id) if session_id else "default"
    world_key = f"char:{character_id}:session:{suffix}"

    existing = await db.fetchrow(
        """
        SELECT world_id, world_key, world_type, parent_world_id, root_world_id,
               anchor_timeline_id, anchor_node_id, origin_character_id
        FROM aios.world
        WHERE world_key=$1
        """,
        world_key,
    )
    if existing:
        result = dict(existing)
        result["anchor_timeline_id"] = None
        result["anchor_node_id"] = None
        return result

    created = await db.execute_returning_row(
        """
        INSERT INTO aios.world (
            world_key,
            world_type,
            parent_world_id,
            root_world_id,
            anchor_timeline_id,
            anchor_node_id,
            origin_character_id,
            meta
        )
        VALUES (
            $1,
            'runtime',
            $2,
            $2,
            NULL,
            NULL,
            $3,
            jsonb_build_object(
                'source','runtime_activation',
                'topology_role','session_branch',
                'source_session_id',$4::text
            )
        )
        ON CONFLICT (world_key) DO NOTHING
        RETURNING world_id, world_key, world_type, parent_world_id, root_world_id,
                  anchor_timeline_id, anchor_node_id, origin_character_id
        """,
        world_key,
        root_world_id,
        character_id,
        str(session_id) if session_id else None,
    )
    if created:
        result = dict(created)
        result["anchor_timeline_id"] = None
        result["anchor_node_id"] = None
        return result

    row = await db.fetchrow(
        """
        SELECT world_id, world_key, world_type, parent_world_id, root_world_id,
               anchor_timeline_id, anchor_node_id, origin_character_id
        FROM aios.world
        WHERE world_key=$1
        """,
        world_key,
    )
    if not row:
        raise RuntimeError(f"Could not resolve runtime branch {world_key}")
    result = dict(row)
    result["anchor_timeline_id"] = None
    result["anchor_node_id"] = None
    return result


def _iri(kind: str, value: object) -> str:
    return f"urn:aios:{kind}:{quote(str(value), safe='')}"


async def project_world_topology(
    db: Database,
    fuseki: FusekiClient,
    *,
    world_id: UUID,
) -> None:
    """Project authoritative SQL world topology into the /world RDF dataset."""
    row = await db.fetchrow(
        """
        SELECT world_id, world_key, world_type, parent_world_id, root_world_id,
               anchor_timeline_id, anchor_node_id, origin_character_id
        FROM aios.world
        WHERE world_id=$1
        """,
        world_id,
    )
    if not row:
        return

    subject = f"<{_iri('world', row['world_id'])}>"
    triples = [
        f"{subject} <urn:aios:world:worldKey> {json.dumps(row['world_key'] or '')} .",
        f"{subject} <urn:aios:world:worldType> {json.dumps(row['world_type'] or 'unknown')} .",
    ]

    if row["parent_world_id"]:
        triples.append(
            f"{subject} <urn:aios:world:parentWorld> <{_iri('world', row['parent_world_id'])}> ."
        )
    if row["root_world_id"]:
        triples.append(
            f"{subject} <urn:aios:world:rootWorld> <{_iri('world', row['root_world_id'])}> ."
        )
    if row["anchor_timeline_id"]:
        triples.append(
            f"{subject} <urn:aios:world:anchorTimeline> <{_iri('timeline', row['anchor_timeline_id'])}> ."
        )
    if row["anchor_node_id"]:
        triples.append(
            f"{subject} <urn:aios:world:branchesAt> <{_iri('dag-node', row['anchor_node_id'])}> ."
        )
    if row["origin_character_id"]:
        triples.append(
            f"{subject} <urn:aios:world:originCharacter> <{_iri('char', row['origin_character_id'])}> ."
        )

    sparql = f"""
    DELETE WHERE {{
      GRAPH <{TOPOLOGY_GRAPH}> {{ {subject} ?p ?o }}
    }};
    INSERT DATA {{
      GRAPH <{TOPOLOGY_GRAPH}> {{
        {' '.join(triples)}
      }}
    }}
    """
    try:
        fuseki.update("world", sparql)
        await db.execute(
            """
            INSERT INTO aios.world_rdf_projection (
                world_id, rdf_graph, projected_at, last_error, updated_at
            )
            VALUES ($1,$2,now(),NULL,now())
            ON CONFLICT (world_id) DO UPDATE
            SET rdf_graph=EXCLUDED.rdf_graph,
                projected_at=now(),
                last_error=NULL,
                updated_at=now()
            """,
            world_id,
            TOPOLOGY_GRAPH,
        )
    except Exception as exc:
        await db.execute(
            """
            INSERT INTO aios.world_rdf_projection (
                world_id, rdf_graph, projected_at, last_error, updated_at
            )
            VALUES ($1,$2,NULL,$3,now())
            ON CONFLICT (world_id) DO UPDATE
            SET last_error=EXCLUDED.last_error,
                updated_at=now()
            """,
            world_id,
            TOPOLOGY_GRAPH,
            repr(exc)[:2000],
        )
        raise
