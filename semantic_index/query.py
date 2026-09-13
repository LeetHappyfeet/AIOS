from __future__ import annotations

import json
import socket
from typing import Any, Iterable

from .config import SemanticIndexConfig


class SemanticQueryService:
    """Lightweight client for the always-warm Semantic Index query service.

    The API/HUD process must never instantiate the sentence-transformer model.
    Embedding and Qdrant vector search are owned by the Semantic Index process,
    which loads and warms the model during service startup.
    """

    def __init__(self, cfg: SemanticIndexConfig | None = None):
        self.cfg = cfg or SemanticIndexConfig()

    def _request(self, payload: dict[str, Any]) -> list[tuple[str, float, dict[str, Any]]]:
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with socket.create_connection(
            (self.cfg.query_host, self.cfg.query_port),
            timeout=self.cfg.query_timeout_seconds,
        ) as sock:
            sock.settimeout(self.cfg.query_timeout_seconds)
            sock.sendall(data)
            response = b""
            while not response.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response += chunk
                if len(response) > 8 * 1024 * 1024:
                    raise RuntimeError("semantic query response exceeded size limit")

        if not response:
            raise RuntimeError("semantic query service returned no response")
        result = json.loads(response.decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "semantic query service failed")
        return [
            (str(item[0]), float(item[1]), dict(item[2]))
            for item in result.get("hits", [])
        ]

    def search(
        self,
        query_text: str,
        *,
        collection: str,
        top_k: int | None = None,
        must: dict[str, Any] | None = None,
        any_values: dict[str, Iterable[Any]] | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        if not query_text.strip():
            return []
        return self._request({
            "op": "search",
            "query_text": query_text,
            "collection": collection,
            "top_k": top_k,
            "must": must or {},
            "any_values": {
                key: [str(value) for value in values if value is not None]
                for key, values in (any_values or {}).items()
            },
        })

    def search_epistemic(
        self,
        query_text: str,
        *,
        character_id: str,
        instance_ids: Iterable[Any],
        top_k: int | None = None,
        world_ids: Iterable[Any] | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        if not query_text.strip():
            return []
        payload: dict[str, Any] = {
            "op": "search_epistemic",
            "query_text": query_text,
            "character_id": character_id,
            "instance_ids": [str(value) for value in instance_ids if value is not None],
            "top_k": top_k,
        }
        if world_ids is not None:
            payload["world_ids"] = [str(value) for value in world_ids if value is not None]
        return self._request(payload)

    def search_epistemic_staged(
        self,
        query_text: str,
        *,
        character_id: str,
        instance_ids: Iterable[Any],
        world_stages: Iterable[Iterable[Any]],
        top_k: int | None = None,
        min_hits: int = 8,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        """Search ordered world scopes while embedding the query only once.

        Each stage is an allow-list of equivalent-priority worlds. The semantic
        service searches stage 0 first and only widens when it has fewer than
        ``min_hits`` unique results.
        """
        if not query_text.strip():
            return []
        stages = [
            [str(value) for value in stage if value is not None]
            for stage in world_stages
        ]
        stages = [stage for stage in stages if stage]
        if not stages:
            return []
        return self._request({
            "op": "search_epistemic_staged",
            "query_text": query_text,
            "character_id": character_id,
            "instance_ids": [str(value) for value in instance_ids if value is not None],
            "world_stages": stages,
            "top_k": top_k,
            "min_hits": max(1, int(min_hits)),
        })
