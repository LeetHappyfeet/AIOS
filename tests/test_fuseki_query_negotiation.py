"""Fuseki must negotiate SPARQL SELECT JSON, not generic SPARQL JSON.

Regression for semantic_hygiene_shadow failing in requests.Response.json() when
the shared Fuseki client advertises application/sparql+json.
"""
import pytest
import requests

from aios_app.rdf import fuseki as fuseki_module
from aios_app.rdf.fuseki import FusekiClient, FusekiError


def _response(body: bytes, content_type: str) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = body
    response.headers["Content-Type"] = content_type
    response.encoding = "utf-8"
    return response


def test_select_query_requests_results_json_and_returns_bindings(monkeypatch):
    observed = {}

    def fake_post(url, *, data, headers, timeout):
        observed.update(url=url, data=data, headers=headers, timeout=timeout)
        return _response(
            b'{"head":{"vars":["count"]},"results":{"bindings":[{"count":{"type":"literal","value":"5"}}]}}',
            "application/sparql-results+json",
        )

    monkeypatch.setattr(fuseki_module.requests, "post", fake_post)
    result = FusekiClient("http://127.0.0.1:3030", retries=0).query(
        "char", "SELECT (COUNT(*) AS ?count) WHERE { GRAPH ?g { ?s ?p ?o } }"
    )
    assert observed["url"].endswith("/char/sparql")
    assert observed["headers"]["Accept"] == "application/sparql-results+json"
    assert result["results"]["bindings"][0]["count"]["value"] == "5"


def test_non_json_response_has_actionable_diagnostic(monkeypatch):
    def fake_post(url, *, data, headers, timeout):
        return _response(b"<sparql><head /></sparql>", "application/sparql-results+xml")

    monkeypatch.setattr(fuseki_module.requests, "post", fake_post)
    with pytest.raises(FusekiError, match="expected SPARQL Results JSON") as exc:
        FusekiClient("http://127.0.0.1:3030", retries=0).query(
            "char", "SELECT * WHERE { ?s ?p ?o } LIMIT 1"
        )
    assert "content_type='application/sparql-results+xml'" in str(exc.value)
    assert "/char/sparql" in str(exc.value)
    assert "<sparql>" in str(exc.value)


def test_json_error_object_not_interpreted_as_empty_graph(monkeypatch):
    def fake_post(url, *, data, headers, timeout):
        return _response(b'{"error":"proxy returned empty response"}', "application/json")

    monkeypatch.setattr(fuseki_module.requests, "post", fake_post)
    with pytest.raises(FusekiError, match="unexpected JSON result shape"):
        FusekiClient("http://127.0.0.1:3030", retries=0).query(
            "char", "SELECT * WHERE { ?s ?p ?o } LIMIT 1"
        )


def test_ask_result_remains_compatible(monkeypatch):
    def fake_post(url, *, data, headers, timeout):
        return _response(
            b'{"head":{},"boolean":true}', "application/sparql-results+json"
        )

    monkeypatch.setattr(fuseki_module.requests, "post", fake_post)
    assert FusekiClient("http://127.0.0.1:3030", retries=0).query(
        "char", "ASK { ?s ?p ?o }"
    )["boolean"] is True
