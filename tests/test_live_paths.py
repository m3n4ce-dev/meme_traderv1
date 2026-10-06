"""Live-execution paths with the network faked out: graduation routing, sell re-quotes,
late landings, rent reclaim, and the sizing agent's hard cap."""
import asyncio
import base64
import json
import types

import pytest

from meme_trader import config
from meme_trader.sniper.curve import FINAL_V_TOKENS
from meme_trader.sniper.events import Launch, Trade
from meme_trader.sniper.execution import LiveExecutor
from meme_trader.sniper.sizing import size_usd, strength
from meme_trader.sniper.strategy import SniperPosition, evaluate_exit
from meme_trader.sniper.tracker import TokenState

P = config.load(config.EXAMPLE)
MINT = "M" * 40 + "pump"


class FakeWallet:
    """attempts: per attempt, the tx_deltas result (None = transaction not visible) and whether it confirmed."""
    pubkey = "W" * 44

    def __init__(self, attempts: list, balance_after: int = 0):
        self.attempts = attempts
        self.balance_after = balance_after
        self.closed = 0
        self.current = None

    simulated_sol_change = 0.0

    def sign(self, tx_b64):
        self.current = self.attempts.pop(0)
        return "raw", "sig"

    def send(self, raw):
        return "sig"

    def simulate_sol_change(self, tx_b64):
        return self.simulated_sol_change

    def tx_deltas(self, sig, mint):
        return self.current[0]

    def token_balance(self, mint):
        return self.balance_after

    def close_empty_token_accounts(self, mint):
        self.closed += 1
        return "close-sig", 0.00203928


@pytest.fixture
def fake_net(monkeypatch):
    sent = []

    def post(url, timeout, data):
        sent.append(data)
        return types.SimpleNamespace(status_code=200, content=b"tx-bytes", text="")
    monkeypatch.setattr("httpx.post", post)
    return sent


def _confirm_from(w):
    return lambda sig, timeout_s=30: w.current[1]


def test_sell_requotes_with_rising_slippage_and_reclaims_rent(fake_net, monkeypatch):
    failed = {"dsol": -0.001005, "dtok": 0, "rent": 0.0, "failed": True}      # landed, failed: definitely not sold
    sold = {"dsol": 0.2, "dtok": -5_000_000, "rent": 0.0, "failed": False}
    w = FakeWallet([(failed, False), (sold, True)], balance_after=0)
    monkeypatch.setattr("meme_trader.wallet.confirm", _confirm_from(w))
    fill = asyncio.run(LiveExecutor(P.sniper.execution, w).sell(MINT, None, 5.0))
    assert fill.ok and fill.tokens == pytest.approx(5.0) and fill.sol == pytest.approx(0.2)
    assert fill.fees_lost == pytest.approx(0.001005)              # the failed attempt still cost fees
    assert [d["slippage"] for d in fake_net] == [15, 25]          # re-quoted, not blindly retried
    assert all(d["pool"] == "auto" for d in fake_net)             # routes to PumpSwap after graduation
    assert w.closed == 1 and fill.rent_reclaimed == pytest.approx(0.00203928 - 0.000005)


def test_buy_not_retried_late_landing_booked_and_rent_kept_out_of_cost(fake_net, monkeypatch):
    w = FakeWallet([(None, False)])
    monkeypatch.setattr("meme_trader.wallet.confirm", _confirm_from(w))
    fill = asyncio.run(LiveExecutor(P.sniper.execution, w).buy(MINT, None, 0.1))
    assert not fill.ok and fill.unknown and fill.signature == "sig" and len(fake_net) == 1   # one attempt only

    bought = {"dsol": -0.10204, "dtok": 7_000_000, "rent": 0.00204, "failed": False}
    w2 = FakeWallet([(bought, False)])                            # confirm timed out, but it landed
    monkeypatch.setattr("meme_trader.wallet.confirm", _confirm_from(w2))
    fill = asyncio.run(LiveExecutor(P.sniper.execution, w2).buy(MINT, None, 0.1))
    assert fill.ok and fill.tokens == pytest.approx(7.0) and fill.sol == pytest.approx(0.1)
    assert fill.rent == pytest.approx(0.00204)                    # reported separately: refundable, not cost


