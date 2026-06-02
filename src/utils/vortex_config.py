"""
Vortex configuration reader utility.

Reads Vortex Mod Manager's configuration files to extract download directories,
mod staging paths, and game-specific settings.
"""

import os
import json
import sqlite3
import logging
from pathlib import Path
from typing import Dict, List, Optional, Union, Any
from dataclasses import dataclass

# Try to import lz4 for potential compressed state files
try:
    import lz4.frame
    HAS_LZ4 = True
except ImportError:
    HAS_LZ4 = False


@dataclass
class VortexGameConfig:
    """Configuration for a specific game in Vortex."""
    game_id: str
    game_name: str
    install_path: str
    mod_staging_path: str
    download_path: str
    enabled: bool = True


@dataclass 
class VortexConfiguration:
    """Complete Vortex configuration."""
    vortex_path: Path
    download_path: Path
    games: Dict[str, VortexGameConfig]
    user_info: Dict[str, Any]
    version: str = "unknown"


class VortexConfigReader:
    """Reads Vortex Mod Manager configuration files."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize the Vortex config reader."""
        self.logger = logger or logging.getLogger(__name__)
        
        # Possible Vortex data locations
        self.data_paths = self._get_vortex_data_paths()
        self.config_data = None
    
    def _log(self, level: str, message: str):
        """Safe logging method that handles both standard and custom loggers."""
        try:
            if hasattr(self.logger, level.lower()):
                getattr(self.logger, level.lower())(message)
            elif hasattr(self.logger, 'log'):
                # Try standard logging interface
                level_map = {
                    'DEBUG': logging.DEBUG,
                    'INFO': logging.INFO,
                    'WARNING': logging.WARNING,
                    'ERROR': logging.ERROR
                }
                self.logger.log(level_map.get(level, logging.INFO), message)
            else:
                # Fallback to print
                print(f"[{level}] {message}")
        except Exception:
            # Ultimate fallback
            print(f"[{level}] {message}")
    
    def _get_vortex_data_paths(self) -> List[Path]:
        """Get possible Vortex data directory paths."""
        paths = []
        
        # Standard AppData location
        appdata = os.environ.get('APPDATA')
        if appdata:
            paths.append(Path(appdata) / "Vortex")
        
        # Shared/multi-user location
        programdata = os.environ.get('PROGRAMDATA')
        if programdata:
            paths.append(Path(programdata) / "Vortex")
        
        # Alternative locations users might use
        userprofile = os.environ.get('USERPROFILE')
        if userprofile:
            paths.append(Path(userprofile) / "Vortex")
            paths.append(Path(userprofile) / "Documents" / "Vortex")
        
        return paths
    
    def find_vortex_installation(self) -> Optional[Path]:
        """Find the active Vortex installation directory."""
        for path in self.data_paths:
            if path.exists():
                # Check for key Vortex files
                state_file = path / "state.v2"
                if state_file.exists():
                    self._log("INFO", f"Found Vortex installation at: {path}")
                    return path
                
                # Check for database files
                db_files = list(path.glob("*.sqlite*")) + list(path.glob("*.db"))
                if db_files:
                    self._log("INFO", f"Found Vortex data at: {path}")
                    return path
        
        self._log("WARNING", "Could not find Vortex installation")
        return None
    
    def read_state_file(self, vortex_path: Path) -> Optional[Dict[str, Any]]:
        """
        Read Vortex state.v2 file.
        
        This file appears to be a JSON-based state file, possibly compressed.
        """
        state_file = vortex_path / "state.v2"
        if not state_file.exists():
            self._log("WARNING", f"State file not found: {state_file}")
            return None
        
        try:
            # Try reading as plain JSON first
            with open(state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self._log("INFO", "Successfully read state.v2 as JSON")
                return data
        
        except PermissionError as e:
            self._log("WARNING", f"Permission denied reading Vortex state file. Vortex may be running: {e}")
            return None
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Try reading as binary (might be compressed)
            try:
                with open(state_file, 'rb') as f:
                    raw_data = f.read()
                
                # Try LZ4 decompression if available
                if HAS_LZ4:
                    try:
                        decompressed = lz4.frame.decompress(raw_data)
                        data = json.loads(decompressed.decode('utf-8'))
                        self._log("INFO", "Successfully read compressed state.v2")
                        return data
                    except:
                        pass
                
                # Try reading as text with different encodings
                for encoding in ['utf-8', 'utf-16', 'latin-1']:
                    try:
                        text_data = raw_data.decode(encoding)
                        data = json.loads(text_data)
                        self._log("INFO", f"Successfully read state.v2 with {encoding} encoding")
                        return data
                    except:
                        continue
                        
            except Exception as e:
                self._log("ERROR", f"Failed to read state file as binary: {e}")
        
        except Exception as e:
            self._log("ERROR", f"Failed to read state file: {e}")
        
        return None
    
    def parse_vortex_config(self, vortex_path: Path) -> Optional[VortexConfiguration]:
        """Parse Vortex configuration from installation directory."""
        try:
            # Read state file
            state_data = self.read_state_file(vortex_path)
            if not state_data:
                return None
            
            # Extract configuration information
            config = VortexConfiguration(
                vortex_path=vortex_path,
                download_path=vortex_path / "downloads",  # default
                games={},
                user_info={}
            )
            
            # Look for settings in the state data
            settings = state_data.get('settings', {})
            
            # Extract download path
            downloads_config = settings.get('downloads', {})
            download_path = downloads_config.get('path')
            if download_path:
                config.download_path = Path(download_path)
                self._log("INFO", f"Found download path: {config.download_path}")
            
            # Extract game configurations
            games_config = settings.get('gameMode', {})
            discovered_games = state_data.get('session', {}).get('gameMode', {}).get('known', {})
            
            for game_id, game_info in discovered_games.items():
                try:
                    game_name = game_info.get('name', game_id)
                    install_path = game_info.get('queryPath', '')
                    
                    # Look for game-specific mod staging path
                    mod_staging = vortex_path / game_id / "mods"
                    if not mod_staging.exists():
                        # Check in settings for custom paths
                        game_settings = settings.get('games', {}).get(game_id, {})
                        staging_path = game_settings.get('modStagingPath')
                        if staging_path:
                            mod_staging = Path(staging_path)
                    
                    game_config = VortexGameConfig(
                        game_id=game_id,
                        game_name=game_name,
                        install_path=install_path,
                        mod_staging_path=str(mod_staging),
                        download_path=str(config.download_path / game_id) if config.download_path else "",
                        enabled=game_info.get('contributed', {}).get('isKnown', False)
                    )
                    
                    config.games[game_id] = game_config
                    self._log("INFO", f"Found game config: {game_name} ({game_id})")
                
                except Exception as e:
                    self._log("WARNING", f"Failed to parse game config for {game_id}: {e}")
            
            # Extract user information
            persistent = state_data.get('persistent', {})
            config.user_info = persistent.get('nexus', {}).get('userInfo', {})
            
            return config
        
        except Exception as e:
            self._log("ERROR", f"Failed to parse Vortex configuration: {e}")
            return None
    
    def get_game_paths(self, game_id: str) -> Optional[Dict[str, str]]:
        """
        Get download and mod paths for a specific game.
        
        Returns:
            Dictionary with 'downloads', 'mods', 'install' paths or None
        """
        if not self.config_data:
            vortex_path = self.find_vortex_installation()
            if not vortex_path:
                return None
            
            self.config_data = self.parse_vortex_config(vortex_path)
            if not self.config_data:
                return None
        
        game_config = self.config_data.games.get(game_id)
        if not game_config:
            return None
        
        return {
            'downloads': game_config.download_path,
            'mods': game_config.mod_staging_path,
            'install': game_config.install_path
        }
    
    def list_managed_games(self) -> List[VortexGameConfig]:
        """List all games managed by Vortex."""
        vortex_path = self.find_vortex_installation()
        if not vortex_path:
            return []
        
        config = self.parse_vortex_config(vortex_path)
        if not config:
            return []
        
        return list(config.games.values())
    
    def get_downloads_folder(self) -> Optional[Path]:
        """Get the global Vortex downloads folder."""
        vortex_path = self.find_vortex_installation()
        if not vortex_path:
            return None
        
        config = self.parse_vortex_config(vortex_path)
        if not config:
            return None
        
        return config.download_path
    
    def auto_detect_paths(self, game_domain: str) -> Dict[str, Optional[str]]:
        """
        Auto-detect download and mod paths for a game domain.
        
        Args:
            game_domain: Game domain name (e.g., 'skyrimspecialedition')
            
        Returns:
            Dictionary with detected paths
        """
        result = {
            'downloads_folder': None,
            'mods_folder': None,
            'game_install': None,
            'vortex_found': False
        }
        
        try:
            # Find Vortex installation
            vortex_path = self.find_vortex_installation()
            if not vortex_path:
                self._log("INFO", "Vortex installation not found")
                return result
            
            result['vortex_found'] = True
            
            # Parse configuration
            config = self.parse_vortex_config(vortex_path)
            if not config:
                self._log("WARNING", "Could not parse Vortex configuration")
                return result
            
            # Get global downloads folder
            result['downloads_folder'] = str(config.download_path)
            
            # Look for game-specific configuration
            game_config = config.games.get(game_domain)
            if game_config:
                result['mods_folder'] = game_config.mod_staging_path
                result['game_install'] = game_config.install_path
                self._log("INFO", f"Found {game_domain} configuration in Vortex")
            else:
                # Fallback: check if folder exists for this game
                game_downloads = config.download_path / game_domain
                if game_downloads.exists():
                    result['downloads_folder'] = str(game_downloads)
                
                game_mods = vortex_path / game_domain / "mods"
                if game_mods.exists():
                    result['mods_folder'] = str(game_mods)
                    
                self._log("INFO", f"Using fallback paths for {game_domain}")
        
        except Exception as e:
            self._log("ERROR", f"Failed to auto-detect paths: {e}")
        
        return result


def get_vortex_config_reader(logger: Optional[logging.Logger] = None) -> VortexConfigReader:
    """Get a configured Vortex config reader instance."""
    return VortexConfigReader(logger)