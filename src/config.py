from dataclasses import dataclass
import json
import os

@dataclass
class AccessControl:
    NexusAPIKey: str

@dataclass
class VortexSettings:
    DownloadsFolderRoot: str
    ModsFolderRoot: str

@dataclass
class Config:
    AccessControl: AccessControl
    VortexSettings: VortexSettings

def get_config() -> Config:
    # Imagine this dictionary is read from a JSON file
    script_dir = os.path.dirname(os.path.abspath(__file__))  # Get the script's directory
    file_path = os.path.join(script_dir, "config.json")  # Construct the full path

    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)    
        # Create nested objects for each section of the JSON
        access_control = AccessControl(**data["AccessControl"])
        vortex_settings = VortexSettings(**data["VortexSettings"])
    
    # Return a complete Config object
    return Config(AccessControl=access_control, VortexSettings=vortex_settings)