def test_live_errors_become_failed_fills_not_crashes(fake_net, monkeypatch):
    class Boom(FakeWallet):
        def sign(self, tx_b64):
            return "raw", "sig"

        def send(self, raw):
            raise RuntimeError("RPC sendTransaction: slippage exceeded (preflight)")
    w = Boom([])
    fill = asyncio.run(LiveExecutor(P.sniper.execution, w).sell(MINT, None, 5.0))
    assert not fill.ok and "slippage" in fill.error
    assert len(fake_net) == 3                                     # every slippage step was still tried
    failed = {"dsol": -0.000005, "dtok": 0, "rent": 0.0, "failed": True}
    w2 = FakeWallet([(failed, True)])
    monkeypatch.setattr("meme_trader.wallet.confirm", _confirm_from(w2))
    assert not asyncio.run(LiveExecutor(P.sniper.execution, w2).buy(MINT, None, 0.1)).ok


def test_close_empty_token_accounts_builds_close_instruction(monkeypatch):
    pytest.importorskip("solders")
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction

    from meme_trader import wallet as wmod

    kp = Keypair()
    w = wmod.Wallet.__new__(wmod.Wallet)
    w._kp, w.pubkey = kp, str(kp.pubkey())
    token2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    empty, full = str(Keypair().pubkey()), str(Keypair().pubkey())
    sent = []

    def rpc(method, params):
        if method == "getTokenAccountsByOwner":
            acct = lambda pk, amt: {"pubkey": pk, "account": {"owner": token2022, "lamports": 2039280, "data": {
                "parsed": {"info": {"tokenAmount": {"amount": str(amt)}}}}}}
            return {"value": [acct(empty, 0), acct(full, 5)]}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": "11111111111111111111111111111111"}}
        sent.append(params[0])
        return "sig"
    monkeypatch.setattr(wmod, "rpc", rpc)
    assert w.close_empty_token_accounts(MINT) == ("sig", pytest.approx(0.00203928))   # only the empty one
    tx = VersionedTransaction.from_bytes(base64.b64decode(sent[0]))
    ix = tx.message.instructions[0]
    keys = tx.message.account_keys
    assert bytes(ix.data) == bytes([9]) and str(keys[ix.program_id_index]) == token2022
    assert str(keys[ix.accounts[0]]) == empty and tx.verify_with_results() == [True]


def test_sizing_agent_bounds_and_hard_cap():
    z = P.sniper.sizing
    weak, _ = strength(55, 55, 1.0, 0.8, 0, None)
    strong, _ = strength(100, 55, 5.0, 1.0, 5, 1.5)
    assert weak == pytest.approx(0.0) and strong == pytest.approx(1.0)
    assert size_usd(z, weak, "sniper", 20, 150)[0] == z.base_usd
    assert size_usd(z, strong, "sniper", 20, 150)[0] == z.max_usd
    assert size_usd(z, strong, "copy", 20, 150)[0] == pytest.approx(z.max_usd * z.copy_multiplier)
    usd, why = size_usd(z, strong, "sniper", 2, 150)               # thin curve -> liquidity cap
    assert usd < z.max_usd and "liquidity" in why
    over = dict(z, base_usd=50, max_usd=20)                          # misconfigured: still capped
    assert size_usd(config.Params(over), 1.0, "sniper", 100, 150)[0] <= 20


def test_graduated_trades_are_priced_from_market_cap():
    s = TokenState(MINT, Launch(MINT, 0, "dev"), 0)
    s.on_launch(s.launch)
    s.on_trade(Trade(MINT, 5, "w", "buy", 1, 1e6, 0, 0, pool="pump-amm", mcap_sol=400), 2)
    assert s.migrated and s.curve.price == pytest.approx(400 / 1e9) and s.curve.v_tokens == FINAL_V_TOKENS


