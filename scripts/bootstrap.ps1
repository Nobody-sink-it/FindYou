param([switch]$SkipBilibili)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
Push-Location $workspace
try {
    if (!(Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
    }
    $runtimeDir = Join-Path $workspace '.venv/sqlite-runtime'
    New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
    $sqliteDll = Join-Path $runtimeDir 'sqlite3.dll'
    if (!(Test-Path -LiteralPath $sqliteDll)) {
        $sqliteArchive = Join-Path $runtimeDir 'sqlite.zip'
        Invoke-WebRequest 'https://www.sqlite.org/2026/sqlite-dll-win-x64-3510300.zip' -OutFile $sqliteArchive
        Expand-Archive -LiteralPath $sqliteArchive -DestinationPath $runtimeDir -Force
    }
    # Load only the venv-local official DLL before CPython imports its sqlite extension.
    # Does not replace Anaconda/system DLLs or modify any global environment.
    $sitecustomize = Join-Path $workspace '.venv/Lib/site-packages/sitecustomize.py'
    $customization = @'
# FindYou isolated SQLite runtime bootstrap.
import ctypes
import pathlib
import sys
_findyou_sqlite = pathlib.Path(sys.prefix) / "sqlite-runtime" / "sqlite3.dll"
if sys.platform == "win32" and _findyou_sqlite.exists():
    _findyou_sqlite_handle = ctypes.WinDLL(str(_findyou_sqlite))
'@
    if ((Test-Path -LiteralPath $sitecustomize) -and !(Get-Content -Raw $sitecustomize).Contains('# FindYou isolated SQLite runtime bootstrap.')) {
        throw 'Existing sitecustomize.py belongs to another setup; refusing to overwrite'
    }
    [IO.File]::WriteAllText($sitecustomize, $customization, [Text.UTF8Encoding]::new($false))
    & .venv/Scripts/python.exe -c "import sqlite3; assert sqlite3.sqlite_version_info >= (3,51,3); print('SQLite',sqlite3.sqlite_version)"
    if ($LASTEXITCODE -ne 0) { throw 'Patched Python SQLite runtime verification failed' }
    & .venv/Scripts/python.exe -m pip install -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency install failed' }
    & .venv/Scripts/python.exe -m pip install --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw 'Dependency install failed' }
    if (!$SkipBilibili) {
        if (!(Test-Path -LiteralPath '.venv-bili/Scripts/python.exe')) { python -m venv .venv-bili }
        & .venv-bili/Scripts/python.exe -m pip install -r requirements-bilibili.lock
        if ($LASTEXITCODE -ne 0) { throw 'Bilibili dependency install failed' }
    }
    if (!(Test-Path -LiteralPath '.env')) { & .venv/Scripts/python.exe -m findyou.cli init-env }
    & .venv/Scripts/python.exe -m findyou.cli init-db
    if ($LASTEXITCODE -ne 0) { throw 'Database initialization failed' }
} finally { Pop-Location }
