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
    """dict with fast attribute access (p.capital.per_trade_sol).

    Every key is mirrored as a plain instance attribute, so reading config in hot loops is a normal
    attribute lookup (an earlier __getattribute__ override cost ~37% of backtest runtime). Nested
    dicts are converted once, on write. Instance attributes shadow dict methods, so a section may
    be called e.g. `copy`. Keep writes going through item assignment (p["x"] = ...) so both stay in sync.
    """

    def __init__(self, data=None, **kw):
        super().__init__()
        for k, v in dict(data or {}, **kw).items():
            self[k] = v

    def __setitem__(self, key, value) -> None:
        if isinstance(value, dict) and not isinstance(value, Params):
            value = Params(value)
        dict.__setitem__(self, key, value)
        if isinstance(key, str) and key.isidentifier():
            object.__setattr__(self, key, value)

    def __delitem__(self, key) -> None:
        dict.__delitem__(self, key)
        if isinstance(key, str) and key in self.__dict__:
            del self.__dict__[key]

    def update(self, other=(), **kw) -> None:
        for k, v in dict(other, **kw).items():
            self[k] = v

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def __getattr__(self, key: str) -> Any:      # only reached for keys that don't exist
        raise AttributeError(key)


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
