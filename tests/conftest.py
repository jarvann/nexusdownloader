"""Pytest setup: put ``src/`` on the path so tests import the same way the app does."""
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, os.path.abspath(SRC))


SAMPLE_NEXUS_MOD = {
    "name": "Halffaces - Giant Mortar - 2K",
    "version": "1",
    "author": "Halffaces",
    "details": {"category": "Models and Textures"},
    "source": {
        "type": "nexus", "modId": 122495, "fileId": 514061,
        "md5": "7874a45fc75d0445fbebb26352642e80", "fileSize": 11309496,
        "logicalFilename": "Halffaces - Giant Mortar - 2K", "tag": "m1z8V2E5s",
    },
}
