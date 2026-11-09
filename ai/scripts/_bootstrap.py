"""Make ``app`` importable when a script is run as ``python scripts/<name>.py`` from ai/."""

import sys
from pathlib import Path

AI_ROOT = Path(__file__).resolve().parents[1]
if str(AI_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ROOT))
