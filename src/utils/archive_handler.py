"""
Archive handling utilities for FOMOD installation.

Supports extraction and manipulation of various archive formats including
ZIP, 7Z, RAR, and others commonly used for mod distribution.
"""

import os
import tempfile
import shutil
import zipfile
import logging
import subprocess
from pathlib import Path
from typing import List, Dict, Optional, Union
from dataclasses import dataclass

try:
    import py7zr
    HAS_7ZIP = True
except ImportError:
    HAS_7ZIP = False

try:
    import rarfile
    HAS_RAR = True
    
    # Auto-configure RAR tools if not already set
    def _configure_rar_tools():
        """Auto-configure RAR extraction tools."""
        import shutil
        from pathlib import Path

        # Prefer a NATIVE unrar (RAR's own engine -- fastest on the big solid
        # archives collections ship, and no GUI window). Look on PATH and in the
        # standard WinRAR dir BEFORE 7-Zip: otherwise a 7z on PATH shadows an
        # installed WinRAR and every RAR gets extracted by the slower engine.
        unrar = shutil.which('unrar') or shutil.which('UnRAR.exe')
        if not unrar:
            for p in [Path("C:/Program Files/WinRAR/UnRAR.exe"),
                      Path("C:/Program Files (x86)/WinRAR/UnRAR.exe"),
                      Path("C:/Program Files/WinRAR/Rar.exe"),
                      Path("C:/Program Files (x86)/WinRAR/Rar.exe")]:
                if p.exists():
                    unrar = str(p)
                    break
        if unrar:
            rarfile.UNRAR_TOOL = unrar
            return unrar

        # Fall back to 7-Zip for RAR support.
        sevenz = shutil.which('7z') or shutil.which('7z.exe')
        if not sevenz:
            for p in [Path("C:/Program Files/7-Zip/7z.exe"),
                      Path("C:/Program Files (x86)/7-Zip/7z.exe")]:
                if p.exists():
                    sevenz = str(p)
                    break
        if sevenz:
            rarfile.SEVENZIP_TOOL = sevenz
            return sevenz

        return None
    
    # Try to auto-configure on import
    _rar_tool_path = _configure_rar_tools()
    
except ImportError:
    HAS_RAR = False
    _rar_tool_path = None

def _find_external_7z_tools():
    """Find external 7-Zip or WinRAR tools for fallback extraction."""
    import shutil
    from pathlib import Path
    
    tools = {}
    
    # Check for 7z executable in PATH
    sevenz_path = shutil.which('7z') or shutil.which('7z.exe')
    if sevenz_path:
        tools['7z'] = sevenz_path
    else:
        # Check standard installation paths
        sevenz_paths = [
            Path("C:/Program Files/7-Zip/7z.exe"),
            Path("C:/Program Files (x86)/7-Zip/7z.exe"),
        ]
        for path in sevenz_paths:
            if path.exists():
                tools['7z'] = str(path)
                break
    
    # Check for WinRAR
    winrar_path = shutil.which('winrar') or shutil.which('winrar.exe')
    if winrar_path:
        tools['winrar'] = winrar_path
    else:
        winrar_paths = [
            Path("C:/Program Files/WinRAR/WinRAR.exe"),
            Path("C:/Program Files (x86)/WinRAR/WinRAR.exe"),
        ]
        for path in winrar_paths:
            if path.exists():
                tools['winrar'] = str(path)
                break

    # Native console RAR extractor (UnRAR.exe / Rar.exe). This is the fastest
    # tool for RAR and -- unlike WinRAR.exe -- never pops a GUI window, so it is
    # the preferred backend for single-pass RAR extraction. Discover it even when
    # 7-Zip is present so RAR routes to its own engine.
    unrar_path = shutil.which('unrar') or shutil.which('UnRAR.exe')
    if not unrar_path:
        unrar_paths = [
            Path("C:/Program Files/WinRAR/UnRAR.exe"),
            Path("C:/Program Files (x86)/WinRAR/UnRAR.exe"),
            Path("C:/Program Files/WinRAR/Rar.exe"),
            Path("C:/Program Files (x86)/WinRAR/Rar.exe"),
        ]
        for path in unrar_paths:
            if path.exists():
                unrar_path = str(path)
                break
    if unrar_path:
        tools['unrar'] = unrar_path

    return tools

