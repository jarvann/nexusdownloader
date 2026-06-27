"""
FOMOD installer implementation for NexusDownloader.

Handles installation of mods using FOMOD (Fallout Mod Manager) configuration
based on collection.json instructions, mimicking Vortex behavior.
"""

import os
import shutil
import subprocess
import tempfile
import time
import logging
import threading
import concurrent.futures
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union, Any, Callable
from dataclasses import dataclass, field
from enum import Enum

from .archive_handler import get_archive_handler, _external_tools
from .unified_logging import get_logger


class InstallResult(Enum):
    """Installation result status."""
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    PARTIAL = "partial"


@dataclass
class FomodChoice:
    """Represents a FOMOD installation choice."""
    name: str
    idx: int
    description: Optional[str] = None
    image: Optional[str] = None
    
    
@dataclass
class FomodGroup:
    """Represents a FOMOD installation group."""
    name: str
    choices: List[FomodChoice] = field(default_factory=list)
    selection_type: str = "SelectExactlyOne"  # SelectExactlyOne, SelectAtLeastOne, SelectAtMostOne, SelectAny


@dataclass 
class FomodOption:
    """Represents a FOMOD installation option/step."""
    name: str
    groups: List[FomodGroup] = field(default_factory=list)
    

@dataclass
class FomodInstallInfo:
    """Installation information for a mod."""
    mod_name: str
    source_archive: Path
    install_path: Path
    choices: Dict[str, Any]
    fomod_type: str = "fomod"
    

@dataclass
class InstallationResult:
    """Result of mod installation."""
    mod_name: str
    status: InstallResult
    installed_files: List[Path] = field(default_factory=list)
    error_message: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


