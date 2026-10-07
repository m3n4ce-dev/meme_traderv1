"""Where the exploratory scripts read and write. They ran from a scratch directory on 2026-10-06; here they read the
recordings from the repo's data/ and write their caches and outputs to $MT_EXPLORE_DIR (default
data/research/exploratory/, git-ignored)."""
import os
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
DATA = os.path.join(ROOT, "data") + "/"
OUT = os.environ.get("MT_EXPLORE_DIR", os.path.join(ROOT, "data", "research", "exploratory")).rstrip("/") + "/"
os.makedirs(OUT, exist_ok=True)
