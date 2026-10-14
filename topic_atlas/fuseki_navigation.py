"""Bounded read-only navigation of Fuseki Topic Atlas graphs.

Never use a Fuseki topic edge as a belief, a corpus authorization, or a direct
source recommendation. Callers MUST join neighbor IDs back to authorized SQL.
"""
from __future__ import annotations

from uuid import UUID

from aios_app.topic_atlas.projection import topic_graph_location

PREFIX="urn:aios:knowledge-topic:"


def parse_neighbor_ids(result: dict, *, anchor: UUID, limit: int = 12) -> tuple[UUID, ...]:
    anchor_iri=PREFIX+str(anchor)
    found=[]
    for row in (result.get("results") or {}).get("bindings") or []:
        start=(row.get("from") or {}).get("value")
        end=(row.get("to") or {}).get("value")
        candidate=end if start==anchor_iri else start if end==anchor_iri else ""
        if not isinstance(candidate,str) or not candidate.startswith(PREFIX):
            continue
        try:
            neighbor=UUID(candidate[len(PREFIX):])
        except ValueError:
            continue
        if neighbor!=anchor and neighbor not in found:
            found.append(neighbor)
        if len(found)>=max(1,min(int(limit),24)):
            break
    return tuple(found)


def query_neighbors(fuseki, topic_row, *, limit: int = 12) -> tuple[UUID, ...]:
    topic_id=topic_row["topic_id"]
    dataset,graph=topic_graph_location(topic_row)
    subject=PREFIX+str(topic_id)
    query=(
        "SELECT ?from ?to WHERE { GRAPH <"+graph+"> { "
        "<"+subject+"> <urn:aios:topic#hasNavigationRelation> ?relation . "
        "?relation <urn:aios:topic#fromTopic> ?from ; "
        "<urn:aios:topic#toTopic> ?to . "
        "} } LIMIT "+str(max(1,min(int(limit)*2,48)))
    )
    result=fuseki.query(dataset,query)
    return parse_neighbor_ids(result,anchor=topic_id,limit=limit)
