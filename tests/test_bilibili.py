import json
import subprocess
from types import SimpleNamespace

import pytest

from findyou.bilibili import BilibiliAdapter
from findyou.config import Settings
from findyou.errors import AppError

BV = "BV1xx411c7mD"


def cli(data=None, *, error=None):
    body = {"ok": False, "error": error} if error else {"ok": True, "data": data}
    return SimpleNamespace(stdout=json.dumps(body), stderr="secret stderr", returncode=1 if error else 0)


class Runner:
    def __init__(self, data=None, *, error=None):
        self.calls = []
        self.result = cli(data or {}, error=error)

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return cli({"version": "1.2.1"}) if args[-1] == "--version" else self.result


def adapter(tmp_path, runner):
    return BilibiliAdapter(Settings(_env_file=None, bili_python="isolated-python",
                                   bili_home=tmp_path / "isolated-home"), runner)


@pytest.mark.parametrize("operation,params,command", [
    ("search", {"keyword": "python", "size": 2}, ["search", "video", "python"]),
    ("recommend", {"count": 2}, ["feed", "recommend", "-n", "2", "--page", "1"]),
    ("video_info", {"video_id": BV}, ["video", "info", BV]),
    ("subtitle", {"video_id": BV, "lang": "ai-zh"}, ["video", "subtitle", BV]),
    ("comments", {"video_id": BV}, ["comment", "list", BV]),
    ("status", {}, ["auth", "status", "--wait", "0"]),
    ("login_start", {}, ["auth", "login", "--wait", "0"]),
    ("login_poll", {"wait": 30}, ["auth", "status", "--wait", "30"]),
])
def test_exact_command_environment_and_envelope(tmp_path, monkeypatch, operation, params, command):
    runner = Runner({"items": [{"from": 0, "to": 1, "content": "caption"}],
                     "sessdata": "secret", "nested": {"cookie": "secret"}})
    monkeypatch.setenv("FINDYOU_LLM_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    out = adapter(tmp_path, runner).execute(operation, params)
    args, options = runner.calls[-1]
    assert args[:4] == ["isolated-python", "-m", "bili_cli.main", "--compact"]
    assert args[4:4 + len(command)] == command
    assert not options["shell"] and options["timeout"] < 240
    assert options["env"]["BILI_CLI_HOME"] == str((tmp_path / "isolated-home").resolve())
    assert "FINDYOU_LLM_API_KEY" not in options["env"] and "OPENAI_API_KEY" not in options["env"]
    assert out["platform"] == "bilibili" and out["operation"] == operation
    assert "secret" not in json.dumps(out)


@pytest.mark.parametrize("operation,params", [("watch", {}), ("comment_post", {}), ("run_bili", {}),
    ("login_start", {"qr_out": "outside.png"}), ("search", {"keyword": "--help"}),
    ("search", {"keyword": "x", "size": 51}), ("search", {"keyword": "\n"}),
    ("subtitle", {"video_id": BV, "page": True}), ("video_info", {"video_id": "https://evil.test"}),
    ("comments", {"video_id": BV, "sort": "post"}), ("login_poll", {"wait": 120})])
def test_write_and_invalid_params_rejected_before_subprocess(tmp_path, operation, params):
    runner = Runner()
    with pytest.raises(AppError):
        adapter(tmp_path, runner).execute(operation, params)
    assert not runner.calls


def test_dependency_pin_status_and_missing_executable(tmp_path):
    def runner(*args, **kwargs):
        return cli({"version": "1.3.0"})
    ad = adapter(tmp_path, runner)
    assert not ad.status()["version_match"]
    with pytest.raises(AppError) as exc:
        ad.execute("search", {"keyword": "x"})
    assert exc.value.code == "connector_dependency_invalid"
    assert not BilibiliAdapter(Settings(_env_file=None)).status()["configured"]


def test_pending_auth_sanitized_and_content_requires_auth(tmp_path):
    runner = Runner(error={"code": "login_pending", "message": "SESSDATA=secret",
                           "detail": {"scanned": True, "qr_url": "secret", "cookie": "secret"}})
    out = adapter(tmp_path, runner).execute("login_poll", {})
    assert out["data"] == {"logged_in": False, "status": "pending", "scanned": True}
    assert "secret" not in json.dumps(out)
    with pytest.raises(AppError) as exc:
        adapter(tmp_path, runner).execute("comments", {"video_id": BV})
    assert exc.value.code == "connector_auth_required"


def test_qr_and_user_allowlist(tmp_path):
    runner = Runner({"logged_in": False, "status": "pending", "qr_url": "https://evil.test/key",
                     "qr_image": "C:/outside.png", "expires_in": 999, "user": {"mid": 1, "cookie": "secret"}})
    data = adapter(tmp_path, runner).execute("login_start", {})["data"]
    assert data == {"logged_in": False, "status": "pending", "expires_in": 180, "user": {"mid": 1}}


@pytest.mark.parametrize("raw", ["not json", "{}", '{"ok":true,"data":[]}',
                                   '{"ok":true,"data":{}} trailing', "x" * 2_000_001],
                         ids=["text", "missing-envelope", "data-list", "trailing", "oversized"])
def test_output_parser_invalid_and_no_stderr_leak(tmp_path, raw):
    runner = Runner()
    runner.result = SimpleNamespace(stdout=raw, returncode=0, stderr="secret")
    with pytest.raises(AppError) as exc:
        adapter(tmp_path, runner).execute("video_info", {"video_id": BV})
    assert exc.value.code == "connector_output_invalid" and "secret" not in str(exc.value)


def test_timeout_safe_error(tmp_path):
    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired("secret", 10, output="credential")
    status = adapter(tmp_path, runner).status()
    assert status["error"]["code"] == "connector_timeout"
    assert "secret" not in json.dumps(status)


def test_platform_failure_safe_and_retryable(tmp_path):
    runner = Runner(error={"code": "api_error", "message": "credential", "hint": "secret"})
    with pytest.raises(AppError) as exc:
        adapter(tmp_path, runner).execute("video_info", {"video_id": BV})
    assert exc.value.code == "connector_upstream_error" and exc.value.retryable
    assert "secret" not in str(exc.value) and "credential" not in str(exc.value)


@pytest.mark.parametrize("upstream,expected,status", [
    ("risk_control", "connector_risk_control", 429),
    ("captcha_required", "connector_captcha_required", 409),
])
def test_risk_and_captcha_stop_without_retry_or_raw_error(tmp_path, upstream, expected, status):
    runner = Runner(error={"code": upstream, "message": "cookie=secret", "hint": "raw sensitive body",
                           "detail": {"cookie": "secret"}})
    with pytest.raises(AppError) as exc:
        adapter(tmp_path, runner).execute("search", {"keyword": "x"})
    assert exc.value.code == expected and exc.value.status == status
    assert not exc.value.retryable
    assert len(runner.calls) == 2  # one version check, one operation; no internal retry
    assert "secret" not in json.dumps(exc.value.as_dict())
    assert "raw sensitive body" not in str(exc.value)


def test_rate_limit_is_retryable_but_adapter_does_not_loop(tmp_path):
    runner = Runner(error={"code": "rate_limited", "message": "secret", "detail": {"retry_after": 999999}})
    with pytest.raises(AppError) as exc:
        adapter(tmp_path, runner).execute("search", {"keyword": "x"})
    assert exc.value.code == "connector_rate_limited" and exc.value.status == 429
    assert exc.value.retryable
    assert len(runner.calls) == 2
    assert runner.calls[-1][1]["timeout"] <= 90
    assert "secret" not in json.dumps(exc.value.as_dict())


def test_expired_auth_is_preserved_for_auth_status_and_content_error(tmp_path):
    runner = Runner(error={"code": "auth_expired", "message": "secret"})
    auth = adapter(tmp_path, runner).execute("status", {})
    assert auth["data"]["status"] == "expired" and not auth["data"]["logged_in"]
    with pytest.raises(AppError) as exc:
        adapter(tmp_path, runner).execute("comments", {"video_id": BV})
    assert exc.value.code == "connector_auth_expired" and not exc.value.retryable


@pytest.mark.parametrize("code", ["not_found", "no_subtitles"])
def test_no_subtitle_error_is_explicit_not_empty_success(tmp_path, code):
    runner = Runner(error={"code": code, "message": "secret", "detail": {"available": []}})
    with pytest.raises(AppError) as exc:
        adapter(tmp_path, runner).execute("subtitle", {"video_id": BV})
    assert exc.value.code == "connector_not_found" and not exc.value.retryable
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("data", [{"status": "no_subtitles"}, {"no_subtitles": True}, {"items": []},
                                  {"items": [{"content": "  "}]}])
def test_no_subtitle_data_is_explicit_not_empty_success(tmp_path, data):
    with pytest.raises(AppError) as exc:
        adapter(tmp_path, Runner(data)).execute("subtitle", {"video_id": BV})
    assert exc.value.code == "connector_not_found" and not exc.value.retryable
