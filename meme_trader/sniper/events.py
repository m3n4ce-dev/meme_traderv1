"""Normalised feed events. Every source (PumpPortal, synthetic, recorded file) yields these."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass


@dataclass
class Launch:
    mint: str
    ts: float
    creator: str
    name: str = ""
    symbol: str = ""
    uri: str = ""
    dev_buy_tokens: float = 0.0
    dev_buy_sol: float = 0.0
    v_sol: float = 30.0
    v_tokens: float = 1_073_000_000.0
    twitter: str = ""
    telegram: str = ""
    website: str = ""
    kind: str = "launch"


@dataclass
class Trade:
    mint: str
    ts: float
    trader: str
    side: str            # buy | sell
    sol: float
    tokens: float
    v_sol: float         # reserves AFTER this trade
    v_tokens: float
    new_balance: float = -1.0   # trader's token balance after the trade, if the source reports it
    signature: str = ""
    pool: str = "pump"          # pump = bonding curve; pump-amm etc. = graduated
    mcap_sol: float = 0.0       # market cap in SOL after the trade (prices graduated-pool trades)
    kind: str = "trade"


@dataclass
class Migration:
    mint: str
    ts: float
    kind: str = "migration"


@dataclass
class Social:
    """A contract address seen in a Telegram/X post."""
    mint: str
    ts: float
    source: str          # telegram | x | manual
    author: str
    text: str = ""
    kind: str = "social"


@dataclass
class Tick:
    ts: float
    kind: str = "tick"


Event = Launch | Trade | Migration | Social | Tick
_KINDS = {"launch": Launch, "trade": Trade, "migration": Migration, "social": Social, "tick": Tick}


def dumps(e: Event) -> str:
    return json.dumps(dataclasses.asdict(e), separators=(",", ":"))


def loads(line: str) -> Event:
    d = json.loads(line)
    cls = _KINDS[d["kind"]]
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


