from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

Scope = Literal["private", "public"]


class ImportRequest(BaseModel):
    title: str = Field(default="Untitled", min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=200000)
    format: Literal["text", "markdown", "json"] = "text"
    platform: str = Field(default="local", max_length=40)
    external_id: str | None = Field(default=None, max_length=200)
    source_url: str | None = Field(default=None, max_length=2000)
    visibility: Scope = "private"
    authorship: Literal["owner", "external"] = "owner"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResult(BaseModel):
    data: dict[str, Any]
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    simulated: bool = False
    raw_usage: dict[str, Any] = Field(default_factory=dict)
    usage_known: bool = False
    response_model: str | None = None
    llm_call_id: str | None = None


class LLMGateway(Protocol):
    def generate(self, operation: str, system: str, payload: dict) -> ModelResult: ...
    def status(self) -> dict: ...


class PlatformAdapter(Protocol):
    def execute(self, operation: str, params: dict) -> dict: ...
    def status(self) -> dict: ...


class Retrieval(Protocol):
    def retrieve(self, query: str, scope: Scope = "private", limit: int = 8) -> list[dict]: ...
