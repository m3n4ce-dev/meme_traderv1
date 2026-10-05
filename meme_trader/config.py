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


_HELP: dict | None = None


def example_help() -> dict[str, str]:
    """{'sniper.late.min_buyers': 'unique buyers in flow_window_s', ...}: the comment after each setting in
    params.example.yaml (plus its continuation lines), so the dashboard can explain every setting."""
    global _HELP
    if _HELP is not None:
        return _HELP
    import re

    out: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    last, last_col = None, 0
    for line in EXAMPLE.read_text().splitlines():
        if not line.strip():
            continue
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):(.*)$", line)
        if m:
            ind, key, rest = len(m.group(1)), m.group(2), m.group(3)
            while stack and stack[-1][0] >= ind:
                stack.pop()
            path = ".".join([k for _, k in stack] + [key])
            val, _, comment = rest.partition("#")
            if not val.strip():                     # a section: its children follow, indented
                stack.append((ind, key))
            out[path] = comment.strip()
            last, last_col = path, line.find("#") if "#" in rest else -1
            continue
        c = line.lstrip()
        if c.startswith("#") and last and last_col >= 0 and len(line) - len(c) >= last_col - 2:
            out[last] = (out[last] + " " + c.lstrip("# ").strip()).strip()   # comment continued below
        else:
            last = None
    _HELP = out
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


class ConfigError(ValueError):
    """An invalid setting. Raised before any feed, wallet or order is touched."""


def _require(ok: bool, msg: str) -> None:
    if not ok:
        raise ConfigError(msg)


