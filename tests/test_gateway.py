import json

import httpx
import pytest

from findyou.config import Settings
from findyou.errors import AppError
from findyou.models_gateway import Gateway


def settings(**kwargs):
    return Settings(_env_file=None, llm_api_key="test-key", **kwargs)


def completion(data, **kwargs):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(data)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}, **kwargs})


@pytest.mark.parametrize("provider,endpoint", [
    ("deepseek", "https://api.deepseek.com/chat/completions"),
    ("glm", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
])
def test_real_request_json_and_usage(provider, endpoint):
    calls = []

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert str(request.url) == endpoint
        assert request.headers["authorization"] == "Bearer test-key"
        assert body["response_format"] == {"type": "json_object"}
        assert body["max_tokens"] == 2048
        assert "JSON schema" in body["messages"][0]["content"]
        return completion({"answer": "Evidence based", "evidence_ids": ["m1"]})

    result = Gateway(settings(llm_provider=provider), httpx.MockTransport(handler)).generate(
        "reply", "Use evidence.", {"question": "?", "evidence": [{"id": "m1", "text": "X"}]})
    assert result.provider == provider and not result.simulated
    assert result.input_tokens == 10 and result.output_tokens == 5
    assert len(calls) == 1


def test_fake_is_explicit_deterministic_and_extracts_exact_blocks():
    gateway = Gateway(Settings(_env_file=None, llm_provider="fake"))
    payload = {"blocks": [{"id": "b1", "text": "Owner likes tea"}]}
    result = gateway.generate("extract", "", payload)
    assert result.simulated and result.data["facts"][0]["evidence_block_ids"] == ["b1"]
    assert result.data == gateway.generate("extract", "", payload).data
    reply = gateway.generate("reply", "", {"evidence": [{"id": "m1", "text": "Tea"}]})
    assert reply.data == {"answer": "[SIMULATED] Tea", "evidence_ids": ["m1"]}
    assert gateway.generate("summarize", "", {"text": "Tea"}).data["summary"].startswith("[SIMULATED]")


@pytest.mark.parametrize("data", [{"answer": "x"}, {"answer": "x", "evidence_ids": [2]},
                                  {"answer": "x", "evidence_ids": [], "extra": 1},
                                  {"answer": "", "evidence_ids": []}])
def test_rejects_invalid_schema(data):
    with pytest.raises(AppError, match="invalid structured"):
        Gateway(settings(), httpx.MockTransport(lambda r: completion(data))).generate("reply", "", {})


def test_timeout_and_error_do_not_echo_secrets_or_fallback():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="test-key confidential upstream body")

    with pytest.raises(AppError) as exc:
        Gateway(settings(), httpx.MockTransport(handler)).generate("summarize", "", {})
    assert exc.value.code == "model_upstream_error" and not exc.value.retryable
    assert "test-key" not in str(exc.value) and len(calls) == 1

    def timeout(request):
        raise httpx.ReadTimeout("test-key", request=request)

    with pytest.raises(AppError) as exc:
        Gateway(settings(), httpx.MockTransport(timeout)).generate("summarize", "", {})
    assert exc.value.code == "model_timeout" and exc.value.retryable
    assert "test-key" not in str(exc.value)


def test_malformed_truncated_output_and_size_limits():
    for response in [httpx.Response(200, text="not json"),
                     completion({}, choices=[{"finish_reason": "length", "message": {"content": "{}"}}]),
                     httpx.Response(200, text="x" * 1_000_001)]:
        with pytest.raises(AppError) as exc:
            Gateway(settings(), httpx.MockTransport(lambda r: response)).generate("summarize", "", {})
        assert exc.value.code == "model_output_invalid"


@pytest.mark.parametrize("operation,payload", [("delete", {}), ("reply", {"cookie": "x"}),
                                               ("extract", {"blocks": "bad"}),
                                               ("reply", {"text": "test-key"})])
def test_invalid_input_never_calls_transport(operation, payload):
    def forbidden(request):
        pytest.fail("Invalid input reached provider")
    with pytest.raises(AppError):
        Gateway(settings(), httpx.MockTransport(forbidden)).generate(operation, "", payload)


def test_unconfigured_never_falls_back():
    gateway = Gateway(Settings(_env_file=None))
    assert not gateway.status()["configured"]
    with pytest.raises(AppError) as exc:
        gateway.generate("summarize", "", {})
    assert exc.value.code == "model_not_configured"


@pytest.mark.parametrize("length,valid", [(40, True), (41, False)])
def test_extraction_kind_matches_storage_limit(length, valid):
    data = {"facts": [{"text": "Tea", "evidence_block_ids": ["b1"], "kind": "x" * length}]}
    gateway = Gateway(settings(), httpx.MockTransport(lambda r: completion(data)))
    if valid:
        assert gateway.generate("extract", "", {}).data == data
    else:
        with pytest.raises(AppError) as exc:
            gateway.generate("extract", "", {})
        assert exc.value.code == "model_output_invalid"


@pytest.mark.parametrize("body", [
    "test-key", [], None,
    {"choices": "test-key"}, {"choices": []}, {"choices": ["test-key"]},
    {"choices": [None]}, {"choices": [42]},
    {"choices": [{"finish_reason": "stop", "message": "test-key"}]},
    {"choices": [{"finish_reason": "stop", "message": []}]},
    {"choices": [{"finish_reason": "stop", "message": None}]},
    *[{"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
      for content in [None, 42, {}, []]],
    *[{"choices": [{"finish_reason": "stop", "message": {"content": '{"summary":"ok"}'}}],
       "usage": usage} for usage in ["test-key", [], 0, False, None]],
], ids=["body-string", "body-list", "body-null", "choices-string", "choices-empty",
        "choice-string", "choice-null", "choice-number", "message-string", "message-list",
        "message-null", "content-null", "content-number", "content-object", "content-list",
        "usage-string", "usage-list", "usage-zero", "usage-false", "usage-null"])
def test_malformed_envelope_has_sanitized_auditable_error(body):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=json.dumps(body)))
    with pytest.raises(AppError) as exc:
        Gateway(settings(), transport).generate("summarize", "", {})
    assert exc.value.code == "model_output_invalid"
    assert exc.value.status == 502
    assert "test-key" not in str(exc.value)


@pytest.mark.parametrize("system,payload", [(None, {}), ([], {}), ("", "wrong"),
                                           ("", {"text": object()}), ("", {"text": float("nan")})],
                         ids=["system-null", "system-list", "payload-string", "non-json", "nan"])
def test_wrong_input_content_never_reaches_provider(system, payload):
    def forbidden(request):
        pytest.fail("Malformed input reached provider")
    with pytest.raises(AppError):
        Gateway(settings(), httpx.MockTransport(forbidden)).generate("summarize", system, payload)
