from aios_app.ingest_api import ingest_message
from aios_app.main import app


def test_canonical_app_exposes_exactly_one_post_ingest_route():
    routes = [
        route
        for route in app.router.routes
        if getattr(route, "path", None) == "/ingest"
        and "POST" in (getattr(route, "methods", None) or set())
    ]

    assert len(routes) == 1
    assert routes[0].endpoint.__module__ == "aios_app.main"


def test_ingest_route_delegates_to_canonical_ingest_service(monkeypatch):
    # The public route is intentionally a thin boundary; behavioral semantics
    # live in ingest_api.ingest_message and are covered by pipeline tests.
    import aios_app.main as main

    assert main.ingest_message is ingest_message
