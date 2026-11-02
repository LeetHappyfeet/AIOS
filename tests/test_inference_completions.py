"""Provider dialects, bounded retries and completion diagnostics without live APIs."""
import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aios_app.inference.broker import (
    CompletionText, InferenceBroker, InferenceCompletionError, InferenceRequest,
    InferenceUnavailable, OpenAICompatibleClient,
)
from aios_app.inference.options import completion_options, validate_options


def provider(**overrides):
    return SimpleNamespace(**{
        "provider_id": uuid4(), "provider_key": "test", "model": "deepseek-reasoner",
        "base_url": "https://api.deepseek.com", "capabilities": {},
        "api_key_env": None, "api_key_secret": None, "timeout_seconds": 30,
        **overrides,
    })


def request(**overrides):
    return InferenceRequest(**{
        "instance_id": uuid4(), "worker_class": "planning", "prompt": "Return JSON",
        "allowed_actions": {}, "output_schema": {"type": "object"}, "max_tokens": 380,
        **overrides,
    })


def response(content='{"strategy":"look up a recipe"}', finish="stop", reasoning="", tokens=10):
    return {"choices": [{"message": {"content": content, "reasoning_content": reasoning},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 50, "completion_tokens": tokens,
                      "completion_tokens_details": {"reasoning_tokens": 7}}}


def mock_api(monkeypatch, responses):
    sent = []
    queue = iter(responses)
    def urlopen(req, timeout):
        assert 0 < timeout <= 30
        sent.append(json.loads(req.data))
        return io.BytesIO(json.dumps(next(queue)).encode())
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return sent


@pytest.mark.parametrize("enabled", [True, False, None])
def test_direct_deepseek_uses_native_thinking_field(monkeypatch, enabled):
    sent = mock_api(monkeypatch, [response()])
    result = OpenAICompatibleClient()._complete(provider(capabilities={"thinking": enabled}), request())
    assert json.loads(result)["strategy"]
    assert "enable_thinking" not in sent[0] and "chat_template_kwargs" not in sent[0]
    assert sent[0].get("thinking") == ({"type": "enabled" if enabled else "disabled"} if enabled is not None else None)
    assert sent[0]["max_tokens"] == 8192
    assert result.metadata["attempts"][0]["reasoning_tokens"] == 7


def test_local_server_flags_and_choice_protocol_are_preserved(monkeypatch):
    sent = mock_api(monkeypatch, [response(content="A")])
    OpenAICompatibleClient()._complete(provider(base_url="http://localhost:5000/v1",
        model="qwen", capabilities={"thinking": False}), request(choice_keys=("A", "B")))
    assert sent[0]["enable_thinking"] is False
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "response_format" not in sent[0]
    assert sent[0]["max_tokens"] == 380


def test_openai_reasoning_and_explicit_proxy_format(monkeypatch):
    sent = mock_api(monkeypatch, [response()])
    OpenAICompatibleClient()._complete(provider(base_url="https://api.openai.com/v1", model="o3-mini",
        capabilities={"reasoning_effort": "low", "thinking": False}), request())
    assert sent[0]["max_completion_tokens"] == 8192
    assert sent[0]["reasoning_effort"] == "low"
    assert "max_tokens" not in sent[0] and "temperature" not in sent[0]
    fields, parameter, budget, limit = completion_options(provider(base_url="https://proxy.invalid/v1",
        capabilities={"api_format": "deepseek", "thinking": True,
                      "token_parameter": "max_completion_tokens", "completion_budget": 4096,
                      "completion_budget_limit": 6000}), 380)
    assert fields == {"thinking": {"type": "enabled"}}
    assert (parameter, budget, limit) == ("max_completion_tokens", 4096, 6000)


def test_generic_api_has_no_server_specific_thinking_flags(monkeypatch):
    sent = mock_api(monkeypatch, [response()])
    OpenAICompatibleClient()._complete(provider(capabilities={"api_format": "generic", "thinking": True,
        "json_mode": False}), request())
    assert not ({"thinking", "enable_thinking", "chat_template_kwargs", "response_format"} & sent[0].keys())


@pytest.mark.parametrize("content", [None, "", '{"strategy":"cut off', '{}'])
def test_truncation_retries_once_with_larger_budget(monkeypatch, content):
    sent = mock_api(monkeypatch, [response(content, "length", "reasoning", 8192), response()])
    result = OpenAICompatibleClient()._complete(provider(), request())
    assert [body["max_tokens"] for body in sent] == [8192, 16384]
    assert len(result.metadata["attempts"]) == 2
    assert result.metadata["attempts"][0]["finish_reason"] == "length"
    assert "reasoning_content" not in json.dumps(result.metadata)
    assert sent[0]["messages"] == sent[1]["messages"]


def test_retry_is_capped_and_exhaustion_preserves_partial_output(monkeypatch):
    sent = mock_api(monkeypatch, [response("", "length", "thinking"), response('{"cut":', "length")])
    with pytest.raises(InferenceCompletionError, match="truncated") as exc:
        OpenAICompatibleClient()._complete(provider(capabilities={"completion_budget": 4000,
            "completion_budget_limit": 5000}), request())
    assert [body["max_tokens"] for body in sent] == [4000, 5000]
    assert exc.value.content == '{"cut":'
    assert len(exc.value.metadata["attempts"]) == 2


@pytest.mark.parametrize("caps", [{"retry_truncated": False}, {"completion_budget_limit": 8192}])
def test_no_retry_when_disabled_or_at_ceiling(monkeypatch, caps):
    sent = mock_api(monkeypatch, [response("", "length", "thinking")])
    with pytest.raises(InferenceCompletionError):
        OpenAICompatibleClient()._complete(provider(capabilities=caps), request())
    assert len(sent) == 1


def test_retry_shares_original_deadline(monkeypatch):
    clock, timeouts = [0.0], []
    monkeypatch.setattr("aios_app.inference.broker.time.monotonic", lambda: clock[0])
    def urlopen(req, timeout):
        timeouts.append(timeout)
        clock[0] += 20
        payload = response("", "length", "thinking") if len(timeouts) == 1 else response()
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    OpenAICompatibleClient()._complete(provider(), request())
    assert timeouts == [30, 10]


@pytest.mark.parametrize("content", [None, ""])
def test_empty_final_is_not_misreported_as_transport_failure(monkeypatch, content):
    sent = mock_api(monkeypatch, [response(content, "stop", "thinking")])
    with pytest.raises(InferenceCompletionError, match="no final content"):
        OpenAICompatibleClient()._complete(provider(), request())
    assert len(sent) == 1


@pytest.mark.parametrize("finish", ["content_filter", "insufficient_system_resource", "aborted"])
def test_other_incomplete_finish_reasons_are_not_accepted(monkeypatch, finish):
    sent = mock_api(monkeypatch, [response("{}", finish)])
    with pytest.raises(InferenceCompletionError, match="did not finish"):
        OpenAICompatibleClient()._complete(provider(), request())
    assert len(sent) == 1


@pytest.mark.parametrize("caps", [{"completion_budget": -1}, {"completion_budget": 1.5},
    {"completion_budget": 2000, "completion_budget_limit": 1000}, {"api_format": "bad"},
    {"thinking": "false"}, {"token_parameter": "bad"}])
def test_invalid_provider_options_are_rejected(caps):
    with pytest.raises(ValueError):
        validate_options(caps)


def broker_fixture(p):
    db = SimpleNamespace(execute_returning_row=AsyncMock(return_value={"request_id": uuid4()}), execute=AsyncMock())
    broker = InferenceBroker(db)
    broker.providers = SimpleNamespace(routable=AsyncMock(return_value=[p]),
        record_health=AsyncMock(), record_protocol_failure=AsyncMock())
    return broker, db


def test_exhausted_budget_is_persisted_without_marking_provider_unhealthy():
    broker, db = broker_fixture(provider())
    broker.client = SimpleNamespace(complete=AsyncMock(side_effect=InferenceCompletionError(
        "completion truncated", '{"partial":', {"attempts": [{"finish_reason": "length"}]})))
    with pytest.raises(InferenceUnavailable, match="truncated"):
        asyncio.run(broker.infer(request()))
    broker.providers.record_health.assert_not_awaited()
    broker.providers.record_protocol_failure.assert_not_awaited()
    persisted = db.execute.await_args.args
    assert persisted[2] == '{"partial":'
    assert json.loads(persisted[5])["_aios_completion"]["attempts"][0]["finish_reason"] == "length"


def test_completion_metadata_is_stored_without_changing_plan_payload():
    broker, db = broker_fixture(provider())
    metadata = {"attempts": [{"finish_reason": "stop", "completion_tokens": 10}]}
    broker.client = SimpleNamespace(complete=AsyncMock(return_value=CompletionText('{"strategy":"test"}', metadata)))
    result = asyncio.run(broker.infer(request()))
    assert result.response.raw == {"strategy": "test"}
    assert result.completion_metadata == metadata
    assert json.loads(db.execute.await_args.args[3])["_aios_completion"] == metadata


def test_completed_malformed_json_remains_a_protocol_error():
    broker, db = broker_fixture(provider())
    broker.client = SimpleNamespace(complete=AsyncMock(return_value=CompletionText(
        '{"invalid":', {"attempts": [{"finish_reason": "stop"}]})))
    with pytest.raises(InferenceUnavailable, match="not valid JSON"):
        asyncio.run(broker.infer(request()))
    broker.providers.record_protocol_failure.assert_awaited_once()
    broker.providers.record_health.assert_not_awaited()


def test_truncation_falls_back_to_another_provider():
    first, second = provider(), provider(provider_key="fallback")
    broker, _ = broker_fixture(first)
    broker.providers.routable.return_value = [first, second]
    broker.client = SimpleNamespace(complete=AsyncMock(side_effect=[
        InferenceCompletionError("truncated", "", {}), '{"strategy":"fallback"}']))
    result = asyncio.run(broker.infer(request()))
    assert result.provider_id == second.provider_id
    broker.providers.record_protocol_failure.assert_not_awaited()
