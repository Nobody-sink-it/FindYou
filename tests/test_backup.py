import json
import zipfile

from typer.testing import CliRunner

from findyou.cli import app
from findyou.config import Settings
from findyou.contracts import ImportRequest
from findyou.runtime import Runtime


def test_backup_restore_portable_and_credentials_excluded(tmp_path, monkeypatch):
    original = tmp_path / "original"
    monkeypatch.setenv("FINDYOU_DATA_DIR", str(original))
    monkeypatch.setenv("FINDYOU_SQLITE_JOURNAL_MODE", "DELETE")
    cfg = Settings(_env_file=None, data_dir=original, sqlite_journal_mode="DELETE", llm_provider="fake")
    rt = Runtime(cfg)
    rt.db.initialize()
    content = rt.storage.import_content(ImportRequest(text="Memory for backup", visibility="public"))
    cred = original / "credentials" / "bilibili"
    cred.mkdir(parents=True)
    (cred / "credential.json").write_text('"never-back-up-this"')
    archive = tmp_path / "backup.zip"
    result = CliRunner().invoke(app, ["backup", str(archive)])
    assert result.exit_code == 0, repr(result.exception)
    with zipfile.ZipFile(archive) as source:
        assert all("credentials" not in path for path in source.namelist())
        assert "never-back-up-this" not in source.read("manifest.json").decode()
    target = tmp_path / "restored"
    monkeypatch.setenv("FINDYOU_DATA_DIR", str(target))
    restored = CliRunner().invoke(app, ["restore", str(archive)])
    assert restored.exit_code == 0, repr(restored.exception)
    other = Runtime(cfg.model_copy(update={"data_dir": target}))
    other.db.initialize()
    raw_path = other.storage.get_content(content["id"])["snapshots"][0]["raw_path"]
    from pathlib import Path
    assert Path(raw_path).is_relative_to(target)
    assert Path(raw_path).exists()
    assert other.storage.delete_content(content["id"]) == {"id": content["id"], "deleted": True}
    assert not Path(raw_path).exists()
    assert Path(content["snapshots"][0]["raw_path"]).exists()
    assert CliRunner().invoke(app, ["restore", str(archive)]).exit_code != 0
    other.db.engine.dispose()
    rt.db.engine.dispose()


def test_restore_rejects_traversal_before_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDYOU_DATA_DIR", str(tmp_path / "target"))
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("../outside.txt", "bad")
        out.writestr("manifest.json", json.dumps({"format": 1}))
    assert CliRunner().invoke(app, ["restore", str(archive)]).exit_code != 0
    assert not (tmp_path / "outside.txt").exists()
