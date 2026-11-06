"""Pinned bili-agent subprocess adapter with an explicit operation whitelist."""

import json
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .errors import AppError

OPERATIONS = ("status", "login_start", "login_poll", "search", "recommend", "video_info", "subtitle", "comments")


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _LoginPoll(_Params):
    wait: int = Field(default=10, ge=0, le=30)


class _Search(_Params):
    keyword: str = Field(min_length=1, max_length=200)
    page: int = Field(default=1, ge=1, le=100)
    size: int = Field(default=20, ge=1, le=50)
    order: str = "default"


class _Video(_Params):
    video_id: str = Field(pattern=r"^(BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,15})$")


class _Recommend(_Params):
    count: int = Field(default=3, ge=1, le=10)
    page: int = Field(default=1, ge=1, le=10)


class _Subtitle(_Video):
    page: int = Field(default=1, ge=1, le=100)
    lang: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9-]{1,30}$")


class _Comments(_Video):
    page: int = Field(default=1, ge=1, le=100)
    sort: str = "hot"


_PARAMS = {"status": _Params, "login_start": _Params, "login_poll": _LoginPoll,
           "search": _Search, "recommend": _Recommend, "video_info": _Video,
           "subtitle": _Subtitle, "comments": _Comments}
_SECRETS = {"sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value",
            "cookie", "cookies", "credential", "credentials", "qrcode_key", "refresh_token",
            "authorization", "api_key", "password"}
_AUTH_CODES = {"not_logged_in": "not_logged_in", "auth_expired": "expired",
               "login_expired": "expired", "login_pending": "pending"}


def _clean(value, depth=0):
    if depth > 15:
        raise AppError("connector_output_invalid", "Connector data is nested too deeply.", 502)
    if isinstance(value, dict):
        return {k: _clean(v, depth + 1) for k, v in value.items()
                if k.lower() not in _SECRETS and k not in {"next", "hint", "refresh_error"}}
    if isinstance(value, list):
        return [_clean(v, depth + 1) for v in value[:2000]]
    if isinstance(value, float) and not math.isfinite(value):
        raise AppError("connector_output_invalid", "Connector returned invalid numeric data.", 502)
    return value


