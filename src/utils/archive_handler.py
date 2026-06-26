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
        
        # Standard installation paths to check
        tool_paths = [
            # 7-Zip locations
            Path("C:/Program Files/7-Zip/7z.exe"),
            Path("C:/Program Files (x86)/7-Zip/7z.exe"),
            # WinRAR locations  
            Path("C:/Program Files/WinRAR/WinRAR.exe"),
            Path("C:/Program Files (x86)/WinRAR/WinRAR.exe"),
            Path("C:/Program Files/WinRAR/unrar.exe"),
            Path("C:/Program Files (x86)/WinRAR/unrar.exe"),
        ]
        
        # Check PATH first
        for tool_name in ['unrar', '7z', '7z.exe']:
            tool_path = shutil.which(tool_name)
            if tool_path:
                if 'unrar' in tool_name:
                    rarfile.UNRAR_TOOL = tool_path
                    return tool_path
                elif '7z' in tool_name:
                    rarfile.SEVENZIP_TOOL = tool_path
                    return tool_path
        
        # Check standard installation paths
        for tool_path in tool_paths:
            if tool_path.exists():
                if '7z.exe' in str(tool_path):
                    rarfile.SEVENZIP_TOOL = str(tool_path)
                    return str(tool_path)
                elif 'unrar.exe' in str(tool_path) or 'WinRAR.exe' in str(tool_path):
                    rarfile.UNRAR_TOOL = str(tool_path)
                    return str(tool_path)
        
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
    
    return tools

# Find external tools for fallback
_external_tools = _find_external_7z_tools()

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
    
    def _extract_rar(self, archive_path: Path, extract_to: Path, selected_files: Optional[List[str]]):
        """Extract RAR archive."""
        try:
            with rarfile.RarFile(archive_path, 'r') as rf:
                if selected_files:
                    for file_path in selected_files:
                        rf.extract(file_path, extract_to)
                else:
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
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
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
            raise ValueError("7-Zip extraction timed out after 5 minutes")
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
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
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
            raise ValueError("WinRAR extraction timed out after 5 minutes")
        except FileNotFoundError:
            raise ValueError(f"WinRAR executable not found: {winrar_exe}")
    
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