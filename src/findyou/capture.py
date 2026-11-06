"""Bounded, explicit platform reads with provenance and normalized results."""

import json
import re
from urllib.parse import urlparse

from findyou.contracts import ImportRequest
from findyou.errors import AppError


def video_identifier(value: str) -> str:
    value = value.strip()
    if value.startswith("https://"):
        url = urlparse(value)
        if url.hostname not in {"www.bilibili.com", "bilibili.com", "m.bilibili.com"}:
            raise AppError("video_id_invalid", "Use a Bilibili video ID or canonical video URL", 422)
        path = url.path.strip("/").split("/")
        value = path[1] if len(path) >= 2 and path[0] == "video" else ""
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,15}", value):
        raise AppError("video_id_invalid", "Use a BV/av ID or canonical Bilibili video URL", 422)
    return value


class CaptureService:
    def __init__(self, settings, storage, social, bili):
        self.settings, self.storage, self.social, self.bili = settings, storage, social, bili

    def persist(self, operation, result, external_id, coverage=None):
        serialized = json.dumps(result["data"], ensure_ascii=False)
        if len(serialized) > self.settings.max_import_chars:
            raise AppError("content_too_large", "Connector content exceeds import limit", 413)
        content = self.storage.import_content(ImportRequest(
            title=f"Bilibili {operation}", text=serialized, format="json", platform="bilibili",
            external_id=external_id, source_url=f"https://www.bilibili.com/video/{external_id}",
            visibility="public", authorship="external", metadata={"operation": operation}))
        normalized = self.social.normalize(operation, result["data"], external_id, content["id"],
                                           observed_at=result.get("observed_at"), coverage=coverage)
        return {**normalized, "content_id": content["id"], "duplicate": content["duplicate"]}

    def capture(self, video_id, page=1, sort="hot", include_subtitle=False, heartbeat=None):
        external_id = video_identifier(video_id)
        if type(page) is not int or not 1 <= page <= 100 or sort not in {"hot", "time"}:
            raise AppError("capture_params_invalid", "Invalid capture page or ordering", 422)
        coverage = {"status": "partial", "requested_page": page, "sort": sort,
                    "subtitle_status": "not_requested"}
        content_ids, thread_ids = [], []
        internal_id = None
        operations = [("video_info", {"video_id": external_id}),
                      ("comments", {"video_id": external_id, "page": page, "sort": sort})]
        if include_subtitle:
            operations.append(("subtitle", {"video_id": external_id}))
        for operation, params in operations:
            if heartbeat:
                heartbeat()
            try:
                result = self.bili.execute(operation, params)
            except AppError as exc:
                if operation != "subtitle" or exc.code not in {
                    "connector_auth_required", "connector_auth_expired", "connector_not_found"
                }:
                    raise
                coverage["subtitle_status"] = exc.code
                continue
            if operation == "subtitle":
                coverage["subtitle_status"] = "read"
            normalized = self.persist(operation, result, external_id, coverage)
            internal_id = normalized["video_id"]
            content_ids.append(normalized["content_id"])
            thread_ids.extend(normalized.get("thread_ids", []))
        return {"video_id": internal_id, "external_id": external_id,
                "thread_ids": list(dict.fromkeys(thread_ids)), "content_ids": content_ids,
                "coverage": coverage, "platform_write": False}
