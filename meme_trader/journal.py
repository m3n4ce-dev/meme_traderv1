"""Append-only JSONL journal of every agent decision and fill (data/journal-YYYY-MM-DD.jsonl)."""
from __future__ import annotations

import dataclasses
import json
import logging
import time

from . import redact
from .config import ROOT

DATA = ROOT / "data"
log = logging.getLogger("meme_trader")


def _default(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    return str(o)


def record(agent: str, event: str, **fields) -> None:
    DATA.mkdir(exist_ok=True)
    entry = redact.clean({"ts": time.time(), "agent": agent, "event": event, **fields})   # (no credential in a file)
    path = DATA / f"journal-{time.strftime('%Y-%m-%d', time.gmtime())}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(entry, default=_default) + "\n")
    log.info("%-8s %-14s %s", agent, event, {k: v for k, v in entry.items() if k not in ("raw", "ts", "agent", "event")})