# Find external tools for fallback
_external_tools = _find_external_7z_tools()


def _primary_extractor_name(ext: str) -> str:
    """Display name of the tool that will PRIMARILY extract a .<ext> archive.

    Reflects the routing in extract_archive (fallbacks aside) using the tools
    discovered once at import, so it costs zero I/O per call."""
    ext = (ext or "").lower().lstrip(".")
    if ext == "zip":
        return "zipfile"
    if ext == "7z":
        if _external_tools.get("7z"):
            return "7-Zip"
        if HAS_7ZIP:
            return "py7zr"
        if _external_tools.get("winrar"):
            return "WinRAR"
        return "7z"
    if ext == "rar":
        p = (_rar_tool_path or "").lower()
        if "unrar" in p:
            return "UnRAR"
        if "winrar" in p or p.endswith("rar.exe"):
            return "WinRAR"
        if "7z" in p:
            return "7-Zip"
        return "UnRAR" if _rar_tool_path else "rar"
    return ext or ""

try:
    import patoolib
    HAS_PATOOL = True
except ImportError:
    HAS_PATOOL = False


@dataclass
class ArchiveInfo:
    """Information about an archive file."""
    path: Path
    format: str
    size: int
    file_count: int
    is_fomod: bool = False
    fomod_info_path: Optional[Path] = None
    module_config_path: Optional[Path] = None


