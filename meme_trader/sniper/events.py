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
    # chain facts (Solana log feed; 0 / -1 in older recordings): ordering, when it happened on chain (the
    # TradeEvent's Clock timestamp, whole seconds) next to `ts` = when we received it, and the fee rates paid
    slot: int = 0
    chain_ts: float = 0.0
    fee_bps: int = -1           # protocol fee, basis points
    creator_fee_bps: int = -1
    event_index: int = -1       # this TradeEvent's place among its transaction's TradeEvents (-1: not recorded):
                                # with the signature, the trade's identity (two identical buys in one tx are two)


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
class Funding:
    """Who first funded a wallet with SOL (from Helius funded-by or the wallet's first transaction)."""
    wallet: str
    ts: float
    funder: str = ""          # "" = unknown (e.g. an old wallet with long history)
    funder_type: str = ""     # e.g. "exchange" when the source labels it
    kind: str = "funding"


@dataclass
class Metadata:
    """Social links fetched from a launch's metadata URI, stamped when they ARRIVED (not at launch time),
    so replays and model training only know them from that moment, like the live bot did."""
    mint: str
    ts: float
    twitter: str = ""
    telegram: str = ""
    website: str = ""
    kind: str = "metadata"


@dataclass
class Health:
    """Written into recordings once a minute: what the feed looked like, so research can drop degraded
    stretches and price results in USD."""
    ts: float
    host: str = ""
    gap_pct: float | None = None   # share of trades missing (reserve-chain check)
    lag_s: float | None = None     # median receive time minus on-chain time
    degraded: str = ""             # why entries were blocked by the feed, "" = healthy
    sol_usd: float = 0.0
    kind: str = "health"


@dataclass
class Reconcile:
    """Which version of a trade event the chain kept, when the feed delivered two that differ (a fork: the first copy
    came from a block that didn't survive): the slot the transaction landed in, from an RPC lookup. Recorded with its
    arrival time, so a replay repairs the coin's state exactly as the live bot did (a sixth review, 2026-10-07)."""
    ts: float
    mint: str
    signature: str
    event_index: int
    slot: int = 0                # the landed slot; 0 = not established (not found, or matches no delivered copy)
    status: str = ""             # the RPC's confirmation status, or why it couldn't be established
    source: str = ""             # how it was established (method, provider host)
    err: str = ""                # the transaction FAILED on chain (its error): its trade never happened
    content: str = ""            # the event as decoded from the transaction itself (JSON list), "" = not fetched
    kind: str = "reconcile"


@dataclass
class Tick:
    ts: float
    kind: str = "tick"


Event = Launch | Trade | Migration | Social | Funding | Metadata | Health | Reconcile | Tick
_KINDS = {"launch": Launch, "trade": Trade, "migration": Migration, "social": Social, "funding": Funding,
          "metadata": Metadata, "health": Health, "reconcile": Reconcile, "tick": Tick}


def dumps(e: Event) -> str:
    return json.dumps(dataclasses.asdict(e), separators=(",", ":"))


def loads(line: str) -> Event:
    d = json.loads(line)
    cls = _KINDS[d["kind"]]
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


