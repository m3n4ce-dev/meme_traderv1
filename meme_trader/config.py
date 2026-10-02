"""Parameter loading. params.yaml overrides params.example.yaml key by key."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config" / "params.example.yaml"
LOCAL = ROOT / "config" / "params.yaml"


class Params(dict):
    """dict with attribute access (p.capital.per_trade_sol). Config keys win over dict methods,
    so a section may be called e.g. `copy`."""

    def __getattribute__(self, key: str) -> Any:
        if not key.startswith("_") and dict.__contains__(self, key):
            v = dict.__getitem__(self, key)
            return Params(v) if isinstance(v, dict) else v
        return super().__getattribute__(key)

    def __getattr__(self, key: str) -> Any:
        try:
            v = self[key]
        except KeyError as e:
            raise AttributeError(key) from e
        return Params(v) if isinstance(v, dict) else v


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_env(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env into os.environ (existing env vars win). Keeps secrets out of git."""
    import os

    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:                                   # empty placeholder = not set
                os.environ.setdefault(k.strip(), v)


def load(path: str | Path | None = None) -> Params:
    load_env()
    data = yaml.safe_load(EXAMPLE.read_text())
    local = Path(path) if path else LOCAL
    if local.exists():
        data = _merge(data, yaml.safe_load(local.read_text()) or {})
    validate(data)
    return Params(data)


def validate(p: dict) -> None:
    c = p["capital"]
    assert p["mode"] in ("paper", "live"), "mode must be paper or live"
    assert 0 < c["per_trade_sol"] <= c["starting_sol"], "per_trade_sol must be in (0, starting_sol]"
    assert c["per_trade_sol"] * c["max_open_positions"] <= c["starting_sol"], \
        "per_trade_sol * max_open_positions exceeds starting_sol"
    assert 0 < p["exits"]["stop_loss_pct"] < 100
    assert sum(s["sell_fraction"] for s in p["exits"]["take_profit_ladder"]) <= 1.0
