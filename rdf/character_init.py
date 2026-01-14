# aios_app/rdf/character_init.py

from __future__ import annotations

import logging
from typing import Optional

from .fuseki import FusekiClient, FusekiError

logger = logging.getLogger("aios.rdf.character_init")


def _graph_iri(character_id: str) -> str:
    safe = character_id.replace(" ", "_")
    return f"urn:aios:graph:character:{safe}"


def _char_iri(character_id: str) -> str:
    safe = character_id.replace(" ", "_")
    return f"urn:aios:character:{safe}"


def ensure_character_exists(
    fuseki: FusekiClient,
    *,
    dataset: str,
    character_id: str,
) -> bool:
    """
    Ensure that a character exists in the RDF character dataset.

    Semantics:
    - If the character already exists → do nothing
    - If missing → create a minimal liminal stub
    - Never overwrites
    - Never deletes
    - Safe to call repeatedly
    - Safe to fail (caller should treat False as non-fatal)

    Returns:
        True if ensured or already exists
        False if Fuseki error occurred
    """

    graph = _graph_iri(character_id)
    char = _char_iri(character_id)

    # -----------------------------
    # Step 1: ASK if character exists
    # -----------------------------
    ask_sparql = f"""
PREFIX aios: <urn:aios:>
ASK {{
  GRAPH <{graph}> {{
    <{char}> a aios:Character .
  }}
}}
""".strip()

    try:
        result = fuseki.query(dataset, ask_sparql)
        exists = bool(result.get("boolean"))
        if exists:
            logger.debug("Character %s already exists in RDF", character_id)
            return True
    except FusekiError as e:
        logger.warning(
            "Fuseki ASK failed while checking character %s: %s",
            character_id,
            e,
        )
        return False
    except Exception as e:
        logger.warning(
            "Unexpected error during character existence check (%s): %s",
            character_id,
            e,
        )
        return False

    # -----------------------------
    # Step 2: INSERT minimal liminal stub
    # -----------------------------
    insert_sparql = f"""
PREFIX aios: <urn:aios:>

INSERT DATA {{
  GRAPH <{graph}> {{
    <{char}> a aios:Character ;
        aios:characterId "{character_id}" ;
        aios:homeWorld <urn:aios:world:liminal> ;
        aios:currentWorld <urn:aios:world:liminal> ;
        aios:epistemicStatus "liminal" ;
        aios:createdBy "aios.system" .
  }}
}}
""".strip()

    try:
        fuseki.update(dataset, insert_sparql)
        logger.info("Created liminal RDF stub for character %s", character_id)
        return True
    except FusekiError as e:
        logger.warning(
            "Fuseki INSERT failed while creating character %s: %s",
            character_id,
            e,
        )
        return False
    except Exception as e:
        logger.warning(
            "Unexpected error during character creation (%s): %s",
            character_id,
            e,
        )
        return False