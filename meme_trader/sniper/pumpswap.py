"""PumpSwap (pump AMM) quotes in exact integer atoms, ported from the official `@pump-fun/pump-swap-sdk@1.20.0`
(npm integrity sha512-DuBZ5ge3…, its `src/sdk/{buy,sell,fees,util}.ts` and IDL pinned in tests/fixtures/pumpswap/).

Offline math only: no RPC, wallet or signing. It reproduces the SDK, rounding included, and is tested against the
SDK's own outputs (94 quote cases, 5 pool layouts, 2 mints; tests/test_pumpswap.py). The SDK's traps, kept on purpose:
- price math uses EFFECTIVE quote reserves = the quote vault + the pool's signed `virtual_quote_reserves` (an i128,
  can be negative); a sell must still be covered by the REAL quote vault;
- each fee component (LP, protocol, coin creator) is rounded UP separately (`fee` = ceil(amount x bps / 10000));
- buying with a quote amount subtracts one atom before the constant-product step;
- the inverse sell (`sell_quote_input`) can name a base amount whose forward sell nets slightly LESS than asked
  (component rounding): check it with `forward_sell_check` before relying on it;
- fees come from the fee program's schedule (`fees_bps`): flat for non-canonical pools; market-cap tiers for
  canonical pools quoted in SOL-like mints, stable tiers for USDC, exotic flat fees (or flat while unset) otherwise;
  the global config's rates only without a fee config. Never a hardcoded rate.
Divisions follow bn.js: they truncate toward zero.

Not covered, and refused rather than guessed: Token-2022 extensions (transfer fees, hooks) - `decode_mint` accepts a
bare mint only; truncated Pool accounts; amounts or reserves out of range. Pool accounts with undocumented bytes
past the documented fields are decoded but flagged (`decode_pool`). A quote is not a fill: the
observer must still check owners, account relationships, freshness and the context slot (T9-E1's quote contract).
"""
from __future__ import annotations

import math
import struct

SDK_VERSION = "1.20.0"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
DEFAULT_KEY = "11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
WSOL_2022 = "9pan9bMn5HatX4EJdBwg9VgCa7Uz5HL8N1m5D3NdXejP"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_LIKE_QUOTE_MINTS = (DEFAULT_KEY, WSOL, WSOL_2022)
STABLE_QUOTE_MINTS = (USDC,)
TOTAL_TOKEN_SUPPLY = 1_000_000_000_000_000           # the market-cap basis of Mayhem pools (pump-amm)
POOL_DISCRIMINATOR = bytes([241, 154, 109, 4, 17, 177, 109, 188])
POOL_LEN = 270                                       # the 1.20.0 IDL's layout, ending with can_edit_creator_fee
# Where an older Pool account may END (pump.fun's README: "pools written before an appended field existed are
# shorter than the current layout; read the missing trailing fields as 0 / false"): after coin_creator, then each
# appended field. A length between these is a truncated account, never an old layout.
POOL_ENDS = (243, 244, 245, 261, 269, 270, 271)
I128 = (-(1 << 127), (1 << 127) - 1)
U64 = (1 << 64) - 1


class QuoteError(ValueError):
    """The SDK would throw (its message), or an input is refused before quoting."""


def _tdiv(a: int, b: int) -> int:
    """bn.js `div`: truncates toward zero."""
    if b == 0:
        raise QuoteError("Cannot divide by zero.")
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def ceil_div(a: int, b: int) -> int:
    """util.ts ceilDiv: (a + b - 1) / b, with bn.js truncation."""
    if b == 0:
        raise QuoteError("Cannot divide by zero.")
    return _tdiv(a + b - 1, b)


def fee(amount: int, bps: int) -> int:
    return ceil_div(amount * bps, 10_000)


def _slip(slippage: float, sign: int) -> int:
    """The SDK computes its slippage factor in floating point: Math.floor((1 +/- s/100) * 1e9)."""
    return math.floor((1 + sign * slippage / 100) * 1_000_000_000)


