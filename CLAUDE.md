# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NexusDownloader is a multi-threaded downloader for Nexus Mods collections, built as a faster alternative to Vortex's built-in collection downloader. Python 3.12+ with a PySide6 GUI and a CLI fallback. Cross-platform (Windows/Linux) with PyInstaller-based packaging.

## Common Commands

### Running the application
```bash
# GUI (preferred entry point — sets up sys.path for src/)
python run_gui.py

# CLI download
cd src && python -m loadcollection --json "/path/to/collection.json" --gamefolder "skyrimspecialedition" --maxthreads 15

# CLI endorse-only (24h after download)
cd src && python -m loadcollection --endorseonly --json "/path/to/collection.json" --gamefolder "skyrimspecialedition"
```

Note: `loadcollection.py` uses relative imports (`from . import download, endorse`) so it must be run as a module from the project root or via `run_gui.py`, not as a bare script from inside `src/`.

### Build / packaging
```bash
make dev-setup          # installs requirements.txt + requirements-build.txt
make build              # cross-platform: python build.py
make build-exe          # PyInstaller only, skip installer
make clean              # python build.py --clean-only
./build_linux.sh        # Linux AppImage / .tar.gz
build_windows.bat       # Windows .exe + NSIS installer (requires NSIS on PATH)
```

The PyInstaller spec is `nexusdownloader.spec` (also `gui.spec`). Build artifacts land in `dist/NexusDownloader/`.

### Tests / lint
`pytest`, `black`, and `flake8` are listed in requirements but no test suite is currently committed under the repo. There is no CI configured. If adding tests, place them under `tests/` and run with `pytest`.

## Architecture

### Entry points and module layout
- `run_gui.py` (project root) — the canonical launcher. Inserts `src/` into `sys.path` then calls `gui.main_window.main()`. Always launch the GUI through this; `src/gui/main_window.py` itself does sibling-package imports that assume `src/` is on the path.
- `src/loadcollection.py` — CLI orchestrator and the *only* module that actually drives a download/endorse session end-to-end. The GUI's `DownloadWorkerThread` shells out and parses its stdout (looks for `PROGRESS: x/y` and `0000\tCompleted...` lines — see `src/loadcollection.py:103-108`). Don't change those print formats without updating the GUI parser in `src/gui/main_window.py`.
- `src/download.py`, `src/endorse.py` — Nexus API calls. Both expose a `set_*_logger()` setter so `loadcollection` can inject the unified logger.

### Configuration system
Two coexisting configs — be aware of both:
1. **Modern**: `src/config/config_manager.py` — dataclass-based `AppConfig` with sections (`NexusApiConfig`, `VortexConfig`, `DownloadConfig`, `LoggingConfig`, `SecurityConfig`, `UIPreferencesConfig`). Versioned (`ConfigVersion` enum), supports migration, atomic writes via `utils.file_operations.AtomicFileWriter`, secure API key storage via `utils.security.SecureConfig` (keyring + Fernet).
2. **Legacy fallback**: `src/config.py` — flat module-level `CONFIG` object. `download.py` constructs a `LegacyConfig` shim around the new system to keep `CONFIG.AccessControl.NexusAPIKey` and `CONFIG.VortexSettings.DownloadsFolderRoot` working. If the new config import fails, it falls back to the old `config.get_config()`, then to a hardcoded minimal config.

The actual config file is `src/config.json` (current version `1.1`). A separate `config.json` at the repo root also exists — the loader resolves paths relative to the working directory, which historically caused bugs; prefer the `src/config.json` location.

### Logging — unified system
`src/utils/unified_logging.py` is authoritative. Use `get_logger(name)` or `create_operation_logger(operation_type, game_domain)` rather than `logging.getLogger()` directly. The module:
- Detects project root regardless of CWD (important because GUI vs CLI launch from different dirs).
- Uses a `CustomRotatingFileHandler` that writes backups as `nexusdownloader_001.log` (not `.log.1`).
- Standardized files in `logs/`: `nexusdownloader.log` (main), `_download.log`, `_install.log`, `_errors.log`, `_performance.log`, plus per-operation `_[operation]_[game]_[timestamp].log`.

There is a custom `VERBOSE` log level (15, between DEBUG and INFO) added in `loadcollection.py` — call `logger.verbose(...)`.

`src/utils/logging_config.py` and `src/utils/async_logger.py` predate the unified system; new code should go through `unified_logging`.

### GUI structure (`src/gui/`)
- `main_window.py` — `QMainWindow` with tabs. `DownloadWorkerThread(QThread)` spawns `loadcollection` as a subprocess and parses stdout into Qt signals (`overall_progress_updated`, `active_downloads_updated`, `file_completed`, `log_message_received`).
- `progress_monitor.py` — live progress widget driven by signals from the worker thread.
- `install_tab.py` — separate tab for FOMOD installation (uses `utils.fomod_installer`, runs in `InstallWorkerThread`).
- `settings_dialog.py` — edits `AppConfig` via `ConfigManager`.

### Mod installation pipeline (separate from download)
After downloads finish, the install tab runs FOMOD installation:
- `src/utils/archive_handler.py` — extracts ZIP/7Z/RAR. Has known race conditions with external 7zip/WinRAR subprocesses; the handler waits for files to materialize before returning (see `_extract_7z_external`, `_extract_winrar_external`).
- `src/utils/fomod_installer.py` — reads `collection.json` install instructions, applies FOMOD choices, validates that the final mod folder is non-empty (`_validate_mod_installation`).
- `src/utils/vortex_config.py` — reads Vortex's own state files (incl. optional lz4-compressed) to discover game install/staging/download paths.

### Threading model
- Downloads: `concurrent.futures.ThreadPoolExecutor` in `loadcollection.main`, default 10 threads (config allows up to 50). Counters use module-level globals + `threading.Lock`.
- GUI: separate `QThread` subclasses per long-running op (`DownloadWorkerThread`, `InstallWorkerThread`); never block the Qt event loop.

## Key conventions and gotchas

- **`print()` is sometimes load-bearing.** `loadcollection.py` uses raw `print()` with `sys.stdout.flush()` to emit GUI-parseable progress lines. Don't replace these with logger calls without updating the GUI subprocess parser.
- **Working directory matters.** `unified_logging` resolves the project root to find `logs/`. The CLI expects to run from project root (so `from . import download` resolves), the GUI sets `sys.path` from `run_gui.py`. If imports fail, check CWD and `sys.path` before changing the import.
- **Config has two roots.** Both `./config.json` and `./src/config.json` exist; the latter is the one the app actually reads. Don't accidentally edit the wrong one.
- **Defensive imports everywhere.** `download.py`, `gui/main_window.py`, `utils/security.py`, etc. all wrap imports in try/except with fallback shims (`PHASE1_AVAILABLE`, `KEYRING_AVAILABLE`, `CRYPTO_AVAILABLE`, `LegacyConfig`, `MinimalConfig`). When adding a new dep, follow the same pattern or the build will silently degrade rather than fail.
- **Vortex must be closed during downloads.** The GUI checks via `psutil` and prompts the user; the CLI does not. This is intentional — Vortex locks the downloads folder.
- **API key handling.** Real keys live in OS keyring when available; only blank string in `config.json`. Never write a key into `config.json` directly. `mykeys.log` in the repo root is gitignored and used for local testing only.
- **Build version is hardcoded.** Update `build.py`, `installer/windows/nexusdownloader_installer.nsi`, and `installer/linux/install.sh` together when bumping versions.
