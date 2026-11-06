import hmac
import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from findyou.config import Settings
from findyou.contracts import ImportRequest, Scope
from findyou.errors import AppError
from findyou.life_api import build_life_router
from findyou.ops import Action, Conversation, Job, LLMCall
from findyou.runtime import Runtime
from findyou.social_api import build_social_router


class ConversationRequest(BaseModel):
    peer_id: str = Field(min_length=1, max_length=200)
    thread_key: str = Field(min_length=1, max_length=200)
    scope: Scope = "public"
    platform: str = Field(default="simulation", max_length=40)
    account_key: str = Field(default="local", max_length=100)


class SimulationRequest(BaseModel):
    conversation_id: str
    question: str = Field(min_length=1, max_length=2000)
    external_id: str | None = Field(default=None, max_length=200)


class CorrectionRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    visibility: Scope | None = None


class VisibilityRequest(BaseModel):
    visibility: Scope


class BiliRequest(BaseModel):
    params: dict = Field(default_factory=dict)


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    rt = runtime or Runtime(settings or Settings())
    app = FastAPI(title="FindYou API", version="0.1.0", description="Single-owner memory and read-only Bilibili. No platform publishing.")
    app.state.runtime = rt
    bearer = HTTPBearer(auto_error=False)

    def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        token = rt.settings.api_token.get_secret_value() if rt.settings.api_token else ""
        if not token:
            raise AppError("auth_not_configured", "Configure FINDYOU_API_TOKEN before accessing management APIs", 503)
        if not credentials or not hmac.compare_digest(credentials.credentials, token):
            raise AppError("unauthorized", "Valid Bearer token required", 401)

    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError):
        return JSONResponse(status_code=exc.status, content={"error": exc.as_dict()})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        # Pydantic input/context can contain private data. Return field paths only.
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error",
            "message": "Invalid request fields", "retryable": False,
            "fields": [".".join(str(x) for x in e["loc"]) for e in exc.errors()]}})

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, _exc: Exception):
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error",
                             "message": "Internal processing error", "retryable": False}})

    @app.get("/health/live", tags=["health"])
    def live() -> dict:
        return {"status": "alive", "version": "0.1.0"}

    @app.get("/health/ready", tags=["health"])
    def ready() -> dict:
        result = rt.db.readiness()
        if not result["ready"]:
            raise AppError("database_not_ready", "Run findyou init-db or inspect doctor output", 503)
        return result

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(authorize)])
    Limit = Annotated[int, Query(ge=1, le=100)]
    Offset = Annotated[int, Query(ge=0)]

    @router.get("/capabilities", tags=["system"])
    def capabilities() -> dict:
        return {"text_import": True, "memory_confirmation": True, "fts5": True,
                "cloud_model": rt.gateway.status(), "bilibili": rt.bili.status(),
                "platform_write": False, "autonomous_social": False, "vector_search": False,
                "autonomous_read_draft": True, "normalized_social": True,
                "contact_memory": True, "versioned_roles": True,
                "ocr": False, "asr": False, "vision_extraction": False,
                "business_pages": (Path("frontend/dist") / "index.html").is_file(), "life": True}

    @router.post("/imports", status_code=202, tags=["content"])
    def submit_import(body: ImportRequest) -> dict:
        return rt.queue.enqueue("import", body.model_dump())

    @router.post("/imports/file", status_code=202, tags=["content"])
    async def upload(file: UploadFile = File(...), visibility: Scope = "private") -> dict:
        name = file.filename or "upload.txt"
        suffix = name.rsplit(".", 1)[-1].lower()
        if suffix not in {"txt", "md", "json"}:
            raise AppError("unsupported_format", "Only UTF-8 .txt, .md and canonical .json imports are available", 415)
        data = await file.read(rt.settings.max_import_chars * 4 + 1)
        if len(data) > rt.settings.max_import_chars * 4:
            raise AppError("import_too_large", "Import exceeds maximum size", 413)
        try:
            text = data.decode("utf-8-sig")
            if suffix == "json":
                body = ImportRequest.model_validate(json.loads(text))
            else:
                body = ImportRequest(title=name[:300], text=text, visibility=visibility,
                                     format="markdown" if suffix == "md" else "text")
        except (ValueError, UnicodeError):
            raise AppError("invalid_import", "Expected UTF-8 text or a canonical ImportRequest JSON object", 422) from None
        return rt.queue.enqueue("import", body.model_dump())

    @router.get("/content", tags=["content"])
    def content_list(limit: Limit = 20, offset: Offset = 0) -> dict:
        return rt.storage.list_content(limit=limit, offset=offset)

    @router.get("/content/{content_id}", tags=["content"])
    def content_detail(content_id: str) -> dict:
        return rt.storage.get_content(content_id)

    @router.post("/content/{content_id}/extract", status_code=202, tags=["memory"])
    def extract(content_id: str) -> dict:
        content = rt.storage.get_content(content_id)
        if content["platform"].startswith("life:"):
            raise AppError("life_source", "Use the authorized life extraction workflow.", 409)
        return rt.queue.enqueue("extract", {"content_id": content_id})

    @router.patch("/content/{content_id}/visibility", tags=["content"])
    def content_visibility(content_id: str, body: VisibilityRequest) -> dict:
        return rt.storage.update_content_visibility(content_id, body.visibility)

    @router.delete("/content/{content_id}", tags=["content"])
    def content_delete(content_id: str) -> dict:
        return rt.storage.delete_content(content_id)

    @router.get("/memories", tags=["memory"])
    def memories(limit: Limit = 20, offset: Offset = 0, status: str | None = None) -> dict:
        return rt.storage.list_memories(limit=limit, offset=offset, status=status)

    @router.get("/memories/search", tags=["memory"])
    def memory_search(q: Annotated[str, Query(min_length=1, max_length=1000)], scope: Scope = "private") -> dict:
        return {"items": rt.storage.retrieve(q, scope=scope)}

    @router.get("/memories/{memory_id}", tags=["memory"])
    def memory_detail(memory_id: str) -> dict:
        return rt.storage.get_memory(memory_id)

    @router.post("/memories/{memory_id}/confirm", tags=["memory"])
    def confirm(memory_id: str) -> dict:
        if rt.life.is_life_memory(memory_id):
            raise AppError("life_version_required", "Confirm the reviewed version in the life workspace.", 409)
        return rt.storage.confirm_memory(memory_id)

    @router.patch("/memories/{memory_id}", tags=["memory"])
    def correct(memory_id: str, body: CorrectionRequest) -> dict:
        return rt.storage.correct_memory(memory_id, body.text, visibility=body.visibility)

    @router.post("/memories/{memory_id}/deactivate", tags=["memory"])
    def deactivate(memory_id: str) -> dict:
        return rt.storage.deactivate_memory(memory_id)

    @router.delete("/memories/{memory_id}", tags=["memory"])
    def delete(memory_id: str) -> dict:
        return rt.storage.delete_memory(memory_id)

    @router.post("/conversations", tags=["conversations"])
    def conversation_create(body: ConversationRequest) -> dict:
        return rt.services.create_conversation(**body.model_dump())

    @router.get("/conversations", tags=["conversations"])
    def conversations(limit: Limit = 20, offset: Offset = 0) -> dict:
        return rt.services.page(Conversation, limit, offset, owner_id=rt.settings.owner_id)

    @router.get("/conversations/{conversation_id}", tags=["conversations"])
    def conversation_detail(conversation_id: str) -> dict:
        return rt.services.conversation(conversation_id)

    @router.post("/chat/simulate", tags=["chat"])
    def simulate(body: SimulationRequest) -> dict:
        return rt.services.simulate(**body.model_dump())

    @router.get("/connectors/bilibili", tags=["bilibili"])
    def bili_status() -> dict:
        return rt.bili.status()

    @router.post("/connectors/bilibili/{operation}", status_code=202, tags=["bilibili"])
    def bili_operation(operation: Literal["status", "login_start", "login_poll", "search", "recommend", "video_info", "subtitle", "comments"],
                       body: BiliRequest) -> dict:
        return rt.queue.enqueue("bilibili", {"operation": operation, "params": body.params})

    @router.get("/jobs", tags=["jobs"])
    def jobs(limit: Limit = 20, offset: Offset = 0) -> dict:
        return rt.services.page(Job, limit, offset)

    @router.get("/jobs/{job_id}", tags=["jobs"])
    def job_detail(job_id: str) -> dict:
        return rt.queue.get(job_id)

    @router.get("/actions", tags=["actions"])
    def actions(limit: Limit = 20, offset: Offset = 0) -> dict:
        page = rt.services.page(Action, limit, offset, owner_id=rt.settings.owner_id)
        page["items"] = [rt.services.visible_action(item) for item in page["items"]]
        return page

    @router.post("/actions/{action_id}/{operation}", tags=["actions"])
    def unavailable_write(action_id: str, operation: str) -> dict:
        raise AppError("platform_write_disabled", "Publishing and approval execution are unavailable in this release", 409)

    @router.get("/agent/status", tags=["system"])
    def agent_status() -> dict:
        return {"paused": rt.services.paused(), "platform_write": False, "autonomous_social": False,
                "autonomous_read_draft": True,
                "model": rt.gateway.status(), "worker": "Run separate findyou worker process"}

    @router.post("/agent/stop", tags=["system"])
    def stop() -> dict:
        return rt.services.set_paused(True)

    @router.post("/agent/resume", tags=["system"])
    def resume() -> dict:
        return rt.services.set_paused(False)

    @router.get("/metrics", tags=["system"])
    def metrics() -> dict:
        from sqlalchemy import func, select
        with rt.db.sessions() as session:
            calls, inputs, outputs, latency = session.execute(select(func.count(LLMCall.id),
                func.coalesce(func.sum(LLMCall.input_tokens), 0), func.coalesce(func.sum(LLMCall.output_tokens), 0),
                func.coalesce(func.avg(LLMCall.latency_ms), 0))).one()
            return {"model_calls": calls, "input_tokens": inputs, "output_tokens": outputs,
                    "mean_latency_ms": latency, "cost": None,
                    "note": "Token totals include simulated calls; monetary cost is not estimated",
                    "failed_calls": session.scalar(select(func.count()).select_from(LLMCall).where(LLMCall.status == "failed"))}

    router.include_router(build_social_router(rt))
    router.include_router(build_life_router(rt))
    app.include_router(router)
    ui = Path("frontend/dist")
    if (ui / "index.html").is_file():
        app.mount("/life", StaticFiles(directory=ui, html=True), name="life")

        @app.get("/", include_in_schema=False)
        def home():
            return RedirectResponse("/life/")
    return app


def app_factory() -> FastAPI:
    return create_app()
