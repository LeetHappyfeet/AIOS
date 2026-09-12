from __future__ import annotations

import json
import logging
import socketserver
import threading
from typing import Any

from .config import SemanticIndexConfig
from .query_local import LocalSemanticQueryService

logger = logging.getLogger("aios.semantic_index.query_server")

_MAX_REQUEST_BYTES = 1024 * 1024


class _SemanticQueryTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_class, service: LocalSemanticQueryService):
        super().__init__(server_address, handler_class)
        self.service = service


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        if not raw or len(raw) > _MAX_REQUEST_BYTES:
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            op = request.get("op")
            service: LocalSemanticQueryService = self.server.service  # type: ignore[attr-defined]

            if op == "search_epistemic":
                hits = service.search_epistemic(
                    str(request.get("query_text") or ""),
                    character_id=str(request.get("character_id") or ""),
                    instance_ids=request.get("instance_ids") or [],
                    top_k=request.get("top_k"),
                )
            elif op == "search":
                hits = service.search(
                    str(request.get("query_text") or ""),
                    collection=str(request.get("collection") or ""),
                    top_k=request.get("top_k"),
                    must=request.get("must") or {},
                    any_values=request.get("any_values") or {},
                )
            else:
                raise ValueError(f"unsupported semantic query op: {op!r}")

            response: dict[str, Any] = {"ok": True, "hits": hits}
        except Exception as exc:
            logger.exception("Semantic query request failed")
            response = {"ok": False, "error": repr(exc), "hits": []}

        self.wfile.write((json.dumps(response, separators=(",", ":"), default=str) + "\n").encode("utf-8"))


def start_query_server(cfg: SemanticIndexConfig) -> _SemanticQueryTCPServer:
    """Start the loopback semantic query server using the warm shared backend."""
    service = LocalSemanticQueryService(cfg)
    server = _SemanticQueryTCPServer((cfg.query_host, cfg.query_port), _Handler, service)
    thread = threading.Thread(
        target=server.serve_forever,
        name="semantic-query-server",
        daemon=True,
    )
    thread.start()
    logger.info(
        "Semantic query service ready on %s:%d model=%s",
        cfg.query_host,
        cfg.query_port,
        cfg.embedding_model,
    )
    return server
