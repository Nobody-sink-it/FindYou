# Shared implementation contracts

Root owns config.py, contracts.py, errors.py, ops.py, API, CLI, jobs, worker, context and integration tests. Do not edit another owner's files.

## Storage executor ownership
Own db.py, storage.py, content_models.py, migrations/, alembic.ini, tests/test_storage.py. Imports may use findyou.config.Settings, contracts.ImportRequest, errors.AppError. All public methods raise AppError for expected failures and return JSON-serializable dictionaries. Database timestamps use naive UTC internally, ISO UTC externally.

`db.Base` is SQLAlchemy DeclarativeBase. `db.Database(settings)` exposes engine and sessions (sessionmaker). `initialize()` applies Alembic migrations, seeds local owner; imports findyou.ops so operational metadata is included. `readiness()` reports {ready, sqlite_version, journal_mode, schema_revision}. Enforce >=3.51.3 BEFORE enabling WAL (DELETE explicit override for portable tests; never silent fallback), foreign_keys=ON, busy_timeout. Alembic upgrade idempotent; package migration resources so installed wheel works. `db.new_id()` UUID str; `db.utcnow()` naive UTC.

`storage.Storage(settings, database=None)` exposes .db, and methods:
- import_content(payload: ImportRequest) -> {id,title,duplicate,blocks:[{id,text,...}],...}; raw files under data_dir, sha256; stable dedup includes owner/platform/authorship; source URL is metadata ONLY, never fetched. Owner/source scope immutable by duplicate import. external content must not become owner memory.
- list_content(limit=20,offset=0)->{items,total,limit,offset}; get_content(id)-> detail with blocks, snapshots, metadata.
- propose_facts(content_id, facts:list[dict])->list[dict], each fact {text,evidence_block_ids:list[str],kind?}; reject nonexistent/foreign block evidence, empty evidence; only owner content can propose owner facts; always candidate.
- list_memories(limit=20,offset=0,status=None)->page; get_memory(id)->dict
- confirm_memory(id)->dict; correct_memory(id,text,visibility=None)->dict (preserve prior version/evidence; old approval invalidated, candidate until confirm); deactivate_memory(id)->dict; delete_memory(id)->dict
- retrieve(query,scope='private',limit=8)->list[{id,text,visibility,evidence:[{block_id,content_id,title,source_url,text}]}], confirmed active facts only; public also requires public source/evidence (all evidence must be eligible), return only eligible fields. FTS5+short query fallback; indexing kept consistent on changes/deletes. Content source fields included in queries to match concepts not literal full questions.
- update_content_visibility(id,visibility)->dict; delete_content(id)->dict (invalidate affected facts and purge indexed text; raw files removed only if no shared references).

Root operational tables all extend db.Base, owned solely by root. Initial migration may use Base.metadata.create_all(op.get_bind()) after import of content_models and ops; subsequent migrations explicit. Storage owner may decide its own content table fields but must expose the above exact methods. No need 21 tables exactly.

## Connector/model executor ownership
Own models_gateway.py, bilibili.py, tests/test_gateway.py, tests/test_bilibili.py, docs/CONNECTORS.md. Reuse Settings, AppError, ModelResult. Root performs persistence and writes LLMCall metadata.

`models_gateway.Gateway(settings, transport=None)` .generate(operation,system,payload)->ModelResult and .status()->{configured,provider,model,simulated,...}. Ops extract -> {facts:[{text,evidence_block_ids,kind}]}; reply -> {answer,evidence_ids:list[str]}; summarize -> {summary}. Fake explicitly simulated and deterministic for tests. True OpenAI-compatible httpx requests, JSON schema validated, output limits, sanitized errors, no secret/body logs, no silent provider fallback; test MockTransport. Model must not see credentials.

`bilibili.BilibiliAdapter(settings, runner=None)` .execute(operation,params)->dict and .status()->dict. Allowed ops status,login_start,login_poll,search,video_info,subtitle,comments; no watch/history writes or generic commands. Inspect pinned bili-agent==1.2.1 CLI source/help/schema before selecting exact args; install into .venv-bili (do not modify root venv), set FINDYOU_BILI_PYTHON for app; no actual login yet. CLI JSON response parser, subprocess list args shell=False, dedicated BILI_CLI_HOME, operation-specific timeouts (login_poll shorter than lease), credential/QR poll errors sanitized, dependency version check. All returned results must JSON-serializable. Narrow validated parameters, capability disabled/write errors. Agent can perform anonymous public reads if supported and record verified reads. Root persists connector output and external content normalization.