class BilibiliAdapter:
    def __init__(self, settings: Settings, runner=None):
        self.settings = settings
        self.runner = runner or subprocess.run

    def _run(self, args: list[str], timeout: float) -> dict:
        s = self.settings
        if not s.bili_python:
            raise AppError("connector_not_configured", "Configure FINDYOU_BILI_PYTHON for isolated bili-agent.", 503)
        env = os.environ.copy()
        # CLI only receives its operational environment, never model/service credentials.
        for key in list(env):
            if key.startswith("FINDYOU_") or re.search(r"(API_KEY|TOKEN|PASSWORD|SECRET|COOKIE)", key, re.I):
                env.pop(key, None)
        env.update({"BILI_CLI_HOME": str(s.effective_bili_home.resolve()),
                    "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        try:
            result = self.runner([s.bili_python, "-m", "bili_cli.main", "--compact", *args],
                                 shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise AppError("connector_timeout", "Bilibili operation timed out.", 504, retryable=True) from None
        except OSError:
            raise AppError("connector_unavailable", "Isolated Bilibili executable is unavailable.", 503) from None
        raw = result.stdout
        if not isinstance(raw, str) or len(raw) > 2_000_000:
            raise AppError("connector_output_invalid", "Connector returned invalid or excessive output.", 502)
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            raise AppError("connector_output_invalid", "Connector did not return one JSON document.", 502) from None
        if not isinstance(body, dict) or type(body.get("ok")) is not bool:
            raise AppError("connector_output_invalid", "Connector returned an invalid JSON envelope.", 502)
        if not body["ok"]:
            error = body.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str):
                raise AppError("connector_output_invalid", "Connector returned an invalid error envelope.", 502)
            if code in _AUTH_CODES:
                detail = error.get("detail") or {}
                return {"logged_in": False, "status": _AUTH_CODES[code],
                        "scanned": isinstance(detail, dict) and detail.get("scanned") is True}
            mapping = {
                "not_found": ("connector_not_found", "No accessible result; login may be required.", 404, False),
                "no_subtitles": ("connector_not_found", "No accessible subtitle content.", 404, False),
                "rate_limited": ("connector_rate_limited", "Bilibili rate limit reached.", 429, True),
                "risk_control": ("connector_risk_control", "Bilibili risk control requires human review.", 429, False),
                "captcha_required": ("connector_captcha_required", "Bilibili requires human verification.", 409, False),
                "timeout": ("connector_timeout", "Bilibili operation timed out.", 504, True),
            }
            mapped = mapping.get(code, ("connector_upstream_error", "Bilibili rejected the read operation.", 502, True))
            raise AppError(*mapped[:3], retryable=mapped[3])
        if result.returncode != 0 or not isinstance(body.get("data"), dict):
            raise AppError("connector_output_invalid", "Connector returned an inconsistent result.", 502)
        return _clean(body["data"])

    def _dependency(self) -> dict:
        status = {"configured": bool(self.settings.bili_python), "available": False,
                  "installed_version": None, "required_version": self.settings.bili_version,
                  "version_match": False, "read_only": True, "operations": list(OPERATIONS)}
        if not status["configured"]:
            return status
        try:
            data = self._run(["--version"], 15)
            version = data.get("version")
            if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version):
                status.update(available=True, installed_version=version,
                              version_match=version == self.settings.bili_version == "1.2.1")
        except AppError as exc:
            status["error"] = exc.as_dict()
        return status

    def status(self) -> dict:
        return self._dependency()

    def execute(self, operation: str, params: dict) -> dict:
        if operation not in OPERATIONS:
            raise AppError("connector_operation_disabled", "Bilibili operation is outside the read-only whitelist.", 403)
        try:
            p = _PARAMS[operation].model_validate(params)
        except ValidationError:
            raise AppError("connector_params_invalid", "Invalid Bilibili operation parameters.") from None
        if isinstance(p, _Search) and (p.order not in {"default", "views", "new", "danmaku", "favorites", "comments"}
                                      or not p.keyword.strip() or p.keyword.startswith("-")
                                      or any(ord(c) < 32 for c in p.keyword)):
            raise AppError("connector_params_invalid", "Invalid search keyword or ordering.")
        if isinstance(p, _Comments) and p.sort not in {"hot", "time"}:
            raise AppError("connector_params_invalid", "Invalid comments ordering.")
        dep = self._dependency()
        if not dep["available"] or not dep["version_match"]:
            raise AppError("connector_dependency_invalid", "Isolated bili-agent 1.2.1 is required.", 503)
        timeout = min(max(self.settings.bili_timeout, 1), 90)
        if operation == "status":
            args = ["auth", "status", "--wait", "0"]
        elif operation == "login_start":
            args = ["auth", "login", "--wait", "0"]
        elif operation == "login_poll":
            args = ["auth", "status", "--wait", str(p.wait)]
            timeout = min(p.wait + 20, 50)  # always below the 240-second job lease
        elif operation == "search":
            args = ["search", "video", p.keyword, "--order", p.order,
                    "--page", str(p.page), "--size", str(p.size)]
        elif operation == "recommend":
            args = ["feed", "recommend", "-n", str(p.count), "--page", str(p.page)]
        elif operation == "video_info":
            args = ["video", "info", p.video_id]
        elif operation == "subtitle":
            args = ["video", "subtitle", p.video_id, "--page", str(p.page)]
            if p.lang:
                args += ["--lang", p.lang]
        else:
            args = ["comment", "list", p.video_id, "--type", "video", "--sort", p.sort, "--page", str(p.page)]
        data = self._run(args, timeout)
        if operation in {"status", "login_start", "login_poll"}:
            data = self._auth_data(data)
        elif "logged_in" in data:
            if data.get("status") == "expired":
                raise AppError("connector_auth_expired", "Bilibili login has expired; renew it to continue.", 401)
            raise AppError("connector_auth_required", "Bilibili login is required for this read.", 401)
        if operation == "subtitle":
            if data.get("status") == "no_subtitles" or data.get("no_subtitles") is True:
                raise AppError("connector_not_found", "No accessible subtitle content.", 404)
            items = data.get("items")
            if not isinstance(items, list):
                raise AppError("connector_output_invalid", "Connector returned invalid subtitle content.", 502)
            if not any(isinstance(item, dict) and isinstance(item.get("content"), str)
                       and item["content"].strip() for item in items):
                raise AppError("connector_not_found", "No accessible subtitle content.", 404)
        return {"platform": "bilibili", "operation": operation,
                "read_only": operation not in {"login_start", "login_poll", "status"},
                "data": data, "observed_at": datetime.now(timezone.utc).isoformat()}

    def _auth_data(self, data: dict) -> dict:
        out = {"logged_in": data.get("logged_in") is True,
               "status": data.get("status", "authenticated" if data.get("logged_in") is True else "unknown")}
        if "scanned" in data:
            out["scanned"] = data["scanned"] is True
        user = data.get("user")
        if isinstance(user, dict):
            out["user"] = {k: v for k, v in user.items() if k in {"mid", "name", "level"}}
        url = data.get("qr_url")
        if isinstance(url, str) and len(url) < 2000:
            parsed = urlparse(url)
            if parsed.scheme == "https" and parsed.hostname == "passport.bilibili.com":
                out["qr_url"] = url
        image = data.get("qr_image")
        if isinstance(image, str):
            expected = (self.settings.effective_bili_home / "login_qrcode.png").resolve()
            if Path(image).resolve() == expected:
                out["qr_image"] = str(expected)
        if type(data.get("expires_in")) is int:
            out["expires_in"] = min(max(data["expires_in"], 0), 180)
        return out
