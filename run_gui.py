#!/usr/bin/env python3
"""
NexusDownloader GUI Entry Point.

This script serves as the main entry point for launching the NexusDownloader
graphical user interface with proper module path setup.
"""

import sys
import os
from pathlib import Path

def main():
    """Main entry point for the GUI application."""
    try:
        # Add the src directory to Python path so relative imports work
        script_dir = Path(__file__).parent
        src_dir = script_dir / "src"
        
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
        
        # Import and run the main GUI application
        from gui.main_window import main as gui_main
        gui_main()
        
    except ImportError as e:
        print(f"Error importing GUI modules: {e}")
        print("Please ensure all dependencies are installed:")
        print("  pip install PySide6>=6.4.0")
        print(f"Current Python path: {sys.path}")
        print(f"Trying to import from: {src_dir}")
        sys.exit(1)
    except Exception as e:
        print(f"Error starting GUI: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()