# aios/rdf/iri.py
from uuid import UUID

BASE = "urn:aios"

def world_iri(world_id: UUID) -> str:
    return f"{BASE}:world:{world_id}"

def character_iri(character_id: str) -> str:
    return f"{BASE}:character:{character_id}"

def char_graph_iri(character_id: str) -> str:
    return f"{BASE}:graph:char:{character_id}"

def world_graph_iri(world_id: UUID) -> str:
    return f"{BASE}:graph:world:{world_id}"

def location_iri(world_id: UUID, loc_id: str) -> str:
    return f"{BASE}:world:{world_id}:loc:{loc_id}"