def test_ladder_exit_profile():
    x = config.Params(json.loads(json.dumps(P.sniper.exit)))
    x["profile"] = "ladder"
    s = TokenState(MINT, Launch(MINT, 0, "dev"), 0)
    s.on_launch(s.launch)
    p0 = s.curve.price
    pos = SniperPosition(MINT, "T", 0, p0, 1000, 1000, 0.05, 0.05, 80, peak_price=p0, exits=[])

    def at(mult):
        k = s.curve.v_sol * s.curve.v_tokens
        v_sol = (k * p0 * mult) ** 0.5
        s.on_trade(Trade(MINT, 1, "w", "buy", 0.1, 0, v_sol, k / v_sol), 2)
        return evaluate_exit(pos, s, 1, x, 1.75)

    frac, why = at(2.1)
    assert why.startswith("ladder 2x") and frac == pytest.approx(0.5)
    pos.ladder_hit, pos.tokens = 1, 500
    assert at(3.5) is None
    frac, why = at(2.3)                     # fell >30% from a 3.5x peak -> ratchet stop
    assert frac == 1.0 and "trail" in why
    pos2 = SniperPosition(MINT, "T", 0, p0 * 4, 1000, 1000, 0.05, 0.05, 80, peak_price=p0 * 4, exits=[])
    assert evaluate_exit(pos2, s, 1, x, 1.75) is None      # 0.57x: ladder holds (no generic -30% stop)
    pos3 = SniperPosition(MINT, "T", 0, p0 * 10, 1000, 1000, 0.05, 0.05, 80, peak_price=p0 * 10, exits=[])
    frac, why = evaluate_exit(pos3, s, 1, x, 1.75)
    assert frac == 1.0 and why.startswith("ladder stop")   # 0.23x <= 0.3x stop


def test_live_state_survives_restart_and_reconciles_with_wallet(tmp_path, monkeypatch):
    import copy as _copy

    from meme_trader.sniper.engine import Engine
    from meme_trader.sniper.execution import PaperExecutor
    from meme_trader.sniper.feeds import Feed

    monkeypatch.setattr("meme_trader.sniper.engine.DATA", tmp_path)
    monkeypatch.setattr("meme_trader.journal.DATA", tmp_path)

    class Quiet(Feed):
        realtime = False

        async def events(self):
            return
            yield

    params = _copy.deepcopy(P)
    eng = Engine(params, Quiet(), PaperExecutor(params.sniper.execution), mode="live", log_to_journal=False)
    for m, tok in (("KEEP" + "k" * 36 + "pump", 1000.0), ("GONE" + "g" * 36 + "pump", 500.0),
                   ("FIX" + "f" * 37 + "pump", 800.0)):
        eng.positions[m] = SniperPosition(m, m[:4], 0, 1e-7, tok, tok, 0.05, 0.05, 70, peak_price=1e-7, exits=[])
    eng.book.sol = 0.8
    eng.save_state()

    class W:                      # wallet after the restart: one sold elsewhere, one partially, plus an orphan
        def all_token_balances(self):
            return {"KEEP" + "k" * 36 + "pump": 1000_000_000, "FIX" + "f" * 37 + "pump": 400_000_000,
                    "ORPH" + "o" * 36 + "pump": 5}

    ex = PaperExecutor(params.sniper.execution)
    ex.wallet = W()
    eng2 = Engine(params, Quiet(), ex, mode="live", log_to_journal=False)
    asyncio.run(eng2.run())
    keep, gone, fix = ("KEEP" + "k" * 36 + "pump", "GONE" + "g" * 36 + "pump", "FIX" + "f" * 37 + "pump")
    assert keep in eng2.positions and gone not in eng2.positions
    assert eng2.positions[fix].tokens == pytest.approx(400.0)
    assert eng2.book.sol == pytest.approx(0.8)
    assert any("aren't tracked" in x["text"] for x in eng2.log)
    # price unknown after restart -> no exit fires even though the default curve price looks like -90%
    s = eng2.tokens[keep]
    assert not s.price_known
    asyncio.run(eng2._check_exit(s))
    assert keep in eng2.positions
