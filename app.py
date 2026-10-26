"""SR Pro launcher."""
import os
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from srp.ui import run

if __name__ == "__main__":
    run()
