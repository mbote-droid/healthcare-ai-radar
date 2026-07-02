"""Ensures the project root is importable as the test rootdir.

pytest adds the directory containing this file to sys.path, so ``config`` and
``radar`` import cleanly whether tests are run from the root or elsewhere.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