class ArchiveHandler:
    """Handles extraction and manipulation of archive files."""
    
    SUPPORTED_FORMATS = {
        '.zip': 'zip',
        '.7z': '7z',
        '.rar': 'rar',
        '.tar': 'tar',
        '.tar.gz': 'tar.gz',
        '.tar.bz2': 'tar.bz2',
        '.tar.xz': 'tar.xz'
    }
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize archive handler with optional logger."""
        self.logger = logger or logging.getLogger(__name__)
        self._check_dependencies()
    
    def _check_dependencies(self):
        """Check which archive libraries are available."""
        missing = []
        if not HAS_7ZIP:
            missing.append("py7zr (for 7z support)")
        if not HAS_RAR:
            missing.append("rarfile (for RAR support)")
        elif not _rar_tool_path:
            self.logger.warning("rarfile library is available but no RAR extraction tool found. Please install 7-Zip or WinRAR.")
        if not HAS_PATOOL:
            missing.append("patoolib (for additional format support)")
        
        if missing:
            self.logger.warning(f"Missing optional dependencies: {', '.join(missing)}")
        
        # Log successful RAR configuration
        if HAS_RAR and _rar_tool_path:
            self.logger.debug(f"RAR extraction configured with: {_rar_tool_path}")
    
    def get_archive_info(self, archive_path: Union[str, Path]) -> ArchiveInfo:
        """Get information about an archive file."""
        archive_path = Path(archive_path)
        
        if not archive_path.exists():
            raise FileNotFoundError(f"Archive not found: {archive_path}")
        
        # Determine format
        format_type = self._detect_format(archive_path)
        if not format_type:
            raise ValueError(f"Unsupported archive format: {archive_path.suffix}")
        
        # Get basic info
        size = archive_path.stat().st_size
        
        # Count files and check for FOMOD
        file_count = 0
        is_fomod = False
        fomod_info_path = None
        module_config_path = None
        
        try:
            if format_type == 'zip':
                with zipfile.ZipFile(archive_path, 'r') as zf:
                    file_list = zf.namelist()
                    file_count = len(file_list)
                    fomod_info_path, module_config_path = self._check_fomod_files(file_list)
                    is_fomod = fomod_info_path is not None
            
            elif format_type == '7z' and HAS_7ZIP:
                with py7zr.SevenZipFile(archive_path, 'r') as szf:
                    file_list = szf.getnames()
                    file_count = len(file_list)
                    fomod_info_path, module_config_path = self._check_fomod_files(file_list)
                    is_fomod = fomod_info_path is not None
            
            elif format_type == 'rar' and HAS_RAR:
                with rarfile.RarFile(archive_path, 'r') as rf:
                    file_list = rf.namelist()
                    file_count = len(file_list)
                    fomod_info_path, module_config_path = self._check_fomod_files(file_list)
                    is_fomod = fomod_info_path is not None
        
        except Exception as e:
            self.logger.warning(f"Could not analyze archive {archive_path}: {e}")
        
        return ArchiveInfo(
            path=archive_path,
            format=format_type,
            size=size,
            file_count=file_count,
            is_fomod=is_fomod,
            fomod_info_path=Path(fomod_info_path) if fomod_info_path else None,
            module_config_path=Path(module_config_path) if module_config_path else None
        )
    
    def _detect_format(self, archive_path: Path) -> Optional[str]:
        """Detect archive format from file extension."""
        suffix = archive_path.suffix.lower()
        
        # Handle compound extensions
        if archive_path.name.lower().endswith(('.tar.gz', '.tar.bz2', '.tar.xz')):
            return self.SUPPORTED_FORMATS.get('.' + archive_path.name.split('.')[-2] + '.' + archive_path.name.split('.')[-1])
        
        return self.SUPPORTED_FORMATS.get(suffix)
    
    def _check_fomod_files(self, file_list: List[str]) -> tuple[Optional[str], Optional[str]]:
        """Check if archive contains FOMOD files."""
        fomod_info = None
        module_config = None
        
        for file_path in file_list:
            file_path_lower = file_path.lower().replace('\\', '/')
            
            if file_path_lower.endswith('fomod/info.xml'):
                fomod_info = file_path
            elif file_path_lower.endswith('fomod/moduleconfig.xml'):
                module_config = file_path
        
        return fomod_info, module_config
    
    def extract_archive(self, archive_path: Union[str, Path], 
                       extract_to: Union[str, Path],
                       selected_files: Optional[List[str]] = None) -> Path:
        """
        Extract archive to specified directory.
        
        Args:
            archive_path: Path to archive file
            extract_to: Directory to extract to
            selected_files: Optional list of specific files to extract
            
        Returns:
            Path to extraction directory
        """
        archive_path = Path(archive_path)
        extract_to = Path(extract_to)
        
        archive_info = self.get_archive_info(archive_path)
        
        # Create extraction directory
        extract_to.mkdir(parents=True, exist_ok=True)
        
        try:
            if archive_info.format == 'zip':
                self._extract_zip(archive_path, extract_to, selected_files)
            elif archive_info.format == '7z' and HAS_7ZIP:
                self._extract_7z(archive_path, extract_to, selected_files)
            elif archive_info.format == 'rar' and HAS_RAR and _rar_tool_path:
                self._extract_rar(archive_path, extract_to, selected_files)
            elif archive_info.format == 'rar' and HAS_RAR and not _rar_tool_path:
                raise ValueError(
                    "Cannot extract RAR files: rarfile library is installed but no RAR extraction tool found.\n"
                    "Please install one of the following:\n"
                    "- 7-Zip (https://www.7-zip.org/)\n"
                    "- WinRAR (https://www.win-rar.com/)\n"
                    "- unrar command line tool"
                )
            elif HAS_PATOOL:
                self._extract_patool(archive_path, extract_to)
            else:
                raise ValueError(f"Cannot extract {archive_info.format}: missing dependencies")
        
        except Exception as e:
            self.logger.error(f"Failed to extract {archive_path}: {e}")
            raise
        
        return extract_to
    
    def _extract_zip(self, archive_path: Path, extract_to: Path, selected_files: Optional[List[str]]):
        """Extract ZIP archive, falling back to external 7-Zip for formats Python's
        zipfile can't handle.

        Python's zipfile only implements DEFLATE/STORE: a .zip compressed with PPMd
        or LZMA raises NotImplementedError ("That compression method is not
        supported"), and a mislabeled archive (a 7z/rar with a .zip extension)
        raises BadZipFile ("File is not a zip file"). 7z.exe handles PPMd/LZMA zips
        and auto-detects the real format regardless of extension, so route those
        failures to it instead of letting the mod fail to install."""
        try:
            with zipfile.ZipFile(archive_path, 'r') as zf:
                if selected_files:
                    for file_path in selected_files:
                        zf.extract(file_path, extract_to)
                else:
                    zf.extractall(extract_to)
        except (NotImplementedError, zipfile.BadZipFile, RuntimeError) as e:
            if '7z' not in _external_tools:
                raise
            self.logger.warning(
                f"zipfile could not extract {archive_path.name} ({e}); "
                f"falling back to external 7-Zip.")
            self._extract_7z_external(archive_path, extract_to, selected_files)
    
    def _extract_7z(self, archive_path: Path, extract_to: Path, selected_files: Optional[List[str]]):
        """Extract 7Z archive with external fallback for unsupported formats."""
        # Prefer EXTERNAL 7-Zip: it runs as a subprocess (bounded memory, released
        # between mods) and handles long paths natively. py7zr decompresses
        # in-memory, so under high concurrency it bloats RAM and stalls the install
        # (Threadripper at 7% CPU, 95GB RAM) -- which is why it's now the fallback.
        # Order: external 7-Zip -> py7zr (+ long-path check) -> WinRAR.
        if '7z' in _external_tools:
            try:
                self._extract_7z_external(archive_path, extract_to, selected_files)
                return
            except Exception as ext_error:
                self.logger.warning(
                    f"External 7-Zip failed for {archive_path.name} ({ext_error}); "
                    f"trying py7zr.")

        try:
            with py7zr.SevenZipFile(archive_path, 'r') as szf:
                expected = None
                if not selected_files:
                    try:
                        expected = sum(1 for fi in szf.list() if not fi.is_directory)
                    except Exception:
                        expected = None
                if selected_files:
                    szf.extract(extract_to, targets=selected_files)
                else:
                    szf.extractall(extract_to)
            # py7zr writes with plain Python file ops, so on Windows it SILENTLY
            # drops files whose path exceeds MAX_PATH (260) -- no exception. Verify
            # the count so a short extraction is treated as a failure.
            if expected is not None:
                got = sum(len(files) for _root, _dirs, files in os.walk(extract_to))
                if got < expected:
                    raise RuntimeError(
                        f"py7zr extracted only {got}/{expected} files (likely long-path drops)")
            return
        except Exception as e:
            error_msg = str(e)
            self.logger.warning(
                f"py7zr failed to extract {archive_path.name} ({error_msg}); trying WinRAR.")

            if 'winrar' in _external_tools:
                try:
                    self._extract_winrar_external(archive_path, extract_to, selected_files)
                    return
                except Exception as ext_error:
                    self.logger.warning(f"External WinRAR failed: {ext_error}")

            raise ValueError(
                f"Archive {archive_path.name} could not be extracted "
                f"(py7zr: {error_msg}). Install 7-Zip or WinRAR for broader format support.")
    
    def _extraction_timeout(self, archive_path: Path) -> int:
        """Timeout (seconds) for a single-pass external extraction, scaled to
        archive size. A flat 5-minute cap silently killed multi-GB extractions
        mid-run; scale by size assuming a conservative ~10 MB/s floor (covers slow
        disks and highly-compressed archives), clamped to [10 min, 2 h]."""
        try:
            size = archive_path.stat().st_size
        except OSError:
            size = 0
        return max(600, min(7200, size // (10 * 1024 * 1024)))

    def _extract_rar_native(self, archive_path: Path, extract_to: Path,
                            selected_files: Optional[List[str]] = None) -> bool:
        """Extract a RAR in a SINGLE subprocess via the fastest native tool.

        Tries UnRAR/WinRAR first (RAR's own engine -- best on solid/RAR5), then
        7-Zip. ONE invocation per archive, so a solid archive is decompressed
        exactly once -- never once per file. If ``selected_files`` is given they
        are passed as arguments to that single invocation (all extractors accept a
        file list), so a caller wanting a subset still gets one pass, not one pass
        per file and not the whole archive. Returns True on success, False if no
        external tool worked (caller falls back to the rarfile library)."""
        timeout = self._extraction_timeout(archive_path)
        dest = str(extract_to) + os.sep
        files = list(selected_files) if selected_files else []
        attempts = []
        if 'unrar' in _external_tools:
            # unrar/Rar: x=extract with full paths, -o+=overwrite, -y=assume yes.
            # Destination path goes LAST, after any file list.
            attempts.append(('UnRAR', [_external_tools['unrar'], 'x', '-o+', '-y',
                                       str(archive_path), *files, dest]))
        if 'winrar' in _external_tools:
            attempts.append(('WinRAR', [_external_tools['winrar'], 'x', '-o+', '-y',
                                        str(archive_path), *files, dest]))
        if '7z' in _external_tools:
            # 7-Zip handles RAR too. -mmt2 caps threads so many parallel installs
            # don't oversubscribe the CPU (matches the .7z path).
            attempts.append(('7-Zip', [_external_tools['7z'], 'x', str(archive_path),
                                       f'-o{extract_to}', '-y', '-mmt2', *files]))

        for label, cmd in attempts:
            try:
                self.logger.debug(f"RAR single-pass via {label}: {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        timeout=timeout, cwd=extract_to.parent)
                if result.returncode != 0:
                    self.logger.warning(
                        f"{label} failed (code {result.returncode}) for "
                        f"{archive_path.name}: {result.stderr or result.stdout}")
                    continue
                if extract_to.exists() and any(extract_to.iterdir()):
                    self.logger.debug(f"RAR extracted via {label}: {archive_path.name}")
                    return True
            except subprocess.TimeoutExpired:
                self.logger.warning(
                    f"{label} extraction timed out after {timeout}s for {archive_path.name}")
            except FileNotFoundError:
                self.logger.warning(f"{label} executable not found: {cmd[0]}")
        return False

    def _extract_rar(self, archive_path: Path, extract_to: Path, selected_files: Optional[List[str]]):
        """Extract a RAR archive in ONE pass, into temp.

        RAR "repository" archives in collections are typically SOLID and huge
        (12-15 GB LODGen/BodySlide outputs). Pulling files one at a time re-reads
        the whole solid block for every file -- effectively O(n^2), and with the
        subprocess backend it re-opens the whole archive per file, which turned a
        single 12 GB archive into an hour-plus extraction. We always extract in a
        SINGLE invocation instead (optionally scoped to ``selected_files``); the
        caller hardlinks only the files it needs into staging."""
        if self._extract_rar_native(archive_path, extract_to, selected_files):
            return

        # Fallback (no external tool succeeded): rarfile library. Extract the WHOLE
        # archive in one call -- never the per-file loop that caused the O(n^2) hang.
        try:
            with rarfile.RarFile(archive_path, 'r') as rf:
                rf.extractall(extract_to)
        except rarfile.RarCannotExec as e:
            self.logger.error(f"RAR extraction tool not found: {e}")
            raise ValueError(
                "Cannot extract RAR files: No working RAR extraction tool found.\n"
                "Please install one of the following:\n"
                "- 7-Zip (https://www.7-zip.org/)\n"
                "- WinRAR (https://www.win-rar.com/)\n"
                "- unrar command line tool"
            ) from e
        except Exception as e:
            self.logger.error(f"Failed to extract RAR archive {archive_path}: {e}")
            raise
    
    def _extract_patool(self, archive_path: Path, extract_to: Path):
        """Extract using patoolib (fallback for other formats)."""
        patoolib.extract_archive(str(archive_path), outdir=str(extract_to))
    
    def _extract_7z_external(self, archive_path: Path, extract_to: Path, selected_files: Optional[List[str]] = None):
        """Extract 7z archive using external 7-Zip tool."""
        if '7z' not in _external_tools:
            raise ValueError("7-Zip executable not found for external extraction")
        
        sevenz_exe = _external_tools['7z']
        
        # Build command. -mmt2 caps each process to 2 CPU threads so many parallel
        # installs (up to MAX_INSTALL_CONCURRENCY) don't oversubscribe the cores --
        # 7z decompression is largely serial anyway, so total throughput comes from
        # running many mods at once, not many threads per mod.
        cmd = [sevenz_exe, 'x', str(archive_path), f'-o{extract_to}', '-y', '-mmt2']
        
        if selected_files:
            # Add specific files to extract
            cmd.extend(selected_files)
        
        self.logger.debug(f"Running external 7-Zip: {' '.join(cmd)}")

        timeout = self._extraction_timeout(archive_path)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,  # size-scaled; a flat cap killed multi-GB extracts
                cwd=extract_to.parent
            )
            
            if result.returncode != 0:
                error_msg = f"7-Zip extraction failed (code {result.returncode})"
                if result.stderr:
                    error_msg += f": {result.stderr}"
                raise ValueError(error_msg)
            
            self.logger.debug(f"External 7-Zip extraction successful: {result.stdout}")
            
            # Verify extraction completed by checking if files exist
            import time
            max_wait = 10  # Wait up to 10 seconds for file system sync
            wait_time = 0
            while wait_time < max_wait:
                if extract_to.exists() and any(extract_to.iterdir()):
                    break
                time.sleep(0.5)
                wait_time += 0.5
            
            if not extract_to.exists() or not any(extract_to.iterdir()):
                raise ValueError("External 7-Zip extraction completed but no files found in output directory")
            
            # Additional verification: check if expected files exist for FOMOD archives
            if selected_files:
                missing_files = []
                for file_path in selected_files:
                    expected_path = extract_to / file_path
                    if not expected_path.exists():
                        # Check if file exists with normalized path separators
                        normalized_path = extract_to / file_path.replace('\\', '/')
                        if not normalized_path.exists():
                            normalized_path = extract_to / file_path.replace('/', '\\')
                            if not normalized_path.exists():
                                missing_files.append(file_path)
                
                if missing_files:
                    self.logger.warning(f"Some selected files were not extracted: {missing_files}")
            
        except subprocess.TimeoutExpired:
            raise ValueError(f"7-Zip extraction timed out after {timeout}s")
        except FileNotFoundError:
            raise ValueError(f"7-Zip executable not found: {sevenz_exe}")
    
    def _extract_winrar_external(self, archive_path: Path, extract_to: Path, selected_files: Optional[List[str]] = None):
        """Extract archive using external WinRAR tool."""
        if 'winrar' not in _external_tools:
            raise ValueError("WinRAR executable not found for external extraction")
        
        winrar_exe = _external_tools['winrar']
        
        # Build command - WinRAR syntax: winrar x archive.rar destination\
        cmd = [winrar_exe, 'x', str(archive_path)]
        
        # WinRAR expects destination without trailing slash for the x command
        cmd.append(str(extract_to) + os.sep)
        
        if selected_files:
            # Add specific files to extract
            cmd.extend(selected_files)
        
        self.logger.debug(f"Running external WinRAR: {' '.join(cmd)}")

        timeout = self._extraction_timeout(archive_path)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,  # size-scaled; a flat cap killed multi-GB extracts
                cwd=extract_to.parent
            )
            
            if result.returncode != 0:
                error_msg = f"WinRAR extraction failed (code {result.returncode})"
                if result.stderr:
                    error_msg += f": {result.stderr}"
                elif result.stdout:
                    error_msg += f": {result.stdout}"
                raise ValueError(error_msg)
            
            self.logger.debug(f"External WinRAR extraction successful")
            
            # Verify extraction completed by checking if files exist
            import time
            max_wait = 10  # Wait up to 10 seconds for file system sync
            wait_time = 0
            while wait_time < max_wait:
                if extract_to.exists() and any(extract_to.iterdir()):
                    break
                time.sleep(0.5)
                wait_time += 0.5
            
            if not extract_to.exists() or not any(extract_to.iterdir()):
                raise ValueError("External WinRAR extraction completed but no files found in output directory")
            
            # Additional verification: check if expected files exist for FOMOD archives
            if selected_files:
                missing_files = []
                for file_path in selected_files:
                    expected_path = extract_to / file_path
                    if not expected_path.exists():
                        # Check if file exists with normalized path separators
                        normalized_path = extract_to / file_path.replace('\\', '/')
                        if not normalized_path.exists():
                            normalized_path = extract_to / file_path.replace('/', '\\')
                            if not normalized_path.exists():
                                missing_files.append(file_path)
                
                if missing_files:
                    self.logger.warning(f"Some selected files were not extracted: {missing_files}")
            
        except subprocess.TimeoutExpired:
            raise ValueError(f"WinRAR extraction timed out after {timeout}s")
        except FileNotFoundError:
            raise ValueError(f"WinRAR executable not found: {winrar_exe}")
    
    def extractor_name(self, archive_path: Union[str, Path]) -> str:
        """Display name of the tool that will primarily extract this archive
        (e.g. 'zipfile', '7-Zip', 'UnRAR'). Best-effort, by extension; never raises."""
        try:
            return _primary_extractor_name(Path(archive_path).suffix)
        except Exception:
            return ""

    def list_archive_contents(self, archive_path: Union[str, Path]) -> List[str]:
        """List all files in an archive."""
        archive_path = Path(archive_path)
        archive_info = self.get_archive_info(archive_path)
        
        try:
            if archive_info.format == 'zip':
                with zipfile.ZipFile(archive_path, 'r') as zf:
                    return zf.namelist()
            
            elif archive_info.format == '7z' and HAS_7ZIP:
                with py7zr.SevenZipFile(archive_path, 'r') as szf:
                    return szf.getnames()
            
            elif archive_info.format == 'rar' and HAS_RAR:
                with rarfile.RarFile(archive_path, 'r') as rf:
                    return rf.namelist()
            
            else:
                raise ValueError(f"Cannot list contents of {archive_info.format}: missing dependencies")
        
        except Exception as e:
            self.logger.error(f"Failed to list archive contents {archive_path}: {e}")
            return []
    
    def extract_fomod_files(self, archive_path: Union[str, Path], temp_dir: Optional[Path] = None) -> Dict[str, str]:
        """
        Extract FOMOD configuration files from archive.
        
        Returns:
            Dictionary with 'info.xml' and/or 'ModuleConfig.xml' contents
        """
        archive_path = Path(archive_path)
        archive_info = self.get_archive_info(archive_path)
        
        if not archive_info.is_fomod:
            return {}
        
        # Use temp directory for extraction
        if temp_dir is None:
            temp_dir = Path(tempfile.mkdtemp())
            cleanup_temp = True
        else:
            cleanup_temp = False
        
        try:
            fomod_files = {}
            file_list = self.list_archive_contents(archive_path)
            
            # Find and extract FOMOD files
            fomod_targets = []
            for file_path in file_list:
                file_path_lower = file_path.lower().replace('\\', '/')
                if file_path_lower.endswith('fomod/info.xml') or file_path_lower.endswith('fomod/moduleconfig.xml'):
                    fomod_targets.append(file_path)
            
            if fomod_targets:
                self.extract_archive(archive_path, temp_dir, fomod_targets)
                
                # Read extracted files
                for target in fomod_targets:
                    extracted_path = temp_dir / target
                    if extracted_path.exists():
                        file_key = 'info.xml' if 'info.xml' in target.lower() else 'ModuleConfig.xml'
                        with open(extracted_path, 'r', encoding='utf-8') as f:
                            fomod_files[file_key] = f.read()
        
        finally:
            if cleanup_temp and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
        
        return fomod_files


def get_archive_handler(logger: Optional[logging.Logger] = None) -> ArchiveHandler:
    """Get a configured archive handler instance."""
    return ArchiveHandler(logger)