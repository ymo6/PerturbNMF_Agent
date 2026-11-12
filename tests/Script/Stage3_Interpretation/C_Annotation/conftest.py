"""Put the C_Annotation script folders on sys.path (their folder names are not importable packages)."""

import sys
from pathlib import Path

ROOT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "Stage3_Interpretation"
    / "C_Annotation"
)
for sub in ("2.Synthesis", "api", "0.Prepare"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)
