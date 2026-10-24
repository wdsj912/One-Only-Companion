"""Tests package init."""
import os
import sys
from pathlib import Path

# Make sure app/ is importable when running pytest from repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