def _finite_tree(node, path: str = "") -> None:
    import math

    if isinstance(node, dict):
        for k, v in node.items():
            _finite_tree(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _finite_tree(v, f"{path}[{i}]")
    elif isinstance(node, float):
        _require(math.isfinite(node), f"{path} must be a finite number, got {node}")


def _num(sn: dict, key: str, lo: float | None = None, hi: float | None = None, lo_open: bool = False,
         hi_open: bool = False) -> float:
    node = sn
    for k in key.split("."):
        _require(isinstance(node, dict) and k in node, f"sniper.{key} is missing")
        node = node[k]
    _require(isinstance(node, (int, float)) and not isinstance(node, bool), f"sniper.{key} must be a number")
    if lo is not None:
        _require(node > lo if lo_open else node >= lo,
                 f"sniper.{key} must be {'>' if lo_open else '>='} {lo}, got {node}")
    if hi is not None:
        _require(node < hi if hi_open else node <= hi,
                 f"sniper.{key} must be {'<' if hi_open else '<='} {hi}, got {node}")
    return node


def validate_sniper(sn: dict) -> None:
    """Ranges and cross-field rules for the pump.fun engine. A zero budget, NaN drawdown limit or empty
    retry list would crash mid-trade or silently disable a safeguard, so they fail here instead."""
    _finite_tree(sn, "sniper")
    start = _num(sn, "capital.starting_sol", 0, lo_open=True)
    _num(sn, "capital.buy_sol", 0, start, lo_open=True)
    _require(isinstance(sn["capital"].get("max_open_positions"), int) and sn["capital"]["max_open_positions"] >= 1,
             "sniper.capital.max_open_positions must be a whole number >= 1")
    _num(sn, "capital.daily_loss_limit_sol", 0, lo_open=True)
    _num(sn, "capital.max_drawdown_pct", 0, 100, lo_open=True)
    _num(sn, "capital.min_sol_reserve", 0, start, hi_open=True)
    base = _num(sn, "sizing.base_usd", 0, lo_open=True)
    _num(sn, "sizing.max_usd", base)
    _num(sn, "sizing.copy_multiplier", 0, lo_open=True)
    _num(sn, "sizing.max_pct_of_curve_sol", 0, 100, lo_open=True)
    _num(sn, "sizing.sol_usd_fallback", 0, lo_open=True)
    _num(sn, "entry.min_score", 0, 100)
    min_age = _num(sn, "entry.min_age_s", 0)
    _num(sn, "entry.max_age_s", min_age, lo_open=True)
    _num(sn, "entry.bundle_window_s", 0)
    _num(sn, "exit.stop_loss_pct", 0, 100, lo_open=True, hi_open=True)
    _num(sn, "exit.max_hold_s", 0, lo_open=True)
    _require(sn["exit"].get("profile") in ("trail", "ladder"), "sniper.exit.profile must be trail or ladder")
    tiers = sn["exit"].get("trail_tiers")
    _require(isinstance(tiers, list) and tiers, "sniper.exit.trail_tiers must be a non-empty list")
    for t in tiers:
        _require(isinstance(t, dict) and 0 < t.get("trail_pct", 0) < 100,
                 "every exit.trail_tiers trail_pct must be in (0, 100)")
    steps = (sn["exit"].get("ladder") or {}).get("steps")
    _require(isinstance(steps, list) and steps, "sniper.exit.ladder.steps must be a non-empty list")
    for k in ("curve_fee_pct", "platform_fee_pct", "priority_fee_sol"):
        _num(sn, f"execution.{k}", 0)
    _num(sn, "execution.paper_latency_slippage_pct", 0, 100, hi_open=True)
    _num(sn, "execution.slippage_pct", 0, lo_open=True)
    for key, positive in (("sell_slippage_steps", True), ("sell_priority_fee_steps", False)):
        v = sn["execution"].get(key)
        _require(isinstance(v, list) and v, f"sniper.execution.{key} must be a non-empty list")
        for x in v:
            _require(isinstance(x, (int, float)) and not isinstance(x, bool) and (x > 0 if positive else x >= 0),
                     f"sniper.execution.{key} values must be {'> 0' if positive else '>= 0'}, got {x!r}")
    _num(sn, "copy.buy_sol", 0, start, lo_open=True)
    lo = _num(sn, "late.min_curve_pct", 0, 100)
    hi = _num(sn, "late.max_curve_pct", lo, 100)
    _num(sn, "late.exit_curve_pct", hi, 100)
    _num(sn, "late.stop_loss_pct", 0, 100, lo_open=True, hi_open=True)
    _num(sn, "late.max_hold_s", 0, lo_open=True)
    _num(sn, "late.max_age_s", 0, lo_open=True)
    _num(sn, "callouts.interval_s", 0, lo_open=True)
    _num(sn, "callouts.position_usd", 0, lo_open=True)
    _num(sn, "callouts.hold_s", 0, lo_open=True)
    _num(sn, "predict.min_p", 0, 1, hi_open=True)
    _num(sn, "predict.kelly_fraction", 0, 1)
    _num(sn, "predict.up_pct", 0, lo_open=True)
    _num(sn, "predict.down_pct", 0, 100, lo_open=True, hi_open=True)
    _num(sn, "predict.horizon_s", 0, lo_open=True)
    cps = sn["predict"].get("checkpoints_s")
    _require(isinstance(cps, list) and cps and all(isinstance(x, (int, float)) and x > 0 for x in cps),
             "sniper.predict.checkpoints_s must be a non-empty list of positive seconds")
    _num(sn, "risk_adapt.size_mult", 0, 1, lo_open=True)
    _num(sn, "desk.quorum", 0, 1, lo_open=True)
    _require(sn["feed"].get("trades") in ("solana", "pumpportal"), "sniper.feed.trades must be solana or pumpportal")
    _require(sn["feed"].get("commitment") in ("processed", "confirmed"), "sniper.feed.commitment must be processed or confirmed")
    _num(sn, "feed.max_gap_pct", 0, 100, lo_open=True)
    if "agent" in sn:
        _num(sn, "agent.max_buy_usd", 0)
        _num(sn, "agent.max_buys_per_hour", 0)
        _num(sn, "agent.max_actions_per_min", 1)
        _num(sn, "agent.max_deposit_sol", 0)
    if "chat" in sn:
        _num(sn, "chat.timeout_s", 10)
        _num(sn, "chat.approval_timeout_s", 10)
        if (sn["chat"].get("model") or "") not in ("", "opus", "sonnet", "haiku"):
            raise ConfigError("sniper.chat.model must be one of: \"\", opus, sonnet, haiku")
    _num(sn, "feed.stall_s", 0, lo_open=True)
    _num(sn, "feed.max_lag_s", 0)
    _num(sn, "feed.backup_mb_per_day", 0)
    _num(sn, "feed.backup_bank_mb", 0)
    _num(sn, "execution.paper_delay_s", 0, 60)
    if "risk" in sn:
        _num(sn, "risk.max_level", 1, 5)
        _num(sn, "risk.level", 1, 5)
        _require(sn["risk"]["level"] <= sn["risk"]["max_level"], "sniper.risk.level can't be above risk.max_level")
    _require(sn["late"].get("entry_mode", "rule") in ("rule", "window"),
             "sniper.late.entry_mode must be rule or window")
    if "xchain" in sn:
        x = sn["xchain"]
        _require(isinstance(x.get("chains"), list) and set(x["chains"]) <= {"bsc", "base", "eth", "solana"},
                 "sniper.xchain.chains must be a list from: bsc, base, eth, solana")
        for k in ("min_liq_usd", "min_vol_h1_usd", "min_age_min", "max_age_h", "min_mcap_usd", "max_mcap_usd", "size_usd",
                  "max_tax_pct", "extra_slip_pct", "cooldown_h"):
            _num(sn, f"xchain.{k}", 0)
        for k in ("poll_s", "scan_s", "stop_loss_pct", "take_profit_pct", "trail_pct", "max_hold_h"):
            _num(sn, f"xchain.{k}", 0, lo_open=True)
        _num(sn, "xchain.take_profit_fraction", 0, 1, lo_open=True)
        _require(isinstance(x.get("max_open"), int) and x["max_open"] >= 1, "sniper.xchain.max_open must be a whole number >= 1")
        _require(x["min_mcap_usd"] <= x["max_mcap_usd"], "sniper.xchain.min_mcap_usd can't be above max_mcap_usd")
    _require(isinstance((sn.get("market") or {}).get("non_organic_wallets", []), list),
             "sniper.market.non_organic_wallets must be a list of wallet addresses")


def validate(p: dict) -> None:
    c = p["capital"]
    _require(p["mode"] in ("paper", "live"), "mode must be paper or live")
    _require(0 < c["per_trade_sol"] <= c["starting_sol"], "per_trade_sol must be in (0, starting_sol]")
    _require(c["per_trade_sol"] * c["max_open_positions"] <= c["starting_sol"],
             "per_trade_sol * max_open_positions exceeds starting_sol")
    _require(0 < p["exits"]["stop_loss_pct"] < 100, "exits.stop_loss_pct must be in (0, 100)")
    _require(sum(s["sell_fraction"] for s in p["exits"]["take_profit_ladder"]) <= 1.0,
             "exits.take_profit_ladder sell fractions add up to more than 1")
    if "sniper" in p:
        validate_sniper(p["sniper"])