# --------------------------------------------------------------------------- fees (fees.ts)
def pool_authority(base_mint: str) -> str:
    """The canonical pump pool's creator: the pump program's PDA ["pool-authority", mint] (pda.ts)."""
    from solders.pubkey import Pubkey
    return str(Pubkey.find_program_address([b"pool-authority", bytes(Pubkey.from_string(base_mint))],
                                           Pubkey.from_string(PUMP_PROGRAM))[0])


def market_cap(base_mint_supply: int, base_reserve: int, quote_reserve: int, mayhem: bool = False) -> int:
    if base_reserve == 0:
        raise QuoteError("Division by zero: pool base token reserves cannot be zero")
    return _tdiv(quote_reserve * (TOTAL_TOKEN_SUPPLY if mayhem else base_mint_supply), base_reserve)


def _tier(tiers: list, cap: int) -> dict:
    if not tiers:
        raise QuoteError("Fee tiers cannot be empty.")
    if cap < tiers[0]["threshold"]:
        return tiers[0]["fees"]
    for t in reversed(tiers):
        if cap >= t["threshold"]:
            return t["fees"]
    return tiers[0]["fees"]


def fees_for_quote_mint(fee_config: dict, is_pump_pool: bool, cap: int, quote_mint: str) -> dict:
    if not is_pump_pool:
        return fee_config["flat"]
    if quote_mint in SOL_LIKE_QUOTE_MINTS:
        return _tier(fee_config["tiers"], cap)
    if quote_mint in STABLE_QUOTE_MINTS:
        return _tier(fee_config["stable_tiers"] or fee_config["tiers"], cap)
    exotic = fee_config["exotic"]
    return fee_config["flat"] if not any(exotic.values()) else exotic


def fees_bps(global_config: dict, fee_config: dict | None, creator: str, base_mint: str, base_mint_supply: int,
             base_reserve: int, quote_reserve: int, quote_mint: str = WSOL, mayhem: bool = False,
             creator_fee_bps: int = 0, pump_pool: bool | None = None) -> dict:
    """computeFeesBps: {lp, protocol, creator} in bps. `quote_reserve` is the EFFECTIVE quote reserve, as in the SDK.
    `pump_pool` may be given (the canonical-pool check derives a PDA otherwise)."""
    if fee_config is not None:
        cap = market_cap(base_mint_supply, base_reserve, quote_reserve, mayhem)
        canonical = pool_authority(base_mint) == creator if pump_pool is None else pump_pool
        f = fees_for_quote_mint(fee_config, canonical, cap, quote_mint)
        if global_config["creator_fee_configurable"] and creator_fee_bps > 0:
            return {**f, "creator": creator_fee_bps}
        return f
    return {"lp": global_config["lp"], "protocol": global_config["protocol"], "creator": global_config["creator"]}


def parse_fees(d: dict) -> dict:
    return {"lp": int(d["lpFeeBps"]), "protocol": int(d["protocolFeeBps"]), "creator": int(d["creatorFeeBps"])}


def parse_fee_config(d: dict) -> dict:
    """The SDK's FeeConfig JSON (decimal strings) as this module's dict."""
    def tiers(xs):
        return [{"threshold": int(t["marketCapLamportsThreshold"]), "fees": parse_fees(t["fees"])} for t in xs]
    return {"flat": parse_fees(d["flatFees"]), "tiers": tiers(d["feeTiers"]),
            "stable_tiers": tiers(d.get("stableFeeTiers") or []), "exotic": parse_fees(d["exoticFlatFees"])}


# --------------------------------------------------------------------------- quotes (buy.ts, sell.ts)
def _check(amount: int, base_reserve: int, quote_reserve: int, virtual: int, slippage: float) -> int:
    """Refusals the SDK leaves to its caller (its own throws are kept separately below)."""
    if not isinstance(amount, int) or amount <= 0 or amount > U64:
        raise QuoteError("amount must be a positive u64")
    if not I128[0] <= virtual <= I128[1]:
        raise QuoteError("virtual quote reserves out of i128 range")
    if base_reserve < 0 or quote_reserve < 0 or base_reserve > U64 or quote_reserve > U64:
        raise QuoteError("reserves must be u64")
    if not 0 <= slippage < 100:
        raise QuoteError("slippage must be in [0, 100)")
    if base_reserve == 0 or quote_reserve == 0:
        raise QuoteError("Invalid input: 'baseReserve' or 'quoteReserve' cannot be zero.")
    eff = quote_reserve + virtual
    if eff <= 0:
        raise QuoteError("effective quote reserves are not positive")
    return eff


