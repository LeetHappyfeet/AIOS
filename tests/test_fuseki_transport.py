from unittest.mock import Mock, patch

import pytest

from aios_app.rdf.fuseki import FusekiClient, FusekiError


def _response(status: int, text: str = "") -> Mock:
    response = Mock()
    response.status_code = status
    response.text = text
    return response


def test_update_uses_native_sparql_update_body() -> None:
    client = FusekiClient("http://127.0.0.1:3030", retries=0)

    with patch("aios_app.rdf.fuseki.requests.post", return_value=_response(200)) as post:
        client.update("world", "INSERT DATA { <urn:a> <urn:b> <urn:c> . }")

    _, kwargs = post.call_args
    assert kwargs["data"] == b"INSERT DATA { <urn:a> <urn:b> <urn:c> . }"
    assert kwargs["headers"]["Content-Type"].startswith("application/sparql-update")


def test_permanent_http_error_is_not_retried() -> None:
    client = FusekiClient("http://127.0.0.1:3030", retries=3)

    with patch("aios_app.rdf.fuseki.requests.post", return_value=_response(413, "too large")) as post:
        with pytest.raises(FusekiError, match="HTTP 413"):
            client.update("world", "INSERT DATA { }")

    assert post.call_count == 1


def test_transient_http_error_is_retried() -> None:
    client = FusekiClient("http://127.0.0.1:3030", retries=2)

    with (
        patch(
            "aios_app.rdf.fuseki.requests.post",
            side_effect=[_response(503, "busy"), _response(200)],
        ) as post,
        patch("aios_app.rdf.fuseki.time.sleep"),
    ):
        client.update("world", "INSERT DATA { }")

    assert post.call_count == 2


def test_error_body_is_bounded() -> None:
    client = FusekiClient("http://127.0.0.1:3030", retries=0)
    body = "x" * 5000

    with patch("aios_app.rdf.fuseki.requests.post", return_value=_response(400, body)):
        with pytest.raises(FusekiError) as exc_info:
            client.update("world", "INSERT DATA { }")

    message = str(exc_info.value)
    assert "[truncated]" in message
    assert len(message) < 2300
