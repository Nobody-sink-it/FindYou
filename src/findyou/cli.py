import hashlib
import json
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

import typer

from findyou.config import Settings
from findyou.errors import AppError

app = typer.Typer(no_args_is_help=True, help="FindYou local administration. Platform publishing is unavailable.")


def emit(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def runtime():
    from findyou.runtime import Runtime
    return Runtime(Settings())


@app.command()
def init_env():
    """Create .env once; never print the management token."""
    target = Path(".env")
    if target.exists():
        raise typer.BadParameter(".env already exists; edit it in place")
    template = Path(".env.example")
    text = template.read_text(encoding="utf-8") if template.exists() else "FINDYOU_API_TOKEN=replace-me\n"
    target.write_text(text.replace("FINDYOU_API_TOKEN=replace-me", "FINDYOU_API_TOKEN=" + secrets.token_urlsafe(32)), encoding="utf-8")
    emit({"created": str(target.resolve()), "note": "Token saved locally; edit model key before live model tests"})


@app.command()
def init_db():
    rt = runtime()
    rt.db.initialize()
    emit(rt.db.readiness())


@app.command()
def doctor():
    rt = runtime()
    emit({"database": rt.db.readiness(), "model": rt.gateway.status(), "bilibili": rt.bili.status(),
          "auth_configured": bool(rt.settings.api_token and rt.settings.api_token.get_secret_value()),
          "platform_write": False})


@app.command()
def serve():
    import uvicorn
    cfg = Settings()
    if cfg.host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("This release binds only to a loopback address")
    uvicorn.run("findyou.api:app_factory", factory=True, host=cfg.host, port=cfg.port, access_log=False)


@app.command()
def worker(once: bool = False):
    rt = runtime()
    if once:
        emit({"processed": rt.worker.run_once()})
    else:
        rt.worker.run()


@app.command("import")
def import_file(path: Path, public: bool = False):
    from findyou.contracts import ImportRequest
    rt = runtime()
    if path.suffix.lower() not in {".md", ".txt", ".json"}:
        raise typer.BadParameter("Only .md, .txt and canonical .json are supported")
    if path.stat().st_size > rt.settings.max_import_chars * 4:
        raise typer.BadParameter("File too large")
    text = path.read_text(encoding="utf-8-sig")
    body = ImportRequest.model_validate(json.loads(text)) if path.suffix == ".json" else ImportRequest(
        title=path.name, text=text, format="markdown" if path.suffix == ".md" else "text",
        visibility="public" if public else "private")
    emit(rt.queue.enqueue("import", body.model_dump()))


@app.command()
def extract(content_id: str):
    rt = runtime()
    rt.storage.get_content(content_id)
    emit(rt.queue.enqueue("extract", {"content_id": content_id}))


@app.command()
def memories(status: str | None = None):
    emit(runtime().storage.list_memories(status=status))


@app.command()
def confirm(memory_id: str):
    emit(runtime().storage.confirm_memory(memory_id))


@app.command()
def correct(memory_id: str, text: str):
    emit(runtime().storage.correct_memory(memory_id, text))


@app.command()
def search(query: str, public: bool = False):
    emit({"items": runtime().storage.retrieve(query, scope="public" if public else "private")})


@app.command()
def job(job_id: str):
    emit(runtime().queue.get(job_id))


@app.command()
def jobs():
    from findyou.ops import Job
    rt = runtime()
    emit(rt.services.page(Job, 20, 0))


@app.command()
def content(content_id: str | None = None):
    rt = runtime()
    emit(rt.storage.get_content(content_id) if content_id else rt.storage.list_content())


@app.command()
def conversation(peer: str = "test-peer", thread: str = "test-thread", public: bool = True):
    emit(runtime().services.create_conversation(peer_id=peer, thread_key=thread, scope="public" if public else "private"))


@app.command()
def chat(conversation_id: str, question: str):
    emit(runtime().services.simulate(conversation_id, question))


@app.command()
def bili(operation: str, params: str = "{}"):
    """Queue only an explicit read or login operation. JSON params never become shell text."""
    if operation not in {"status", "login_start", "login_poll", "search", "recommend", "video_info", "subtitle", "comments"}:
        raise typer.BadParameter("Unsupported/read-only operation")
    body = json.loads(params)
    if not isinstance(body, dict):
        raise typer.BadParameter("params must be a JSON object")
    emit(runtime().queue.enqueue("bilibili", {"operation": operation, "params": body}))


@app.command()
def pause():
    emit(runtime().services.set_paused(True))


@app.command("capture")
def capture_video(video: str, subtitle: bool = False):
    from findyou.capture import video_identifier
    emit(runtime().queue.enqueue("capture", {"video_id": video_identifier(video),
        "page": 1, "sort": "hot", "include_subtitle": subtitle}))


@app.command("agent-once")
def agent_once(video: str = "", keyword: str = "", targets: int = 2):
    """Queue a bounded discover/understand/decide/draft run. Never publishes."""
    from findyou.capture import video_identifier
    from findyou.social_api import CycleRequest
    rt = runtime()
    config = CycleRequest(video_ids=[video_identifier(video)] if video else [],
                          keyword=keyword or None, max_targets=targets).model_dump()
    config["role_version_id"] = rt.identity.select_role()["id"]
    emit(rt.queue.enqueue("agent_cycle", {"config": config}, max_attempts=1))


@app.command("agent-runs")
def agent_runs():
    emit(runtime().agent.runs())


@app.command("agent-drafts")
def agent_drafts():
    emit(runtime().agent.drafts())


@app.command("contacts")
def contacts_list():
    emit(runtime().identity.list_contacts())


@app.command()
def resume():
    emit(runtime().services.set_paused(False))


@app.command()
def backup(output: Path):
    """Consistent SQLite online backup + raw sources; excludes keys and Bilibili credentials."""
    if output.exists():
        raise typer.BadParameter("Output already exists")
    rt = runtime()
    if rt.db.readiness().get("schema_revision") not in {
        "0001_initial", "0002_proposal_identity", "0003_social_agent", "0004_life_model"
    }:
        raise typer.BadParameter("Database not initialized")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        snap = Path(scratch) / "findyou.db"
        with rt.db.engine.connect() as lock:
            # Serializes source deletion/import for the whole DB+raw snapshot. Use a separate
            # reader for sqlite backup(), which must not run on its own write transaction.
            lock.exec_driver_sql("BEGIN IMMEDIATE")
            with rt.db.engine.connect() as connection, closing(sqlite3.connect(snap)) as destination:
                connection.connection.driver_connection.backup(destination)
            paths = {}
            with closing(sqlite3.connect(snap)) as snapshot:
                for old_path, expected_hash in snapshot.execute("SELECT raw_path, sha256 FROM source_snapshots"):
                    path = Path(old_path).resolve()
                    if not path.is_relative_to(rt.settings.data_dir.resolve() / "raw") or not path.is_file():
                        raise typer.BadParameter("Referenced raw source is missing or outside the data directory")
                    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                        raise typer.BadParameter("Referenced raw source failed its SHA-256 check")
                    paths[old_path] = path.relative_to(rt.settings.data_dir.resolve()).as_posix()
            with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(snap, "findyou.db")
                for old_path, relative in paths.items():
                    archive.write(old_path, relative)
                assets = rt.settings.data_dir / "assets"
                if assets.exists():
                    for item in assets.rglob("*"):
                        if item.is_file() and not item.is_symlink():
                            archive.write(item, item.relative_to(rt.settings.data_dir).as_posix())
                archive.writestr("manifest.json", json.dumps({"format": 1, "credentials_included": False,
                                                              "snapshot_paths": paths}))
            lock.rollback()
    emit({"backup": str(output.resolve()), "credentials_included": False})


@app.command()
def restore(archive: Path):
    """Restore into an EMPTY local data directory only; does not overwrite existing data."""
    cfg = Settings()
    if cfg.database_url:
        raise typer.BadParameter("Restore requires the default local database path")
    root = cfg.data_dir.resolve()
    if root.exists() and any(root.iterdir()):
        raise typer.BadParameter("Stop processes and choose an empty FINDYOU_DATA_DIR; existing data is never overwritten")
    with tempfile.TemporaryDirectory() as scratch, zipfile.ZipFile(archive) as source:
        stage = Path(scratch)
        if len(source.infolist()) > 10000 or sum(i.file_size for i in source.infolist()) > 1024 ** 3:
            raise typer.BadParameter("Backup exceeds restore limits")
        for item in source.infolist():
            target = (stage / item.filename).resolve()
            if not target.is_relative_to(stage.resolve()) or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise typer.BadParameter("Unsafe backup path")
            parts = Path(item.filename).parts
            if not parts or (parts[0] not in {"raw", "assets"} and item.filename not in {"findyou.db", "manifest.json"}):
                raise typer.BadParameter("Unexpected backup entry")
        source.extractall(stage)
        try:
            manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("format") != 1 or not isinstance(manifest.get("snapshot_paths"), dict):
                raise ValueError
        except (ValueError, OSError):
            raise typer.BadParameter("Invalid backup manifest") from None
        with closing(sqlite3.connect(f"file:{(stage / 'findyou.db').as_posix()}?mode=rw", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise typer.BadParameter("Backup database failed integrity check")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise typer.BadParameter("Backup has invalid foreign keys")
            for old_path, expected_hash in db.execute("SELECT raw_path, sha256 FROM source_snapshots").fetchall():
                relative = manifest["snapshot_paths"].get(old_path)
                if not isinstance(relative, str) or not relative.startswith("raw/"):
                    raise typer.BadParameter("Missing source path mapping")
                mapped = (root / relative).resolve()
                if not mapped.is_relative_to(root / "raw") or not (stage / relative).is_file():
                    raise typer.BadParameter("Invalid or missing raw source")
                if hashlib.sha256((stage / relative).read_bytes()).hexdigest() != expected_hash:
                    raise typer.BadParameter("Raw source SHA-256 mismatch")
                db.execute("UPDATE source_snapshots SET raw_path=? WHERE raw_path=?", (str(mapped), old_path))
            db.commit()
        root.mkdir(parents=True, exist_ok=True)
        for name in ("findyou.db", "raw", "assets"):
            if (stage / name).exists():
                shutil.move(str(stage / name), str(root / name))
    emit({"restored": str(root), "note": "Run init-db to migrate; configure model and re-login separately"})


def main():
    try:
        app()
    except AppError as exc:
        emit({"error": exc.as_dict()})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