def buy_base_input(base: int, slippage: float, base_reserve: int, quote_reserve: int, fees: dict,
                   virtual: int = 0, coin_creator: str = "x") -> dict:
    """Buy exactly `base` atoms: the quote it costs (internal, with fees, and the slippage maximum)."""
    eff = _check(base, base_reserve, quote_reserve, virtual, slippage)
    if base > base_reserve:
        raise QuoteError("Cannot buy more base tokens than the pool reserves.")
    if base_reserve - base == 0:
        raise QuoteError("Pool would be depleted; denominator is zero.")
    q_in = ceil_div(eff * base, base_reserve - base)
    creator = 0 if coin_creator == DEFAULT_KEY else fee(q_in, fees["creator"])
    total = q_in + fee(q_in, fees["lp"]) + fee(q_in, fees["protocol"]) + creator
    return {"internalQuoteAmount": q_in, "uiQuote": total, "maxQuote": _tdiv(total * _slip(slippage, 1), 10 ** 9)}


def buy_quote_input(quote: int, slippage: float, base_reserve: int, quote_reserve: int, fees: dict,
                    virtual: int = 0, coin_creator: str = "x") -> dict:
    """Spend `quote` atoms (fees included): the base atoms received."""
    eff = _check(quote, base_reserve, quote_reserve, virtual, slippage)
    c_bps = 0 if coin_creator == DEFAULT_KEY else fees["creator"]
    effective = _tdiv(quote * 10_000, 10_000 + fees["lp"] + fees["protocol"] + c_bps)
    with_fees = effective + fee(effective, fees["lp"]) + fee(effective, fees["protocol"]) + \
        (0 if coin_creator == DEFAULT_KEY else fee(effective, c_bps))
    if with_fees > quote:
        effective -= with_fees - quote
    inp = effective - 1                                   # (the SDK's one-atom adjustment)
    if eff + inp == 0:
        raise QuoteError("Pool would be depleted; denominator is zero.")
    return {"base": _tdiv(base_reserve * inp, eff + inp), "internalQuoteWithoutFees": effective,
            "maxQuote": _tdiv(quote * _slip(slippage, 1), 10 ** 9)}


def sell_base_input(base: int, slippage: float, base_reserve: int, quote_reserve: int, fees: dict,
                    virtual: int = 0, coin_creator: str = "x") -> dict:
    """Sell exactly `base` atoms: the quote received after fees (and the slippage minimum)."""
    eff = _check(base, base_reserve, quote_reserve, virtual, slippage)
    out = _tdiv(eff * base, base_reserve + base)
    lp, proto = fee(out, fees["lp"]), fee(out, fees["protocol"])
    creator = 0 if coin_creator == DEFAULT_KEY else fee(out, fees["creator"])
    if quote_reserve < out - lp:
        raise QuoteError("Insufficient real quote reserves to cover the sell output.")
    final = out - lp - proto - creator
    if final < 0:
        raise QuoteError("Fees exceed total output; final quote is negative.")
    return {"uiQuote": final, "minQuote": _tdiv(final * _slip(slippage, -1), 10 ** 9), "internalQuoteAmountOut": out}


def sell_quote_input(quote: int, slippage: float, base_reserve: int, quote_reserve: int, fees: dict,
                     virtual: int = 0, coin_creator: str = "x") -> dict:
    """Receive `quote` atoms after fees: the base atoms to sell (an INVERSE quote: see `forward_sell_check`)."""
    eff = _check(quote, base_reserve, quote_reserve, virtual, slippage)
    if quote > quote_reserve:
        raise QuoteError("Cannot receive more quote tokens than the pool quote reserves.")
    c_bps = 0 if coin_creator == DEFAULT_KEY else fees["creator"]
    raw = ceil_div(quote * 10_000, 10_000 - (fees["lp"] + fees["protocol"] + c_bps))
    if raw >= eff:
        raise QuoteError("Invalid input: Desired quote amount exceeds available reserve.")
    return {"internalRawQuote": raw, "base": ceil_div(base_reserve * raw, eff - raw),
            "minQuote": _tdiv(quote * _slip(slippage, -1), 10 ** 9)}