class FomodInstaller:
    """Handles FOMOD-based mod installation."""
    
    def __init__(self, staging_path: Union[str, Path], logger: Optional[logging.Logger] = None,
                 temp_root: Optional[str] = None):
        """Initialize FOMOD installer.

        ``temp_root`` overrides where extraction scratch dirs are created. Blank/None
        uses the system ``%TEMP%``; point it at a roomy drive when ``%TEMP%`` is a
        small RAM disk (avoids "No space left on device" during extraction).
        """
        self.staging_path = Path(staging_path)
        # Use the provided logger or get the unified install logger
        if logger is None:
            self.logger = get_logger('install')
        else:
            self.logger = logger
        self.archive_handler = get_archive_handler(self.logger)

        # Resolve the scratch/temp root override (None -> system default).
        self._temp_root = self._resolve_temp_root(temp_root)

        # Create temporary directory for extractions
        self.temp_dir = Path(tempfile.mkdtemp(prefix="nexusdownloader_install_", dir=self._temp_root))
        self.logger.debug(f"Created temp directory: {self.temp_dir}")

        # Space-aware extraction throttle. The temp volume is often small (e.g. a
        # RAM-disk %TEMP%), and many parallel workers each unpacking a multi-GB
        # archive can exhaust it ("No space left on device"). Workers wait on this
        # condition for free space before extracting; whoever finishes and cleans
        # up notifies the waiters. Concurrency self-adjusts to available space.
        self._space_cond = threading.Condition()
        self._temp_min_headroom = 1 * 1024 ** 3   # always keep >= 1 GiB free
        self._temp_safety_factor = 3              # extracted size ~= 3x compressed

        # Decide how files get placed temp -> staging (see _init_placement).
        self._init_placement()

    # --------------------------------------------------------------------- #
    # File placement strategy (temp extraction -> staging)
    # --------------------------------------------------------------------- #
    def _init_placement(self) -> None:
        """Pick the fastest SAFE way to move extracted files into staging:

          * 'hardlink' — temp and staging are on the SAME volume: link instead of
            copy. A metadata op (instant, zero bytes moved); the file is "written"
            the moment 7-Zip wrote it to temp. Temp cleanup just drops the link.
          * 'robocopy' — different volumes, Windows, robocopy present: copy the
            extracted tree with robocopy /MT (multi-threaded). Install concurrency
            is capped so workers x robocopy-threads can't swamp the machine.
          * 'copy'     — anything else: the original per-file shutil.copy2.

        Every path still falls back to copy2 per-file on error, so a wrong guess
        can never break an install -- it only loses the speedup.
        """
        self._robocopy_mt = 0
        try:
            staging_dev = os.stat(self.staging_path).st_dev
            temp_probe = str(self._temp_root) if self._temp_root else str(self.temp_dir)
            temp_dev = os.stat(temp_probe).st_dev
        except OSError:
            staging_dev, temp_dev = 0, 1            # assume different -> safe path

        if staging_dev == temp_dev:
            self._placement_mode = "hardlink"
        elif os.name == "nt" and shutil.which("robocopy"):
            self._placement_mode = "robocopy"
            # Cap parallelism: robocopy /MT:K per mod x N workers = N*K threads.
            # Keep the product near 2x CPU so a cross-disk copy can't thrash.
            cpus = os.cpu_count() or 4
            self._robocopy_mt = max(2, min(8, cpus))
        else:
            self._placement_mode = "copy"
        self.logger.info(f"File placement mode: {self._placement_mode}"
                         + (f" (robocopy /MT:{self._robocopy_mt})" if self._robocopy_mt else ""))

    def placement_worker_cap(self, requested: int) -> int:
        """Safe install-worker count for the chosen placement mode.

        For robocopy each worker may spawn /MT:K threads, so cap workers so
        workers*K stays near 2x CPU. hardlink/copy are unaffected.
        """
        if self._placement_mode == "robocopy" and self._robocopy_mt:
            cpus = os.cpu_count() or 4
            safe = max(2, (2 * cpus) // self._robocopy_mt)
            return max(2, min(requested, safe))
        return requested

    def _hardlink_or_copy(self, src_ext: str, dst_ext: str) -> None:
        """Hardlink src->dst; if the link can't be made (cross-device slipped
        through, FS without links, existing dst), fall back to a real copy."""
        try:
            if os.path.exists(dst_ext):
                os.remove(dst_ext)                  # os.link won't overwrite
            os.link(src_ext, dst_ext)
        except OSError:
            shutil.copy2(src_ext, dst_ext)

    def _robocopy_tree(self, src_dir: Path, dst_dir: Path) -> bool:
        """Multi-threaded cross-volume copy of a whole tree via robocopy /MT.

        robocopy exit codes 0-7 are success (8+ are real errors); the quiet flags
        suppress its per-file output. Returns False so the caller can fall back to
        the per-file copy if robocopy isn't usable for this tree.
        """
        try:
            os.makedirs(dst_dir, exist_ok=True)
            cmd = ["robocopy", str(src_dir), str(dst_dir), "/E",
                   f"/MT:{self._robocopy_mt or 8}", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1"]
            rc = subprocess.run(cmd, capture_output=True, text=True).returncode
            return rc < 8
        except (OSError, subprocess.SubprocessError) as e:
            self.logger.warning(f"robocopy failed ({e}); falling back to per-file copy")
            return False

    def _resolve_temp_root(self, temp_root: Optional[str]) -> Optional[str]:
        """Validate/create the temp override; also point child extractors at it.

        Returns the directory to pass as ``dir=`` to ``mkdtemp`` (None for system
        default). When set, ``TEMP``/``TMP`` are exported so the external 7z/WinRAR
        processes use it for their own scratch too. Falls back to the system temp
        (with a warning) if the override can't be created/written.
        """
        if not temp_root or not str(temp_root).strip():
            return None
        path = str(temp_root).strip()
        try:
            os.makedirs(path, exist_ok=True)
            if not os.access(path, os.W_OK):
                raise OSError("not writable")
        except OSError as e:
            self.logger.warning(
                f"Configured install temp dir '{path}' is unusable ({e}); "
                f"falling back to system temp.")
            return None
        # Make external extractor subprocesses inherit the override for their scratch.
        os.environ["TEMP"] = path
        os.environ["TMP"] = path
        self.logger.info(f"Using override install temp dir: {path}")
        return path

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - cleanup temp directory."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.logger.debug(f"Cleaned up temp directory: {self.temp_dir}")

    def _estimate_extract_size(self, archive_path) -> int:
        """Estimate extracted size from the compressed archive size (conservative)."""
        try:
            return Path(archive_path).stat().st_size * self._temp_safety_factor
        except OSError:
            return 0

    def _acquire_temp_space(self, archive_path, mod_name: str = "", timeout: float = 600.0) -> bool:
        """Block until the temp volume has room to extract ``archive_path``.

        Throttles concurrent extractions by real free space rather than a fixed
        worker count. Returns True when space is available; returns False (and lets
        the caller proceed best-effort) if the archive is larger than the whole
        temp volume or we time out -- never blocks forever, and re-checks every 2s
        so a missed notify can't deadlock it.
        """
        needed = self._estimate_extract_size(archive_path)
        if needed <= 0:
            return True
        deadline = time.monotonic() + timeout
        with self._space_cond:
            while True:
                try:
                    usage = shutil.disk_usage(self.temp_dir)
                except OSError:
                    return True
                if usage.free >= needed + self._temp_min_headroom:
                    return True
                if needed + self._temp_min_headroom > usage.total:
                    # Won't ever fit on this volume; don't wait, let it try/fail cleanly.
                    self.logger.warning(
                        f"{mod_name}: needs ~{needed // (1024**2)}MB temp but volume holds "
                        f"~{usage.total // (1024**2)}MB; extracting without throttle")
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.logger.warning(f"{mod_name}: timed out waiting for temp space; proceeding")
                    return False
                self._space_cond.wait(timeout=min(2.0, remaining))

    def _release_temp_space(self):
        """Wake workers waiting on temp space (call after cleaning up an extraction)."""
        with self._space_cond:
            self._space_cond.notify_all()
    
    def install_mod(self, mod_data: Dict[str, Any], downloads_path: Union[str, Path]) -> InstallationResult:
        """
        Install a mod based on collection.json data.
        
        Args:
            mod_data: Mod data from collection.json
            downloads_path: Path to downloaded mod files
            
        Returns:
            Installation result
        """
        mod_name = mod_data.get("name", "Unknown Mod")
        self.logger.info(f"Installing mod: {mod_name}")
        
        try:
            # Find the downloaded archive
            archive_path = self._find_mod_archive(mod_data, downloads_path)
            if not archive_path:
                return InstallationResult(
                    mod_name=mod_name,
                    status=InstallResult.FAILED,
                    error_message="Downloaded archive not found"
                )
            
            # Check if mod has installation choices
            choices = mod_data.get("choices", {})
            if not choices or choices.get("type") != "fomod":
                # Simple extraction without FOMOD
                return self._install_simple(mod_name, archive_path, mod_data)
            else:
                # FOMOD installation with choices
                return self._install_fomod(mod_name, archive_path, choices, mod_data)
        
        except Exception as e:
            self.logger.error(f"Failed to install {mod_name}: {str(e)}")
            return InstallationResult(
                mod_name=mod_name,
                status=InstallResult.FAILED,
                error_message=str(e)
            )
    
    def _find_mod_archive(self, mod_data: Dict[str, Any], downloads_path: Union[str, Path]) -> Optional[Path]:
        """Find the downloaded archive for a mod."""
        downloads_path = Path(downloads_path)

        # Off-site mods have no modId -- match them by filename/size instead.
        if self._is_manual_mod(mod_data):
            manual = self._find_manual_archive(mod_data, downloads_path)
            if manual:
                return manual

        # Try to find by logical filename or mod ID
        source = mod_data.get("source", {})
        logical_filename = source.get("logicalFilename", "")
        mod_id = source.get("modId")

        # Common archive extensions
        extensions = ['.zip', '.7z', '.rar', '.tar.gz', '.tar.bz2']
        expected_size = source.get("fileSize")
        mod_name = mod_data.get("name", "")

        def _pick(matches):
            """Choose among candidate archives. When several share a modId -- e.g.
            the DOMAIN 'file repository' hosts a dozen DIFFERENT files under modId
            99737 -- returning the first match installs the SAME archive for all of
            them. Disambiguate by EXACT file size (the only reliable key), then by
            overlap with the logical/display name."""
            matches = list(dict.fromkeys(matches))
            if len(matches) <= 1:
                return matches[0] if matches else None
            if expected_size:
                for m in matches:
                    try:
                        if m.stat().st_size == expected_size:
                            return m
                    except OSError:
                        pass
            target = (logical_filename or mod_name).lower().split()
            return max(matches, key=lambda p: sum(
                1 for w in target if len(w) > 3 and w in p.name.lower()))

        # 1) modId TOKEN ("-<modId>-") + exact-size, the reliable key. This MUST
        # precede logical-filename matching: a prefix like "Open Animation Replacer"
        # greedily matches addon archives ("... - Dialogue Plugin-107186-...") and
        # the base mod (a .7z tried after .zip) loses, so the WRONG file installs.
        # Vortex avoids this entirely by tracking each download's stored fileId/md5;
        # we have no such records (we re-match pre-downloaded files on disk), so the
        # "-<modId>-" token + fileSize is the closest reliable identity. The dashes
        # make it a real token -- "*-92109-*" won't match a timestamp like
        # "...1660892109". _pick disambiguates shared-modId variants by exact size.
        if mod_id:
            cands = []
            for ext in extensions:
                cands += downloads_path.glob(f"*-{mod_id}-*{ext}")
            m = _pick(cands)
            if m:
                return m
        # 2) logical filename as exact-or-prefix (Nexus names start with it),
        #    size-confirmed by _pick when several share the prefix.
        if logical_filename:
            for ext in extensions:
                m = _pick(list(downloads_path.glob(f"{logical_filename}*{ext}")))
                if m:
                    return m
        # 3) last-resort fuzzy name match, also size-disambiguated
        if mod_name:
            for ext in extensions:
                cands = [a for a in downloads_path.glob(f"*{ext}")
                         if any(w in a.stem.lower()
                                for w in mod_name.lower().split() if len(w) > 3)]
                m = _pick(cands)
                if m:
                    return m
        return None
    
    def _find_mod_archive_optimized(self, mod_data: Dict[str, Any], available_archives: Dict[str, Path]) -> Optional[Path]:
        """
        Optimized version of _find_mod_archive that uses a pre-built cache of available files.
        
        Args:
            mod_data: Mod data from collection.json
            available_archives: Dictionary mapping lowercase archive names to Path objects
            
        Returns:
            Path to the archive file if found, None otherwise
        """
        source = mod_data.get("source", {})
        logical_filename = (source.get("logicalFilename") or "").lower()
        mod_id = source.get("modId")
        mod_name = (mod_data.get("name") or "").lower()
        extensions = ['.zip', '.7z', '.rar', '.tar.gz', '.tar.bz2']

        # 1) PRECISE: Nexus archive/folder names carry the mod id as a "-<modId>-"
        # token (e.g. "Mod Name-69415-1-0-1655653694.7z"). Match on that token --
        # this is the reliable key. When several files share a modId, disambiguate
        # by name overlap with the logical/display name.
        expected_size = source.get("fileSize")
        if mod_id:
            token = f"-{mod_id}-"
            candidates = [(name, path) for name, path in available_archives.items()
                          if token in name]
            if len(candidates) == 1:
                return candidates[0][1]
            if candidates:
                # Several files share this modId (e.g. the DOMAIN file repository,
                # modId 99737, hosts a dozen DIFFERENT files). Disambiguate by EXACT
                # file size first -- the reliable key, mirroring how Vortex tells
                # variants apart by fileId/fileMD5/fileSize -- then by name overlap.
                # MUST match _find_mod_archive so the skip-check predicts the same
                # folder the installer creates (else variants re-install every run).
                if expected_size:
                    for _name, path in candidates:
                        try:
                            if path.stat().st_size == expected_size:
                                return path
                        except OSError:
                            pass
                target = (logical_filename or mod_name).split()
                def overlap(archive_name: str) -> int:
                    return sum(1 for w in target if len(w) > 3 and w in archive_name)
                candidates.sort(key=lambda c: overlap(c[0]), reverse=True)
                return candidates[0][1]

        # 2) Exact logical-filename match (some archives are named exactly that)
        for ext in extensions:
            if logical_filename and f"{logical_filename}{ext}" in available_archives:
                return available_archives[f"{logical_filename}{ext}"]

        # 3) Last resort: loose name-word match (kept for odd/non-nexus names)
        if mod_name:
            for archive_name, archive_path in available_archives.items():
                if any(word in archive_name for word in mod_name.split() if len(word) > 3):
                    return archive_path

        return None
    
    def _get_vortex_folder_name(self, mod_name: str, archive_path: Path, mod_data: Dict[str, Any] = None) -> str:
        """
        Use the archive filename (without extension) as the folder name.
        This ensures consistency with downloaded filenames and proper detection of existing installations.
        """
        # Simply use the archive filename without extension
        folder_name = archive_path.stem
        
        # Sanitize folder name (remove invalid characters) 
        import re
        folder_name = re.sub(r'[<>:"/\\|?*]', '_', folder_name)
        
        return folder_name
    
    def _find_mod_root(self, extract_path: Path) -> Path:
        """
        Find the actual mod root directory, skipping unnecessary wrapper folders.
        
        This mimics Vortex's smart detection that looks for:
        1. Direct mod content (esp, bsa, meshes, textures, etc.)
        2. Single subdirectory containing mod content
        3. Data folder containing mod content
        """
        # Common Skyrim mod file patterns
        mod_indicators = [
            "*.esp", "*.esm", "*.esl",  # Plugin files
            "*.bsa", "*.ba2",           # Archive files
            "meshes", "textures", "scripts", "sound", "music", "video",  # Data folders
            "CalienteTools", "SKSE", "Interface", "Source",  # Tool folders
            "meta.ini", "fomod"         # Meta files
        ]
        
        def has_mod_content(path: Path) -> bool:
            """Check if a directory contains mod content."""
            if not path.is_dir():
                return False
            
            for pattern in mod_indicators:
                if pattern.startswith("*"):
                    # File pattern
                    if list(path.glob(pattern)):
                        return True
                else:
                    # Directory name
                    if (path / pattern).exists():
                        return True
            return False
        
        # Check if extract_path itself has mod content
        if has_mod_content(extract_path):
            self.logger.debug(f"Mod root found at extraction root: {extract_path}")
            return extract_path
        
        # Look for single subdirectory with mod content
        subdirs = [d for d in extract_path.iterdir() if d.is_dir()]
        
        if len(subdirs) == 1:
            single_subdir = subdirs[0]
            if has_mod_content(single_subdir):
                self.logger.debug(f"Mod root found in single subdirectory: {single_subdir}")
                return single_subdir
            
            # Check for Data folder inside single subdirectory
            data_folder = single_subdir / "Data"
            if data_folder.exists() and has_mod_content(data_folder):
                self.logger.debug(f"Mod root found in Data subfolder: {data_folder}")
                return data_folder
        
        # Look for Data folder at extraction root
        data_folder = extract_path / "Data"
        if data_folder.exists() and has_mod_content(data_folder):
            self.logger.debug(f"Mod root found in Data folder: {data_folder}")
            return data_folder
        
        # Look for any subdirectory with mod content
        for subdir in subdirs:
            if has_mod_content(subdir):
                self.logger.debug(f"Mod root found in subdirectory: {subdir}")
                return subdir
        
        # Fallback to extraction path
        self.logger.warning(f"No clear mod root found, using extraction path: {extract_path}")
        return extract_path
    
    def _copy_long(self, src: Path, dst: Path) -> bool:
        """Copy ``src`` -> ``dst`` using the Windows extended-length (``\\\\?\\``)
        prefix so files whose full path exceeds MAX_PATH (260 chars) still install
        instead of being silently skipped. This is what was dropping every file in
        mods with a long folder name + deep paths (e.g. OAR animations), leaving
        "no files installed". No-op prefix on non-Windows. Returns True on success.
        """
        def ext(p):
            s = os.fspath(p)
            if os.name != "nt":
                return s
            ap = os.path.abspath(s)
            if ap.startswith("\\\\?\\"):
                return ap
            return ("\\\\?\\UNC\\" + ap[2:]) if ap.startswith("\\\\") else ("\\\\?\\" + ap)
        try:
            os.makedirs(ext(dst.parent), exist_ok=True)
            if self._placement_mode == "hardlink":
                # Same volume: link instead of copy (instant, zero bytes moved).
                # Falls back to copy2 internally if the link can't be made.
                self._hardlink_or_copy(ext(src), ext(dst))
            else:
                shutil.copy2(ext(src), ext(dst))
            return True
        except (OSError, FileNotFoundError) as e:
            self.logger.warning(f"Failed to place (even with long-path prefix) {dst}: {e}")
            return False

    def _install_simple(self, mod_name: str, archive_path: Path, mod_data: Dict[str, Any] = None) -> InstallationResult:
        """Install mod without FOMOD (simple extraction)."""
        temp_extract = None
        try:
            # Use Vortex naming convention: ModName-ModID-Version-Timestamp
            folder_name = self._get_vortex_folder_name(mod_name, archive_path, mod_data)
            mod_install_path = self.staging_path / folder_name
            mod_install_path.mkdir(parents=True, exist_ok=True)
            
            # Extract archive - use unique temp directory to avoid hash collisions
            import hashlib
            import threading
            import time
            # Create unique hash using folder name, thread ID, and timestamp to prevent collisions
            unique_string = f"{folder_name}_{threading.get_ident()}_{int(time.time() * 1000000)}"
            folder_hash = hashlib.md5(unique_string.encode()).hexdigest()[:16]  # Use 16 chars instead of 8
            temp_extract = self.temp_dir / f"e_{folder_hash}"
            self.logger.debug(f"Using temp extraction directory: {temp_extract} for mod: {mod_name}")
            
            # Wait for room on the (often small/ramdisk) temp volume before extracting
            self._acquire_temp_space(archive_path, mod_name)
            try:
                self.archive_handler.extract_archive(archive_path, temp_extract)
            except Exception as extract_error:
                error_msg = str(extract_error)
                if "path specified" in error_msg.lower() or "long path" in error_msg.lower():
                    self.logger.warning(f"Path length issue extracting {archive_path.name}, trying alternative extraction")
                    # Try with shorter path but still unique
                    short_hash = hashlib.md5(unique_string.encode()).hexdigest()[:8]
                    temp_extract = self.temp_dir / f"e_{short_hash}_{threading.get_ident()}"
                    self.logger.debug(f"Fallback temp extraction directory: {temp_extract} for mod: {mod_name}")
                    try:
                        self.archive_handler.extract_archive(archive_path, temp_extract)
                    except Exception:
                        self._release_temp_space()
                        return InstallationResult(
                            mod_name=mod_name,
                            status=InstallResult.FAILED,
                            error_message=f"Failed to extract archive due to path length issues: {error_msg}"
                        )
                else:
                    self._release_temp_space()
                    raise  # Re-raise other extraction errors (including BCJ2 with helpful message from archive handler)

            # Place files into staging with smart path detection.
            installed_files = []
            root_path = self._find_mod_root(temp_extract)

            # Cross-volume + robocopy available: copy the whole tree multi-threaded
            # in one shot, then drop the few skip-pattern files. (Hardlink/copy
            # modes use the per-file path below; robocopy falls back to it too if
            # the tree copy didn't succeed.)
            placed_by_robocopy = False
            if self._placement_mode == "robocopy" and self._robocopy_tree(root_path, mod_install_path):
                placed_by_robocopy = True
                for item in mod_install_path.rglob("*"):
                    if item.is_file():
                        rel_path = item.relative_to(mod_install_path)
                        if self._should_skip_file(rel_path):
                            try:
                                item.unlink()
                            except OSError:
                                pass
                        else:
                            installed_files.append(item)

            if not placed_by_robocopy:
                for item in root_path.rglob("*"):
                    if item.is_file():
                        # Skip FOMOD files and metadata
                        rel_path = item.relative_to(root_path)
                        if not self._should_skip_file(rel_path):
                            dest_path = mod_install_path / rel_path
                            if self._copy_long(item, dest_path):
                                installed_files.append(dest_path)
            
            # Clean up temp extraction immediately after copying
            if temp_extract and temp_extract.exists():
                try:
                    self.logger.debug(f" Cleaning up temp extraction for {mod_name}: {temp_extract}")
                    shutil.rmtree(temp_extract, ignore_errors=True)
                    self.logger.debug(f"Cleaned up temp extraction for {mod_name}: {temp_extract}")
                    self.logger.debug(f" Cleanup completed for {mod_name}")
                except Exception as cleanup_error:
                    self.logger.debug(f" Cleanup failed for {mod_name}: {cleanup_error}")
                    self.logger.warning(f"Failed to cleanup temp extraction {temp_extract}: {cleanup_error}")
            # Freed temp space -> wake any workers throttled waiting for room
            self._release_temp_space()

            # Validate installation is not empty
            if not installed_files:
                error_msg = f"Installation completed but no files were installed for {mod_name}. This could be due to path length issues, file filtering, or extraction problems."
                self.logger.error(error_msg)
                return InstallationResult(
                    mod_name=mod_name,
                    status=InstallResult.FAILED,
                    error_message=error_msg
                )
            
            # Verify mod folder has content and is valid
            if not self._validate_mod_installation(mod_install_path):
                error_msg = f"Installation completed but mod folder appears to be empty or invalid for {mod_name}"
                self.logger.error(error_msg)
                return InstallationResult(
                    mod_name=mod_name,
                    status=InstallResult.FAILED,
                    error_message=error_msg
                )

            # Phantom-install guard + diagnostics. Some mods (notably SKSE-only
            # frameworks like OAR) have reported success with N files yet left NO
            # staging folder afterward. Log the RESOLVED destination and the real
            # on-disk count so we can see whether files landed in the actual
            # staging path -- and fail loudly (so the retry re-attempts) if the
            # folder didn't materialize, instead of claiming a phantom success.
            on_disk = (sum(1 for p in mod_install_path.rglob("*") if p.is_file())
                       if mod_install_path.exists() else 0)
            self.logger.info(
                f"INSTALL-VERIFY {mod_name}: claimed={len(installed_files)} "
                f"on_disk={on_disk} dest={mod_install_path} "
                f"abs={os.path.abspath(mod_install_path)} staging={self.staging_path}")
            if on_disk == 0:
                error_msg = (f"Installation completed but no files were installed for "
                             f"{mod_name} (phantom: {len(installed_files)} copied, 0 on "
                             f"disk at {mod_install_path})")
                self.logger.error(error_msg)
                return InstallationResult(mod_name=mod_name, status=InstallResult.FAILED,
                                          error_message=error_msg)

            return InstallationResult(
                mod_name=mod_name,
                status=InstallResult.SUCCESS,
                installed_files=installed_files
            )
        
        except Exception as e:
            # Clean up temp extraction even on failure
            if temp_extract and temp_extract.exists():
                try:
                    self.logger.debug(f" Error cleanup for {mod_name}: {temp_extract}")
                    shutil.rmtree(temp_extract, ignore_errors=True)
                    self.logger.debug(f"Cleaned up temp extraction after error for {mod_name}: {temp_extract}")
                    self.logger.debug(f" Error cleanup completed for {mod_name}")
                except Exception:
                    self.logger.debug(f" Error cleanup failed for {mod_name}")
                    pass  # Don't let cleanup errors mask the original error
            # Wake any workers throttled waiting for temp room
            self._release_temp_space()

            return InstallationResult(
                mod_name=mod_name,
                status=InstallResult.FAILED,
                error_message=str(e)
            )
    
    def _install_fomod(self, mod_name: str, archive_path: Path, choices_data: Dict[str, Any], mod_data: Dict[str, Any] = None) -> InstallationResult:
        """Install mod with FOMOD choices."""
        temp_extract = None
        try:
            # Use Vortex naming convention: ModName-ModID-Version-Timestamp
            folder_name = self._get_vortex_folder_name(mod_name, archive_path, mod_data)
            mod_install_path = self.staging_path / folder_name
            mod_install_path.mkdir(parents=True, exist_ok=True)
            
            # Extract archive to temp location - use unique temp directory to avoid hash collisions
            import hashlib
            import threading
            import time
            # Create unique hash using folder name, thread ID, and timestamp to prevent collisions
            unique_string = f"{folder_name}_{threading.get_ident()}_{int(time.time() * 1000000)}"
            folder_hash = hashlib.md5(unique_string.encode()).hexdigest()[:16]  # Use 16 chars instead of 8
            temp_extract = self.temp_dir / f"e_{folder_hash}"
            self.logger.debug(f"Using temp extraction directory: {temp_extract} for mod: {mod_name}")
            
            # Wait for room on the (often small/ramdisk) temp volume before extracting
            self._acquire_temp_space(archive_path, mod_name)
            try:
                self.archive_handler.extract_archive(archive_path, temp_extract)
            except Exception as extract_error:
                error_msg = str(extract_error)
                if "path specified" in error_msg.lower() or "long path" in error_msg.lower():
                    self.logger.warning(f"Path length issue extracting {archive_path.name}, trying alternative extraction")
                    # Try with shorter path but still unique
                    short_hash = hashlib.md5(unique_string.encode()).hexdigest()[:8]
                    temp_extract = self.temp_dir / f"e_{short_hash}_{threading.get_ident()}"
                    self.logger.debug(f"Fallback temp extraction directory: {temp_extract} for mod: {mod_name}")
                    try:
                        self.archive_handler.extract_archive(archive_path, temp_extract)
                    except Exception:
                        self._release_temp_space()
                        return InstallationResult(
                            mod_name=mod_name,
                            status=InstallResult.FAILED,
                            error_message=f"Failed to extract archive due to path length issues: {error_msg}"
                        )
                else:
                    self._release_temp_space()
                    raise  # Re-raise other extraction errors (including BCJ2 with helpful message from archive handler)

            # Resolve which files to install by parsing the real FOMOD
            # ModuleConfig.xml and mapping the collection's recorded choices onto
            # it (see utils.fomod_engine). If the archive has no parseable FOMOD,
            # fall back to a plain extraction so the mod is never silently lost.
            from utils.fomod_engine import find_moduleconfig, parse_moduleconfig, resolve_install

            module_config = find_moduleconfig(temp_extract)
            if module_config is None:
                # A complex multi-folder FOMOD can come out of a py7zr extraction
                # INCOMPLETE on Windows (long paths), silently dropping
                # ModuleConfig.xml -- which would then lose the whole mod via the
                # simple-install fallback (everything lives in option subfolders).
                # If the archive actually CONTAINS a ModuleConfig.xml, re-extract
                # with the external 7z tool (robust + long-path safe) and retry.
                archive_has_fomod = False
                try:
                    archive_has_fomod = any(
                        n.lower().replace("\\", "/").endswith("fomod/moduleconfig.xml")
                        for n in self.archive_handler.list_archive_contents(archive_path))
                except Exception:
                    pass
                if archive_has_fomod and _external_tools.get("7z"):
                    self.logger.warning(
                        f"{mod_name}: ModuleConfig.xml missing after extraction but present "
                        f"in the archive -- re-extracting with external 7z.")
                    try:
                        self.archive_handler._extract_7z_external(archive_path, temp_extract)
                        module_config = find_moduleconfig(temp_extract)
                    except Exception as reextract_error:
                        self.logger.error(
                            f"{mod_name}: external 7z re-extract failed: {reextract_error}")

            if module_config is None:
                self.logger.warning(
                    f"{mod_name}: choices present but no fomod/ModuleConfig.xml found; "
                    f"falling back to simple install")
                shutil.rmtree(temp_extract, ignore_errors=True)
                return self._install_simple(mod_name, archive_path, mod_data)

            package_root = module_config.parent.parent  # dir containing the fomod/ folder
            try:
                config = parse_moduleconfig(module_config)
                file_ops, report = resolve_install(config, choices_data, package_root)
            except Exception as parse_error:
                self.logger.error(
                    f"{mod_name}: failed to parse FOMOD ({parse_error}); falling back to simple install")
                shutil.rmtree(temp_extract, ignore_errors=True)
                return self._install_simple(mod_name, archive_path, mod_data)

            self.logger.info(
                f"{mod_name}: FOMOD resolved {len(report.chosen_plugins)} option(s) -> "
                f"{len(file_ops)} file(s)"
                + (f"; {report.conditional_patterns_applied} conditional rule(s) applied"
                   if report.conditional_patterns_applied else ""))
            if report.unmatched_selections:
                self.logger.warning(
                    f"{mod_name}: could not match FOMOD selections {report.unmatched_selections}")

            # A FOMOD that resolves to nothing usually means the collection
            # recorded no usable selection; install everything so the mod isn't lost.
            if not file_ops:
                self.logger.warning(
                    f"{mod_name}: FOMOD resolved to 0 files; falling back to simple install")
                shutil.rmtree(temp_extract, ignore_errors=True)
                return self._install_simple(mod_name, archive_path, mod_data)

            # Copy resolved files into staging. file_ops are priority-ordered, so a
            # later op overwriting the same destination wins (FOMOD overwrite rule).
            installed_files = []
            for op in file_ops:
                source_path = Path(op.abs_source)
                if not source_path.is_file():
                    continue
                dest_path = mod_install_path / op.destination
                if self._copy_long(source_path, dest_path) and dest_path not in installed_files:
                    installed_files.append(dest_path)
            
            # Clean up temp extraction immediately after copying
            if temp_extract and temp_extract.exists():
                try:
                    self.logger.debug(f" Cleaning up temp extraction for {mod_name}: {temp_extract}")
                    shutil.rmtree(temp_extract, ignore_errors=True)
                    self.logger.debug(f"Cleaned up temp extraction for {mod_name}: {temp_extract}")
                    self.logger.debug(f" Cleanup completed for {mod_name}")
                except Exception as cleanup_error:
                    self.logger.debug(f" Cleanup failed for {mod_name}: {cleanup_error}")
                    self.logger.warning(f"Failed to cleanup temp extraction {temp_extract}: {cleanup_error}")
            # Freed temp space -> wake any workers throttled waiting for room
            self._release_temp_space()

            # Validate installation is not empty
            if not installed_files:
                error_msg = f"FOMOD installation completed but no files were installed for {mod_name}. Check FOMOD choices and file mapping."
                self.logger.error(error_msg)
                return InstallationResult(
                    mod_name=mod_name,
                    status=InstallResult.FAILED,
                    error_message=error_msg
                )
            
            # Verify mod folder has content and is valid
            if not self._validate_mod_installation(mod_install_path):
                error_msg = f"FOMOD installation completed but mod folder appears to be empty or invalid for {mod_name}"
                self.logger.error(error_msg)
                return InstallationResult(
                    mod_name=mod_name,
                    status=InstallResult.FAILED,
                    error_message=error_msg
                )
            
            return InstallationResult(
                mod_name=mod_name,
                status=InstallResult.SUCCESS,
                installed_files=installed_files
            )
        
        except Exception as e:
            # Clean up temp extraction even on failure
            if temp_extract and temp_extract.exists():
                try:
                    self.logger.debug(f" Error cleanup for {mod_name}: {temp_extract}")
                    shutil.rmtree(temp_extract, ignore_errors=True)
                    self.logger.debug(f"Cleaned up temp extraction after error for {mod_name}: {temp_extract}")
                    self.logger.debug(f" Error cleanup completed for {mod_name}")
                except Exception:
                    self.logger.debug(f" Error cleanup failed for {mod_name}")
                    pass  # Don't let cleanup errors mask the original error
            # Wake any workers throttled waiting for temp room
            self._release_temp_space()

            return InstallationResult(
                mod_name=mod_name,
                status=InstallResult.FAILED,
                error_message=str(e)
            )
    
    def _parse_fomod_choices(self, choices_data: Dict[str, Any], extract_path: Path) -> List[str]:
        """
        Parse FOMOD choices from collection.json format.
        
        This is a simplified implementation that assumes the collection creator
        has already made the optimal choices and we just need to map them to files.
        """
        selected_files = []
        
        # Get the selected options from collection
        options = choices_data.get("options", [])
        
        self.logger.debug(f"FOMOD: Processing {len(options)} options from choices data")
        
        for option_idx, option in enumerate(options):
            option_name = option.get("name", "")
            groups = option.get("groups", [])
            
            self.logger.debug(f"FOMOD: Option {option_idx}: '{option_name}' with {len(groups)} groups")
            
            for group_idx, group in enumerate(groups):
                group_name = group.get("name", "")
                group_choices = group.get("choices", [])
                
                self.logger.debug(f"FOMOD: Group {group_idx}: '{group_name}' with {len(group_choices)} choices")
                
                for choice_idx_in_group, choice in enumerate(group_choices):
                    choice_name = choice.get("name", "")
                    choice_idx = choice.get("idx", 0)
                    
                    self.logger.debug(f"FOMOD: Choice {choice_idx_in_group}: '{choice_name}' (idx: {choice_idx})")
                    
                    # Map choice to files based on common FOMOD patterns
                    choice_files = self._map_choice_to_files(
                        option_name, group_name, choice_name, choice_idx, extract_path
                    )
                    
                    self.logger.debug(f"FOMOD: Choice '{choice_name}' mapped to {len(choice_files)} files: {choice_files[:5]}{'...' if len(choice_files) > 5 else ''}")
                    
                    selected_files.extend(choice_files)
        
        # Remove duplicates and sort
        unique_files = sorted(list(set(selected_files)))
        self.logger.debug(f"FOMOD: Total selected files after deduplication: {len(unique_files)}")
        
        if len(unique_files) == 0:
            self.logger.warning(f"FOMOD: No files selected! Available files in extract_path:")
            all_files = list(extract_path.rglob("*"))
            for file_path in all_files[:20]:  # Log first 20 files for debugging
                self.logger.warning(f"FOMOD: Available: {file_path.relative_to(extract_path)}")
            if len(all_files) > 20:
                self.logger.warning(f"FOMOD: ... and {len(all_files) - 20} more files")
        
        return unique_files
    
    def _map_choice_to_files(self, option_name: str, group_name: str, choice_name: str, 
                           choice_idx: int, extract_path: Path) -> List[str]:
        """
        Map FOMOD choice to actual files in the archive.
        
        This is a heuristic approach since we don't have the full FOMOD XML structure.
        """
        mapped_files = []
        
        # Common patterns for FOMOD organization
        common_paths = [
            f"{choice_idx:02d} {choice_name}",
            f"{choice_idx} {choice_name}",
            choice_name,
            f"{group_name}/{choice_name}",
            f"{option_name}/{choice_name}",
            f"fomod/{choice_name}",
            f"options/{choice_name}",
            f"optional/{choice_name}"
        ]
        
        self.logger.debug(f"FOMOD: Mapping choice '{choice_name}' (option: '{option_name}', group: '{group_name}', idx: {choice_idx})")
        self.logger.debug(f"FOMOD: Testing {len(common_paths)} path patterns")
        
        # Search for matching directories/files with case-insensitive comparison
        for pattern_idx, pattern in enumerate(common_paths):
            self.logger.debug(f"FOMOD: Testing pattern {pattern_idx}: '{pattern}'")
            
            # Try exact match first
            pattern_path = extract_path / pattern
            if pattern_path.exists():
                self.logger.debug(f"FOMOD: Exact match found for pattern: '{pattern}'")
                self._add_files_from_path(pattern_path, extract_path, mapped_files)
                continue
            
            # Try case-insensitive search if exact match fails
            found_match = False
            pattern_lower = pattern.lower()
            
            # Search through all directories in extract_path
            for item in extract_path.rglob("*"):
                item_relative = item.relative_to(extract_path)
                item_name_lower = item.name.lower()
                item_path_lower = str(item_relative).lower().replace('\\', '/')
                pattern_path_lower = pattern_lower.replace('\\', '/')
                
                if item_name_lower == pattern_lower or item_path_lower == pattern_path_lower:
                    self.logger.debug(f"FOMOD: Case-insensitive match found: '{item_relative}' matches pattern '{pattern}'")
                    self._add_files_from_path(item, extract_path, mapped_files)
                    found_match = True
                    break
            
            if not found_match:
                self.logger.debug(f"FOMOD: No match found for pattern: '{pattern}'")
        
        # If no specific mapping found, include files that match choice name
        if not mapped_files:
            self.logger.debug(f"FOMOD: No pattern matches found, searching for files containing choice name '{choice_name}'")
            
            choice_lower = choice_name.lower()
            for file_path in extract_path.rglob("*"):
                if file_path.is_file():
                    file_name_lower = file_path.name.lower()
                    file_path_lower = str(file_path.relative_to(extract_path)).lower().replace('\\', '/')
                    
                    # Check if choice name appears in filename or path (case-insensitive)
                    if (choice_lower in file_name_lower or choice_lower in file_path_lower) and \
                       not self._should_skip_file(file_path.relative_to(extract_path)):
                        self.logger.debug(f"FOMOD: Found matching file by name: '{file_path.relative_to(extract_path)}'")
                        mapped_files.append(str(file_path.relative_to(extract_path)))
        
        self.logger.debug(f"FOMOD: Choice '{choice_name}' mapped to {len(mapped_files)} files")
        return mapped_files
    
    def _add_files_from_path(self, path: Path, extract_path: Path, mapped_files: List[str]):
        """Helper method to add files from a path to mapped_files list"""
        if path.is_dir():
            # Add all files in directory
            files_added = 0
            for file_path in path.rglob("*"):
                if file_path.is_file() and not self._should_skip_file(file_path.relative_to(extract_path)):
                    mapped_files.append(str(file_path.relative_to(extract_path)))
                    files_added += 1
            self.logger.debug(f"FOMOD: Added {files_added} files from directory: '{path.relative_to(extract_path)}'")
        else:
            # Single file
            if not self._should_skip_file(path.relative_to(extract_path)):
                mapped_files.append(str(path.relative_to(extract_path)))
                self.logger.debug(f"FOMOD: Added single file: '{path.relative_to(extract_path)}'")
    
    def _should_skip_file(self, file_path: Path) -> bool:
        """Check if a file should be skipped during installation."""
        skip_patterns = [
            'fomod/',
            'readme',
            'changelog',
            'license',
            '.txt',
            '.md',
            '.pdf',
            'thumbs.db',
            '.ds_store'
        ]
        
        # Convert to lowercase for case-insensitive comparison
        file_path_str = str(file_path).lower().replace('\\', '/')
        
        for pattern in skip_patterns:
            pattern_lower = pattern.lower()
            if pattern_lower in file_path_str:
                self.logger.debug(f"FOMOD: Skipping file '{file_path}' - matches skip pattern '{pattern}'")
                return True
        
        return False
    
    def validate_downloads_complete(self, collection_data: Dict[str, Any], 
                                   downloads_path: Union[str, Path]) -> Dict[str, Any]:
        """
        Validate that all required files from collection.json have been downloaded.
        
        Args:
            collection_data: Full collection.json data
            downloads_path: Path to downloaded mod files
            
        Returns:
            Dictionary with validation results
        """
        validation_result = {
            "all_downloaded": True,
            "missing_files": [],
            "found_files": [],
            "total_mods": 0,
            "available_mods": 0
        }
        
        mods = collection_data.get("mods", [])
        validation_result["total_mods"] = len(mods)
        
        self.logger.debug(f" Validating downloads for {len(mods)} mods...")
        self.logger.info(f"Validating downloads for {len(mods)} mods...")
        
        # Optimize: Build a cache of available archive files first
        self.logger.debug(f" Building archive file cache...")
        downloads_path = Path(downloads_path)
        available_archives = {}
        extensions = ['.zip', '.7z', '.rar', '.tar.gz', '.tar.bz2']
        
        for ext in extensions:
            for archive_file in downloads_path.glob(f"*{ext}"):
                archive_name_lower = archive_file.name.lower()
                available_archives[archive_name_lower] = archive_file
        
        self.logger.debug(f" Found {len(available_archives)} archive files in downloads directory")
        
        for i, mod_data in enumerate(mods):
            if i % 500 == 0:  # Progress update every 500 mods (faster validation)
                self.logger.debug(f" Validation progress: {i}/{len(mods)} mods checked")
            
            mod_name = mod_data.get("name", "Unknown Mod")
            # Use optimized lookup instead of full file search
            archive_path = self._find_mod_archive_optimized(mod_data, available_archives)
            
            if archive_path and archive_path.exists():
                validation_result["found_files"].append({
                    "mod_name": mod_name,
                    "archive_path": str(archive_path)
                })
                validation_result["available_mods"] += 1
            else:
                validation_result["missing_files"].append({
                    "mod_name": mod_name,
                    "source": mod_data.get("source", {}),
                    "expected_filename": mod_data.get("source", {}).get("logicalFilename", "")
                })
                validation_result["all_downloaded"] = False
        
        if validation_result["all_downloaded"]:
            self.logger.info(f"✓ All {len(mods)} mod files are available for installation")
        else:
            missing_count = len(validation_result["missing_files"])
            self.logger.warning(f"✗ {missing_count} mod files are missing from downloads folder")
            for missing in validation_result["missing_files"]:
                self.logger.warning(f"  Missing: {missing['mod_name']} ({missing['expected_filename']})")
        
        return validation_result
    
    def _build_archive_cache(self, downloads_path: Union[str, Path]) -> Dict[str, Path]:
        """Map lowercase archive filename -> Path for a downloads folder (one glob pass)."""
        cache: Dict[str, Path] = {}
        downloads_path = Path(downloads_path)
        if not downloads_path.exists():
            return cache
        for ext in ('.zip', '.7z', '.rar', '.tar.gz', '.tar.bz2'):
            for archive_file in downloads_path.glob(f"*{ext}"):
                cache[archive_file.name.lower()] = archive_file
        return cache

    def scan_existing_installations(self, collection_data: Dict[str, Any],
                                    downloads_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
        """
        Scan the mod staging folder to identify already installed mods.

        The expected staging-folder name is the mod's archive filename stem (this is
        how Vortex and our installer both name folders), so we resolve each mod to
        its real archive in ``downloads_path`` to predict the folder. Without
        ``downloads_path`` the folder can't be predicted and every mod is treated as
        missing (safe -- the install step then re-checks per mod).

        Args:
            collection_data: Full collection.json data
            downloads_path: Folder holding the downloaded archives

        Returns:
            Dictionary with scan results
        """
        scan_result = {
            "total_mods": 0,
            "installed_mods": [],
            "missing_mods": [],
            "installed_count": 0,
            "missing_count": 0
        }

        mods = collection_data.get("mods", [])
        scan_result["total_mods"] = len(mods)

        self.logger.info(f"Scanning staging folder for existing installations: {self.staging_path}")

        # Get all existing mod folders in staging directory
        existing_folders = set()
        if self.staging_path.exists():
            for item in self.staging_path.iterdir():
                if item.is_dir():
                    existing_folders.add(item.name.lower())  # Case-insensitive matching

        self.logger.debug(f" Found {len(existing_folders)} existing mod folders in staging directory")

        # Resolve folder names from real archives so the prediction matches disk.
        available_archives = self._build_archive_cache(downloads_path) if downloads_path else {}
        if not available_archives:
            self.logger.warning(
                "No downloads path for the existing-install scan; cannot predict "
                "folder names, so all mods will be treated as needing install.")

        for i, mod_data in enumerate(mods):
            if i % 500 == 0:  # Progress update every 500 mods
                self.logger.debug(f" Scan progress: {i}/{len(mods)} mods checked")

            mod_name = mod_data.get("name", f"Mod {i+1}")

            # Predict the staging folder name from the mod's real archive stem.
            archive_path = (self._find_mod_archive_optimized(mod_data, available_archives)
                            if available_archives else None)
            expected_folder_name = (self._get_vortex_folder_name(mod_name, archive_path, mod_data)
                                    if archive_path else "")

            # Check if this mod appears to be installed
            if expected_folder_name and expected_folder_name.lower() in existing_folders:
                scan_result["installed_mods"].append({
                    "mod_name": mod_name,
                    "folder_name": expected_folder_name,
                    "mod_data": mod_data
                })
                scan_result["installed_count"] += 1
            else:
                scan_result["missing_mods"].append({
                    "mod_name": mod_name,
                    "expected_folder": expected_folder_name,
                    "mod_data": mod_data
                })
                scan_result["missing_count"] += 1
        
        self.logger.info(f"Staging folder scan complete: {scan_result['installed_count']} installed, {scan_result['missing_count']} missing")
        self.logger.debug(f" Scan complete - {scan_result['installed_count']} already installed, {scan_result['missing_count']} to install")
        
        return scan_result
    
    def create_filtered_collection(self, collection_data: Dict[str, Any], scan_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a filtered collection containing only mods that need to be installed.
        
        Args:
            collection_data: Original collection.json data
            scan_result: Result from scan_existing_installations
            
        Returns:
            Filtered collection data with only missing mods
        """
        # Create a copy of the original collection
        filtered_collection = collection_data.copy()
        
        # Replace the mods list with only missing mods
        filtered_collection["mods"] = [mod["mod_data"] for mod in scan_result["missing_mods"]]
        
        # Update metadata if present
        if "info" in filtered_collection:
            original_count = scan_result["total_mods"]
            filtered_count = scan_result["missing_count"]
            
            # Add scan info to collection metadata
            filtered_collection["install_scan"] = {
                "original_mod_count": original_count,
                "already_installed": scan_result["installed_count"],
                "to_install": filtered_count,
                "scan_time": str(datetime.now()) if 'datetime' in globals() else "unknown"
            }
        
        return filtered_collection
    
    def check_mod_already_installed(self, mod_name: str, mod_data: Dict[str, Any],
                                    available_archives: Optional[Dict[str, Path]] = None) -> bool:
        """
        Check if a mod is already installed correctly in the staging folder.

        Args:
            mod_name: Name of the mod
            mod_data: Mod data from collection.json
            available_archives: Optional cache (lowercase archive name -> Path) used to
                predict the staging-folder name from the mod's real archive stem.

        Returns:
            True if mod is already installed correctly
        """
        try:
            # Predict the folder name from the mod's real archive (None -> can't tell).
            archive_path = (self._find_mod_archive_optimized(mod_data, available_archives)
                            if available_archives else None)
            if archive_path is None:
                return False
            folder_name = self._get_vortex_folder_name(mod_name, archive_path, mod_data)
            mod_install_path = self.staging_path / folder_name
            
            if not mod_install_path.exists():
                return False
            
            # Check if the mod folder has content
            has_content = any(mod_install_path.iterdir())
            if not has_content:
                self.logger.debug(f"Mod folder exists but is empty: {folder_name}")
                return False
            
            # Basic validation: check for common mod file patterns
            has_mod_files = self._validate_mod_installation(mod_install_path)
            if has_mod_files:
                self.logger.debug(f"Mod already installed correctly: {folder_name}")
                return True
            else:
                self.logger.debug(f"Mod folder exists but appears incomplete: {folder_name}")
                return False
                
        except Exception as e:
            self.logger.warning(f"Error checking if mod is installed {mod_name}: {e}")
            return False
    
    def _validate_mod_installation(self, mod_path: Path) -> bool:
        """
        Validate that a mod installation appears to be complete.
        
        Args:
            mod_path: Path to the installed mod folder
            
        Returns:
            True if the installation appears valid
        """
        # Look for common Skyrim mod file indicators
        mod_indicators = [
            "*.esp", "*.esm", "*.esl",  # Plugin files
            "*.bsa", "*.ba2",           # Archive files
        ]
        
        # Check for plugin or archive files (most mods have at least one)
        for pattern in mod_indicators:
            if list(mod_path.glob(pattern)):
                return True
        
        # Check for common mod directories with content
        mod_directories = ["meshes", "textures", "scripts", "sound", "music", "CalienteTools", "SKSE"]
        for dir_name in mod_directories:
            dir_path = mod_path / dir_name
            if dir_path.exists() and dir_path.is_dir():
                # Check if directory has content
                if any(dir_path.rglob("*")):
                    return True
        
        # If we get here, the folder might just have config/text files
        # Check if there are any substantial files (not just readme/meta files)
        for file_path in mod_path.rglob("*"):
            if file_path.is_file():
                file_name = file_path.name.lower()
                # Skip common metadata files
                if not any(skip in file_name for skip in ["readme", "license", "changelog", ".txt", ".md", "meta.ini"]):
                    return True
        
        return False

    def _is_manual_mod(self, mod_data: Dict[str, Any]) -> bool:
        """True if a mod can't be auto-installed (external/bundled source or no Nexus file ids).

        Collections include mods hosted off-Nexus (Google Drive, LoversLab, etc.,
        with source.type "browse") and files bundled into the collection
        (source.type "bundle"). These have no downloadable Nexus archive and must
        be installed by hand from the collection page.
        """
        source = mod_data.get("source", {})
        source_type = (source.get("type") or "").lower()
        if source_type and source_type != "nexus":
            return True
        if not source.get("modId") or not source.get("fileId"):
            return True
        return False

    def _find_manual_archive(self, mod_data: Dict[str, Any],
                             downloads_path: Union[str, Path]) -> Optional[Path]:
        """Find a hand-downloaded archive for an off-site mod.

        Off-site/bundled mods have no Nexus modId, but the collection lists each by
        its archive filename (the mod ``name``) plus an ``md5``/``fileSize``. So if
        the user dropped the file in the downloads folder, match it by exact name,
        then by exact byte size (handles a renamed file)."""
        downloads_path = Path(downloads_path)
        exts = (".7z", ".zip", ".rar")
        name = (mod_data.get("name") or "").strip()
        src = mod_data.get("source", {})
        # 1) exact filename -- off-site mods are named by their archive
        if name and name.lower().endswith(exts):
            cand = downloads_path / name
            if cand.exists():
                return cand
        # 2) exact byte-size match, via a one-time cached {size: path} index so we
        #    don't rescan the (large) downloads folder once per off-site mod.
        size = src.get("fileSize")
        if size:
            return self._download_size_index(downloads_path).get(size)
        return None

    def _download_size_index(self, downloads_path: Path) -> Dict[int, Path]:
        """Cached ``{file_size: archive_path}`` for the downloads folder (built once)."""
        idx = getattr(self, "_dl_size_index", None)
        if idx is None:
            idx = {}
            try:
                for f in Path(downloads_path).iterdir():
                    if f.suffix.lower() in (".7z", ".zip", ".rar") and f.is_file():
                        try:
                            idx.setdefault(f.stat().st_size, f)
                        except OSError:
                            pass
            except OSError:
                pass
            self._dl_size_index = idx
        return idx

    def _partition_manual_mods(self, collection_data: Dict[str, Any],
                               downloads_path: Union[str, Path] = None):
        """Split out off-site/bundled mods that still need a HAND download.

        An off-site mod whose archive is ALREADY in the downloads folder installs
        like any other mod (it's kept in the install list); only the ones we can't
        find on disk are set aside as SKIPPED, carrying the source URL/instructions.
        """
        mods = collection_data.get("mods", [])
        manual = [m for m in mods if self._is_manual_mod(m)]
        if not manual:
            return collection_data, []

        present = set()
        if downloads_path:
            for m in manual:
                if self._find_manual_archive(m, downloads_path):
                    present.add(id(m))

        manual_results = []
        for m in manual:
            if id(m) in present:
                continue   # archive is here -> install it normally
            source = m.get("source", {})
            url = source.get("url", "")
            instructions = (source.get("instructions") or m.get("instructions") or "").strip()
            note = "Manual install required (external or bundled source)."
            if url:
                note += f" Download from: {url}"
            if instructions:
                note += f" Author note: {instructions}"
            manual_results.append(InstallationResult(
                mod_name=m.get("name", "Unknown Mod"),
                status=InstallResult.SKIPPED,
                installed_files=[],
                warnings=[note],
            ))

        # Keep nexus mods + any off-site mods whose archive is present on disk.
        filtered = dict(collection_data)
        filtered["mods"] = [m for m in mods
                            if (not self._is_manual_mod(m)) or (id(m) in present)]
        self.logger.info(
            f"{len(present)} off-site mod(s) present on disk -> installing; "
            f"{len(manual) - len(present)} still need a manual download.")
        return filtered, manual_results

    def install_collection(self, collection_data: Dict[str, Any],
                          downloads_path: Union[str, Path]) -> List[InstallationResult]:
        """
        Install all mods from a collection.

        Args:
            collection_data: Full collection.json data
            downloads_path: Path to downloaded mod files

        Returns:
            List of installation results
        """
        results = []

        # Set aside mods that can't be auto-installed (external/bundled sources)
        collection_data, manual_results = self._partition_manual_mods(collection_data, downloads_path)
        results.extend(manual_results)

        # First, scan for existing installations (needs downloads to predict folders)
        self.logger.info("Scanning for existing installations...")
        self._archive_cache = self._build_archive_cache(downloads_path)
        scan_result = self.scan_existing_installations(collection_data, downloads_path)

        # Create filtered collection with only missing mods
        if scan_result["installed_count"] > 0:
            self.logger.info(f"Found {scan_result['installed_count']} already installed mods, filtering collection")
            collection_data = self.create_filtered_collection(collection_data, scan_result)
            
            # Create SKIPPED results for already installed mods
            for installed_mod in scan_result["installed_mods"]:
                results.append(InstallationResult(
                    mod_name=installed_mod["mod_name"],
                    status=InstallResult.SKIPPED,
                    installed_files=[],
                    warnings=[f"Already installed in folder: {installed_mod['folder_name']}"]
                ))
        
        mods = collection_data.get("mods", [])
        self.logger.info(f"After scan filtering: {len(mods)} mods to install, {len(results)} already installed")
        
        # First, validate that all files are downloaded
        validation = self.validate_downloads_complete(collection_data, downloads_path)
        if not validation["all_downloaded"]:
            missing_count = len(validation["missing_files"])
            self.logger.error(f"Cannot start installation: {missing_count} mod files are missing")
            
            # Create failed results for missing mods
            for missing in validation["missing_files"]:
                results.append(InstallationResult(
                    mod_name=missing["mod_name"],
                    status=InstallResult.FAILED,
                    error_message=f"Archive file not found in downloads folder"
                ))
            
            # Only proceed with available mods if user wants partial installation
            available_mods = [mod for mod in mods if any(
                found["mod_name"] == mod.get("name", "") for found in validation["found_files"]
            )]
            
            if not available_mods:
                self.logger.error("No mod files available for installation")
                return results
            
            self.logger.warning(f"Proceeding with partial installation of {len(available_mods)} available mods")
            mods = available_mods
        
        self.logger.info(f"Installing collection with {len(mods)} mods")
        
        for i, mod_data in enumerate(mods, 1):
            mod_name = mod_data.get("name", f"Mod {i}")
            self.logger.info(f"Processing mod {i}/{len(mods)}: {mod_name}")
            
            # Check if mod is already installed
            if self.check_mod_already_installed(mod_name, mod_data, self._archive_cache):
                self.logger.info(f"✓ Mod already installed, skipping: {mod_name}")
                results.append(InstallationResult(
                    mod_name=mod_name,
                    status=InstallResult.SKIPPED,
                    installed_files=[],
                    warnings=["Mod already installed correctly"]
                ))
                continue
            
            self.logger.info(f"Installing mod: {mod_name}")
            result = self.install_mod(mod_data, downloads_path)
            results.append(result)
            
            if result.status == InstallResult.FAILED:
                self.logger.error(f"Failed to install {mod_name}: {result.error_message}")
            elif result.status == InstallResult.SUCCESS:
                self.logger.info(f"Successfully installed {mod_name}: {len(result.installed_files)} files")
        
        # Summary
        successful = sum(1 for r in results if r.status == InstallResult.SUCCESS)
        failed = sum(1 for r in results if r.status == InstallResult.FAILED)
        skipped = sum(1 for r in results if r.status == InstallResult.SKIPPED)
        self.logger.info(f"Installation complete: {successful} successful, {failed} failed, {skipped} skipped")
        
        return results


class _ConcurrencyLimiter:
    """A live-adjustable concurrency gate -- like a semaphore you can resize while
    in use. Workers run ``with limiter:`` around the heavy work; ``set_max(n)``
    changes how many may run at once on the fly (raising it wakes waiters;
    lowering it just makes the next finishers block until active drops to n)."""

    def __init__(self, value: int):
        self._cond = threading.Condition()
        self._max = max(1, int(value))
        self._active = 0

    def __enter__(self):
        with self._cond:
            while self._active >= self._max:
                self._cond.wait()
            self._active += 1
        return self

    def __exit__(self, *exc):
        with self._cond:
            self._active -= 1
            self._cond.notify_all()
        return False

    def set_max(self, n: int):
        with self._cond:
            self._max = max(1, int(n))
            self._cond.notify_all()


# Upper bound on the OS threads the pool spins up; actual concurrency is gated
# live by the limiter, so the spinbox can move anywhere in [1, this] without a
# restart.
MAX_INSTALL_CONCURRENCY = 48   # external-7z extraction is subprocess-bounded in
                               # memory, so high concurrency is safe on many-core
                               # machines (the temp-space throttle still gates by
                               # free scratch space for large archives)


class ParallelFomodInstaller(FomodInstaller):
    """Thread-safe parallel FOMOD installer with concurrent execution support."""

    def __init__(self, staging_path: Union[str, Path], logger: Optional[logging.Logger] = None,
                 max_workers: int = 4, config: Optional[Dict[str, Any]] = None,
                 temp_root: Optional[str] = None):
        """Initialize parallel FOMOD installer."""
        super().__init__(staging_path, logger, temp_root)

        # Threading configuration
        self.max_workers = max_workers
        self.config = config or {}
        # Live-adjustable concurrency gate (see set_concurrency).
        self._limiter = _ConcurrencyLimiter(max_workers)
        # Cooperative cancel: set by cancel(); checked at the top of each mod's
        # install so queued mods bail instantly (running ones finish cleanly).
        self._cancelled = False
        
        # Thread safety locks
        self._temp_dir_lock = threading.Lock()
        self._progress_lock = threading.Lock()
        
        # Progress tracking for concurrent operations
        self._completed_count = 0
        self._total_count = 0
        self._progress_callback: Optional[Callable] = None
        self._installation_callback: Optional[Callable] = None
        
        # Individual temp directories per thread to avoid conflicts
        self._thread_temp_dirs: Dict[int, Path] = {}

    def set_concurrency(self, n: int):
        """Change how many mods install at once -- takes effect immediately, no
        restart. Raising it lets queued workers start; lowering it lets in-flight
        installs finish, then holds at the new ceiling."""
        n = max(1, min(int(n), MAX_INSTALL_CONCURRENCY))
        self.max_workers = n
        self._limiter.set_max(n)
        self.logger.info(f"Install concurrency set to {n} (live)")

    def cancel(self):
        """Request cancellation: queued mods bail immediately; mods already
        extracting finish cleanly (we don't kill threads mid-write). Also bumps
        the limiter so any worker blocked on it wakes up to see the flag."""
        self._cancelled = True
        self._limiter.set_max(MAX_INSTALL_CONCURRENCY)
        self.logger.warning("Installation cancel requested -- draining queued mods.")

    def set_progress_callback(self, callback: Callable[[int, int, str], None]):
        """Set callback for progress updates."""
        self._progress_callback = callback
        
    def set_installation_callback(self, callback: Callable[[str, bool, str, int], None]):
        """Set callback for individual installation completion."""
        self._installation_callback = callback
        
    def _get_thread_temp_dir(self) -> Path:
        """Get or create a temporary directory for the current thread."""
        thread_id = threading.get_ident()
        
        with self._temp_dir_lock:
            if thread_id not in self._thread_temp_dirs:
                # Use shorter directory names to avoid Windows path length issues
                short_id = str(thread_id)[-8:]  # Last 8 digits of thread ID
                thread_temp = Path(tempfile.mkdtemp(prefix=f"nxd_{short_id}_", dir=self._temp_root))
                self._thread_temp_dirs[thread_id] = thread_temp
                self.logger.debug(f"Created thread temp directory: {thread_temp}")
                
            return self._thread_temp_dirs[thread_id]
            
    def _ledger(self):
        """Lazily open the staging ledger (process-wide shared writer). Cached;
        None if unavailable so recording degrades to a no-op."""
        cache = getattr(self, "_ledger_cache", "unset")
        if cache != "unset":
            return cache
        try:
            from utils import local_state
            from utils.vortex_sync import _gen_id
            self._gen_id = _gen_id
            self._ledger_cache = local_state.get_ledger(local_state.db_path_for(self.staging_path))
        except Exception as e:
            self.logger.debug(f"ledger unavailable for install recording: {e}")
            self._ledger_cache = None
        return self._ledger_cache

    def _record_install(self, mod_data, archive_path, result):
        """Write the authoritative archive->folder->identity pairing to the ledger
        AT INSTALL TIME (this thread chose the archive for this mod, so it knows
        the truth -- exactly like Vortex sets the mod's archiveId on install).
        The download id is keyed off the archive (1:1, unique), so two files of one
        mod can never collapse. Best-effort: never fails the install."""
        led = self._ledger()
        if not led:
            return
        try:
            s = mod_data.get("source") or {}
            archive = archive_path.name
            # The folder the files ACTUALLY landed in (robust to naming differences).
            folder = None
            if result.installed_files:
                try:
                    folder = Path(result.installed_files[0]).relative_to(self.staging_path).parts[0]
                except (ValueError, IndexError):
                    folder = None
            if not folder:
                folder = self._get_vortex_folder_name(mod_data.get("name", ""), archive_path, mod_data)
            dl_id = self._gen_id(archive)
            try:
                size = s.get("fileSize") or archive_path.stat().st_size
            except OSError:
                size = s.get("fileSize") or 0
            led.upsert_download(dl_id, archive, s.get("modId"), s.get("fileId"),
                                s.get("md5", "") or "", size, size,
                                s.get("logicalFilename") or mod_data.get("name", "") or "",
                                None, state="installed",
                                game=mod_data.get("domainName") or "", source="nexus")
            led.upsert_mod(folder, dl_id, installer_choices=mod_data.get("choices"),
                           file_count=len(result.installed_files),
                           install_time=int(time.time()), state="installed")
        except Exception as e:
            self.logger.debug(f"install-record skipped for {mod_data.get('name')}: {e}")

    def _cleanup_thread_temp_dirs(self):
        """Clean up all thread temporary directories."""
        with self._temp_dir_lock:
            for thread_id, temp_dir in self._thread_temp_dirs.items():
                if temp_dir.exists():
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=True)
                        self.logger.debug(f"Cleaned up thread temp directory: {temp_dir}")
                    except Exception as e:
                        self.logger.warning(f"Failed to cleanup thread temp dir {temp_dir}: {e}")
            self._thread_temp_dirs.clear()
            
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - explicit, logged purge of ALL temp scratch.

        Removes the per-thread temp dirs and then the whole temp root. When files
        were hardlinked into staging this is near-instant (only the temp directory
        entries are dropped; the data persists via the staging links). Never
        raises -- a temp-cleanup hiccup must not fail the install.
        """
        self._cleanup_thread_temp_dirs()
        try:
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir, ignore_errors=True)
                self.logger.info(f"Purged install temp scratch: {self.temp_dir}")
        except Exception as e:
            self.logger.warning(f"Temp purge issue (ignored): {e}")
        
    def _install_mod_thread_safe(self, mod_data: Dict[str, Any], downloads_path: Union[str, Path]) -> InstallationResult:
        """Thread-safe version of install_mod that uses per-thread temp directories."""
        mod_name = mod_data.get("name", "Unknown Mod")
        thread_id = threading.get_ident()

        # Cancelled while this mod was still queued -> skip it instantly. (Mods
        # already past this point finish their current extract so we never leave a
        # half-written staging folder.)
        if self._cancelled:
            with self._progress_lock:
                self._completed_count += 1
            return InstallationResult(mod_name=mod_name, status=InstallResult.SKIPPED,
                                      installed_files=[], warnings=["Cancelled"])

        try:
            # Use thread-specific temp directory
            original_temp_dir = self.temp_dir
            self.temp_dir = self._get_thread_temp_dir()
            
            # Check if mod is already installed (thread-safe)
            if self.check_mod_already_installed(mod_name, mod_data, getattr(self, "_archive_cache", None)):
                with self._progress_lock:
                    self._completed_count += 1
                    if self._progress_callback:
                        self._progress_callback(min(self._completed_count, self._total_count), self._total_count, mod_name)
                    if self._installation_callback:
                        self._installation_callback(mod_name, True, "Already installed, skipped", 0)

                return InstallationResult(
                    mod_name=mod_name,
                    status=InstallResult.SKIPPED,
                    installed_files=[],
                    warnings=["Mod already installed correctly"]
                )
            
            # Gate the heavy work (extract + copy) behind the live concurrency
            # limiter so the running thread count can be changed mid-install. The
            # already-installed skip above is fast and intentionally ungated.
            with self._limiter:
                # Find the downloaded archive
                archive_path = self._find_mod_archive(mod_data, downloads_path)
                if not archive_path:
                    result = InstallationResult(
                        mod_name=mod_name,
                        status=InstallResult.FAILED,
                        error_message="Downloaded archive not found"
                    )
                else:
                    # Check if mod has installation choices
                    choices = mod_data.get("choices", {})
                    fomod = bool(choices) and choices.get("type") == "fomod"

                    # Retry the "empty result" race. Tiny mods (a single file, a
                    # Pandora behavior cache) extract so fast that under high
                    # concurrency the temp dir is cleaned / the file isn't yet
                    # visible on the DrvFs mount when we scan it -> 0 files copied
                    # -> "no files installed" / "appears to be empty". The same mod
                    # installs fine on another run, so it's a timing race, not a
                    # broken archive. Re-extract to a fresh temp dir a couple times
                    # with a short settle before giving up. Deterministic failures
                    # (archive not found, real extraction errors) carry a different
                    # message and fall straight through.
                    for attempt in range(3):
                        if fomod:
                            result = self._install_fomod(mod_name, archive_path, choices, mod_data)
                        else:
                            result = self._install_simple(mod_name, archive_path, mod_data)
                        msg = (result.error_message or "").lower()
                        race = result.status == InstallResult.FAILED and (
                            "no files were installed" in msg or "appears to be empty" in msg)
                        if not race or attempt == 2:
                            break
                        self.logger.warning(
                            f"{mod_name}: empty install (attempt {attempt + 1}/3) -- "
                            f"likely extract/copy race, retrying after settle")
                        time.sleep(0.75 * (attempt + 1))

                    # Record the install into the ledger NOW, while we hold the
                    # authoritative pairing: THIS archive (identity from mod_data)
                    # went into THIS folder. The system knows the truth at the
                    # moment it acts -- nothing downstream has to re-derive it.
                    if result.status == InstallResult.SUCCESS:
                        self._record_install(mod_data, archive_path, result)

            # Update progress and notify
            with self._progress_lock:
                self._completed_count += 1
                if self._progress_callback:
                    self._progress_callback(min(self._completed_count, self._total_count), self._total_count, mod_name)
                if self._installation_callback:
                    success = result.status in [InstallResult.SUCCESS, InstallResult.SKIPPED]
                    file_count = len(result.installed_files)
                    message = result.error_message or f"Installed {file_count} files"
                    if result.status == InstallResult.SKIPPED:
                        message = "Already installed, skipped"
                    self._installation_callback(mod_name, success, message, file_count)
            
            # Note: Individual extraction cleanup is handled by _install_simple/_install_fomod
            # No need for aggressive cleanup here as each method cleans up its own extraction
            
            # Restore original temp dir reference
            self.temp_dir = original_temp_dir
            return result
            
        except Exception as e:
            error_msg = str(e)
            self.logger.error(f"Thread {thread_id} failed to install {mod_name}: {error_msg}")
            
            # Note: Individual extraction cleanup is handled by _install_simple/_install_fomod even on failure
            
            with self._progress_lock:
                self._completed_count += 1
                if self._progress_callback:
                    self._progress_callback(min(self._completed_count, self._total_count), self._total_count, mod_name)
                if self._installation_callback:
                    self._installation_callback(mod_name, False, error_msg)
                    
            return InstallationResult(
                mod_name=mod_name,
                status=InstallResult.FAILED,
                error_message=error_msg
            )
            
    def install_collection_parallel(self, collection_data: Dict[str, Any], 
                                   downloads_path: Union[str, Path]) -> List[InstallationResult]:
        """
        Install all mods from a collection using parallel processing.
        
        Args:
            collection_data: Full collection.json data
            downloads_path: Path to downloaded mod files
            
        Returns:
            List of installation results
        """
        self.logger.debug(f" install_collection_parallel started")
        results = []

        # Set aside mods that can't be auto-installed (external/bundled sources)
        collection_data, manual_results = self._partition_manual_mods(collection_data, downloads_path)
        results.extend(manual_results)

        # First, scan for existing installations (needs downloads to predict folders)
        self.logger.debug(f" Scanning for existing installations...")
        self._archive_cache = self._build_archive_cache(downloads_path)
        scan_result = self.scan_existing_installations(collection_data, downloads_path)

        # Create filtered collection with only missing mods
        if scan_result["installed_count"] > 0:
            self.logger.debug(f" Found {scan_result['installed_count']} already installed mods, filtering collection")
            collection_data = self.create_filtered_collection(collection_data, scan_result)
            
            # Create SKIPPED results for already installed mods
            for installed_mod in scan_result["installed_mods"]:
                results.append(InstallationResult(
                    mod_name=installed_mod["mod_name"],
                    status=InstallResult.SKIPPED,
                    installed_files=[],
                    warnings=[f"Already installed in folder: {installed_mod['folder_name']}"]
                ))
        
        mods = collection_data.get("mods", [])
        self.logger.debug(f" After filtering: {len(mods)} mods to install, {len(results)} already installed")
        
        # Reset progress tracking
        original_total = scan_result.get("total_mods", len(mods))
        self._completed_count = len(results)  # Start with already-installed count
        self._total_count = original_total
        
        # First, validate that all files are downloaded
        self.logger.debug(f" Starting validation of downloads")
        validation = self.validate_downloads_complete(collection_data, downloads_path)
        self.logger.debug(f" Validation completed: all_downloaded={validation['all_downloaded']}")
        if not validation["all_downloaded"]:
            missing_count = len(validation["missing_files"])
            self.logger.error(f"Cannot start installation: {missing_count} mod files are missing")
            
            # Create failed results for missing mods
            for missing in validation["missing_files"]:
                results.append(InstallationResult(
                    mod_name=missing["mod_name"],
                    status=InstallResult.FAILED,
                    error_message=f"Archive file not found in downloads folder"
                ))
            
            # Only proceed with available mods
            available_mods = [mod for mod in mods if any(
                found["mod_name"] == mod.get("name", "") for found in validation["found_files"]
            )]
            
            if not available_mods:
                self.logger.error("No mod files available for installation")
                return results
            
            self.logger.warning(f"Proceeding with partial installation of {len(available_mods)} available mods")
            mods = available_mods
            self._total_count = len(mods)
        
        # Make the progress total EXACT: whatever's already counted (installed +
        # any missing-marked) plus the mods we're about to attempt, each of which
        # increments exactly once. This stops the numerator overshooting the
        # denominator (the old "4950/4949").
        self._total_count = self._completed_count + len(mods)

        # In robocopy placement mode, cap concurrency so workers x /MT threads
        # can't swamp a cross-disk copy (no effect for hardlink/copy modes).
        capped = self.placement_worker_cap(self.max_workers)
        if capped != self.max_workers:
            self.logger.info(f"Capping install concurrency {self.max_workers} -> {capped} "
                             f"for robocopy placement")
            self.set_concurrency(capped)

        self.logger.info(f"Installing collection with {len(mods)} mods using {self.max_workers} threads")

        # Size the pool to the upper bound; the limiter gates actual concurrency
        # to self.max_workers and can be changed live via set_concurrency().
        pool_size = max(self.max_workers, MAX_INSTALL_CONCURRENCY)
        with concurrent.futures.ThreadPoolExecutor(max_workers=pool_size) as executor:
            # Submit all installation tasks
            future_to_mod = {
                executor.submit(self._install_mod_thread_safe, mod_data, downloads_path): mod_data
                for mod_data in mods
            }
            
            # Collect results as they complete
            for future in concurrent.futures.as_completed(future_to_mod):
                mod_data = future_to_mod[future]
                mod_name = mod_data.get("name", "Unknown Mod")
                
                try:
                    result = future.result(timeout=self.config.get("installation_timeout_seconds", 600))
                    results.append(result)
                    
                    if result.status == InstallResult.FAILED:
                        self.logger.error(f"Failed to install {mod_name}: {result.error_message}")
                    elif result.status == InstallResult.SUCCESS:
                        self.logger.info(f"Successfully installed {mod_name}: {len(result.installed_files)} files")
                    elif result.status == InstallResult.SKIPPED:
                        self.logger.info(f"✓ Mod already installed, skipped: {mod_name}")
                        
                except concurrent.futures.TimeoutError:
                    error_msg = f"Installation timeout after {self.config.get('installation_timeout_seconds', 600)} seconds"
                    self.logger.error(f"Timeout installing {mod_name}: {error_msg}")
                    results.append(InstallationResult(
                        mod_name=mod_name,
                        status=InstallResult.FAILED,
                        error_message=error_msg
                    ))
                except Exception as e:
                    error_msg = str(e)
                    self.logger.error(f"Exception during installation of {mod_name}: {error_msg}")
                    results.append(InstallationResult(
                        mod_name=mod_name,
                        status=InstallResult.FAILED,
                        error_message=error_msg
                    ))
        
        # Summary
        successful = sum(1 for r in results if r.status == InstallResult.SUCCESS)
        failed = sum(1 for r in results if r.status == InstallResult.FAILED)
        skipped = sum(1 for r in results if r.status == InstallResult.SKIPPED)
        self.logger.info(f"Parallel installation complete: {successful} successful, {failed} failed, {skipped} skipped")
        
        return results


def create_fomod_installer(staging_path: Union[str, Path],
                          logger: Optional[logging.Logger] = None,
                          temp_root: Optional[str] = None) -> FomodInstaller:
    """Create a FOMOD installer instance."""
    return FomodInstaller(staging_path, logger, temp_root)


def create_parallel_fomod_installer(staging_path: Union[str, Path],
                                   logger: Optional[logging.Logger] = None,
                                   max_workers: int = 4,
                                   config: Optional[Dict[str, Any]] = None,
                                   temp_root: Optional[str] = None) -> ParallelFomodInstaller:
    """Create a parallel FOMOD installer instance."""
    return ParallelFomodInstaller(staging_path, logger, max_workers, config, temp_root)