def forward_sell_check(quote: int, slippage: float, base_reserve: int, quote_reserve: int, fees: dict,
                       virtual: int = 0, coin_creator: str = "x") -> dict:
    """Does selling the inverse quote's base amount actually net `quote`? (The SDK's component rounding can leave it
    a few atoms short: never rely on the inverse amount, or a minimum receive derived from it, without this.)"""
    inv = sell_quote_input(quote, slippage, base_reserve, quote_reserve, fees, virtual, coin_creator)
    fwd = sell_base_input(inv["base"], slippage, base_reserve, quote_reserve, fees, virtual, coin_creator)
    return {"output": fwd, "targetQuote": quote, "meetsTarget": fwd["uiQuote"] >= quote,
            "shortfallAtoms": max(0, quote - fwd["uiQuote"])}


# --------------------------------------------------------------------------- accounts
def decode_pool(data: bytes) -> dict:
    """A pump AMM Pool account. Checked on mainnet 2026-10-07 (100 pools): accounts are 287-301 bytes - the struct
    plus space the program allocated - and pump.fun's README documents one field past the 1.20.0 IDL
    (`is_holder_reward`, byte 270; "trading is unchanged"). Some pools carry further, UNDOCUMENTED non-zero bytes past
    it: they're reported (`undocumented_tail`), since the pricing doc says only effective reserves enter a quote, and
    an observer can count or refuse such pools. Older, shorter accounts are read with their missing trailing fields
    as 0 / false, but only when they end exactly at a field boundary: anything else is a truncated account."""
    from solders.pubkey import Pubkey
    n = len(data)
    if n < 243 or (n < 271 and n not in POOL_ENDS):
        raise QuoteError(f"unsupported Pool layout: {n} bytes")
    if data[:8] != POOL_DISCRIMINATOR:
        raise QuoteError("not a Pool account (discriminator)")

    def key(o):
        return str(Pubkey.from_bytes(data[o:o + 32]))

    def flag(o):
        if n <= o:
            return False
        if data[o] > 1:
            raise QuoteError("invalid bool in Pool account")
        return bool(data[o])
    bump, index = data[8], struct.unpack_from("<H", data, 9)[0]
    lp_supply, = struct.unpack_from("<Q", data, 203)
    virtual = int.from_bytes(data[245:261], "little", signed=True) if n >= 261 else 0
    creator_fee_bps = struct.unpack_from("<Q", data, 261)[0] if n >= 269 else 0
    return {"pool_bump": bump, "index": index, "creator": key(11), "base_mint": key(43), "quote_mint": key(75),
            "lp_mint": key(107), "pool_base_token_account": key(139), "pool_quote_token_account": key(171),
            "lp_supply": lp_supply, "coin_creator": key(211), "is_mayhem_mode": flag(243),
            "is_cashback_coin": flag(244), "virtual_quote_reserves": virtual, "creator_fee_bps": creator_fee_bps,
            "can_edit_creator_fee": flag(269), "is_holder_reward": flag(270), "bytes": n,
            "undocumented_tail": any(data[271:])}


def decode_mint(data: bytes, owner: str) -> dict:
    """A bare SPL mint (legacy Token or Token-2022 WITHOUT extensions): its supply and decimals. Any extension
    (a longer Token-2022 account) is refused: transfer fees or hooks would change what a quote means."""
    if owner not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise QuoteError("mint not owned by a token program")
    if len(data) != 82:
        raise QuoteError("unsupported mint: extensions present or not a mint" if len(data) > 82 else "not a mint")
    supply, = struct.unpack_from("<Q", data, 36)
    if data[45] != 1:
        raise QuoteError("mint not initialized")
    return {"supply": supply, "decimals": data[44], "owner": owner}
