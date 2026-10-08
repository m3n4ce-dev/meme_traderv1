"""Quote observation tests: reads, evidence, trace bracketing, and liveness (quote v5 / q1)."""
import json
import sqlite3

from meme_trader.sniper import q1, quotes as q
from tests import test_quotes as qt


class TestSuccessfulQuoteRecordsReads:
    """A successful quote records read stages."""

    def test_successful_quote_has_discover_and_accounts_reads(self):
        """A quote via test_quotes fixtures records discover and accounts reads."""
        rec = qt.quoter(qt.chain()).quote(qt.POOL, "buy", 100_000_000)
        assert rec["reason"] == "ok"
        assert rec["v"] == q.RECORD_VERSION
        assert len(rec["reads"]) == 2
        assert rec["reads"][0]["stage"] == "discover"
        assert rec["reads"][0]["outcome"] == "responded"
        assert rec["reads"][1]["stage"] == "accounts"
        assert rec["reads"][1]["outcome"] == "responded"
        o, _ = q1.observation(rec)
        assert o == "up"


class TestCachedPoolMetadata:
    """Cached pool metadata skips discovery."""

    def test_cached_non_sol_quote_no_fetch_no_reads(self):
        """A cached pool with non-SOL quote avoids a fetch and has empty reads."""
        fetch = qt.chain()
        quoter = qt.quoter(fetch)
        # Populate the cache with pool metadata pointing to non-SOL
        quoter.static[qt.POOL] = {"quote_mint": "USVz2KmE1K4iT9wKUBUWMsqzEEFZeFr2B9jvFYYQYGE"}

        rec = quoter.quote(qt.POOL, "buy", 100_000_000)
        assert rec["reason"] == "non_sol_quote"
        assert rec["reads"] == []
        o, _ = q1.observation(rec)
        assert o == "unknown"


class TestReadTimeout:
    """A timeout on the accounts read."""

    def test_accounts_read_timeout_is_down(self):
        """A timeout on the accounts read produces reason 'timeout' and observation 'down'."""
        class ReadTimeout(Exception):
            pass

        def timeout_fetch(url, keys):
            raise ReadTimeout("connection timeout")

        rec = qt.quoter(timeout_fetch).quote(qt.POOL, "buy", 100_000_000)
        assert rec["reason"] == "timeout"
        assert len(rec["reads"]) > 0
        assert rec["reads"][-1]["outcome"] == "failed"
        o, _ = q1.observation(rec)
        assert o == "down"


class TestLocalError:
    """Local errors after successful reads."""

    def test_local_error_after_responded_read(self, monkeypatch):
        """A local error after a responded accounts read gives local_error reason."""
        # Monkeypatch a function called after the accounts read
        def bad_fees(*args, **kwargs):
            raise ValueError("bug in fees calculation")

        monkeypatch.setattr(q.ps, "fees_bps", bad_fees)
        rec = qt.quoter(qt.chain()).quote(qt.POOL, "buy", 100_000_000)
        assert rec["reason"] == "local_error"
        assert len(rec["reads"]) >= 1
        # The accounts read should have responded before the error
        assert rec["reads"][-1]["outcome"] == "responded"
        o, _ = q1.observation(rec)
        assert o == "up"

    def test_url_function_error_gives_local_error_no_reads(self):
        """A Quoter with a url() that raises gives local_error with no reads."""
        def bad_url():
            # This will raise when called
            gen = (_ for _ in ()).throw(RuntimeError("no url available"))
            return gen

        quoter = q.Quoter(bad_url, lambda: 1000, qt.chain(), qt.Clock())
        rec = quoter.quote(qt.POOL, "buy", 100_000_000)
        assert rec["reason"] == "local_error"
        assert rec["reads"] == []
        o, _ = q1.observation(rec)
        assert o == "unknown"


class TestLegacyObservation:
    """Legacy records without reads field."""

    def test_legacy_responded_at_is_up(self):
        """A legacy record with responded_at is 'up'."""
        rec = {"responded_at": 5.0, "reason": "ok"}
        o, evidence = q1.observation(rec)
        assert o == "up"
        assert "legacy" in evidence.lower()

    def test_legacy_timeout_reason_is_down(self):
        """A legacy record with timeout reason is 'down'."""
        rec = {"reason": "timeout"}
        o, evidence = q1.observation(rec)
        assert o == "down"
        assert "legacy" in evidence.lower()

    def test_legacy_non_sol_quote_is_unknown(self):
        """A legacy record with non_sol_quote reason is 'unknown'."""
        rec = {"reason": "non_sol_quote"}
        o, evidence = q1.observation(rec)
        assert o == "unknown"
        assert "legacy" in evidence.lower()


class TestTransportBracketsClipping:
    """Transport brackets respect now parameter."""

    def test_brackets_clip_to_now_and_expose_unobserved_trailing_gap(self):
        """trace_brackets clips to now; trailing gap > 600s is unobserved."""
        start = 1000.0
        # One observation at start+10, window is [start, start+86400), now=start+3600
        trace = [
            {"t": start + 10, "reason": "ok", "observation": "up", "evidence": "test", "down": False, "host": "h1", "degraded": False}
        ]
        result = q1.transport_brackets(trace, start, start + 86400, now=start + 3600)
        assert result["exposure_hours"] == 1.0
        assert result["running"] is True
        # The trailing gap is from start+10 to start+3600, which is 3590s > 600s, so it's unobserved
        assert result["unobserved_hours"] == round((3600 - 10) / 3600, 2)


class TestHostChangeEndsRun:
    """A host change ends a run."""

    def test_host_change_creates_two_runs(self):
        """up h1, down h1, down h2, up h2 creates 2 runs."""
        start = 1000.0
        trace = [
            {"t": start + 0, "reason": "ok", "observation": "up", "evidence": "test", "down": False, "host": "h1", "degraded": False},
            {"t": start + 30, "reason": "timeout", "observation": "down", "evidence": "test", "down": True, "host": "h1", "degraded": False},
            {"t": start + 60, "reason": "rpc_error", "observation": "down", "evidence": "test", "down": True, "host": "h2", "degraded": False},
            {"t": start + 90, "reason": "ok", "observation": "up", "evidence": "test", "down": False, "host": "h2", "degraded": False},
        ]
        result = q1.transport_brackets(trace, start, start + 120)
        assert result["runs"] == 2
        assert result["brackets"][0]["right"] == "host_change"
        assert result["brackets"][1]["left"] == "host_change"
        assert result["brackets"][1]["may_join_previous"] is True


class TestUnknownAttemptsNoObservation:
    """Unknown attempts don't contribute to observed time."""

    def test_unknown_attempts_unobserved(self):
        """Trace with unknowns in the middle doesn't count those gaps as observed."""
        start = 1000.0
        trace = [
            {"t": start + 0, "reason": "ok", "observation": "up", "evidence": "test", "down": False, "host": "h1", "degraded": False},
            {"t": start + 300, "reason": "non_sol_quote", "observation": "unknown", "evidence": "no read", "down": False, "host": "h1", "degraded": False},
            {"t": start + 600, "reason": "non_sol_quote", "observation": "unknown", "evidence": "no read", "down": False, "host": "h1", "degraded": False},
            {"t": start + 900, "reason": "non_sol_quote", "observation": "unknown", "evidence": "no read", "down": False, "host": "h1", "degraded": False},
            {"t": start + 1200, "reason": "ok", "observation": "up", "evidence": "test", "down": False, "host": "h1", "degraded": False},
        ]
        result = q1.transport_brackets(trace, start, start + 1200)
        # All 1200s is unobserved because the gap from 0 to 1200 is > 600s
        assert result["unobserved_hours"] == round(1200 / 3600, 2)
        # Count unknowns in observations
        unknown_count = sum(1 for a in trace if a["observation"] == "unknown")
        assert unknown_count == 3


class TestDegradedResponses:
    """Degraded responses are down for read_path but up for transport."""

    def test_degraded_creates_read_path_run_not_transport(self):
        """trace [up, degraded, up] has 0 transport runs but 1 read_path run."""
        start = 1000.0
        trace = [
            {"t": start + 0, "reason": "ok", "observation": "up", "evidence": "test", "down": False, "host": "h1", "degraded": False},
            {"t": start + 30, "reason": "stale_state", "observation": "up", "evidence": "test", "down": False, "host": "h1", "degraded": True},
            {"t": start + 60, "reason": "ok", "observation": "up", "evidence": "test", "down": False, "host": "h1", "degraded": False},
        ]
        result = q1.transport_brackets(trace, start, start + 120)
        assert result["runs"] == 0
        assert result["read_path"]["runs"] == 1
        assert result["degraded_responses"] == 1


class TestLivenessSplit:
    """Liveness intervals split unobserved time."""

    def test_liveness_splits_unobserved_time(self):
        """Empty trace over 0..7200 with liveness [(0, 3600)] splits unobserved."""
        start = 1000.0
        trace = []
        liveness = [(start + 0, start + 3600)]
        result = q1.transport_brackets(trace, start, start + 7200, liveness=liveness)
        assert result["unobserved_split"]["process_up_no_reads_hours"] == 1.0
        assert result["unobserved_split"]["process_down_or_unknown_hours"] == 1.0


class TestQuoteBookHeartbeat:
    """QuoteBook heartbeat intervals."""

    def test_heartbeat_extends_within_window(self, tmp_path):
        """QuoteBook._beat extends interval within HEARTBEAT_S window."""
        class Clock:
            def __init__(self):
                self.t = 1000.0

            def __call__(self):
                return self.t

        clock = Clock()
        book = q.QuoteBook(tmp_path / "q.db", None, clock=clock)

        # First beat at t=1000
        book._beat(1000)
        # Within HEARTBEAT_S (60s), no new interval
        clock.t = 1030
        book._beat(1030)
        # After HEARTBEAT_S, extends the interval
        clock.t = 1061
        book._beat(1061)
        # After HEARTBEAT_GAP_S (180s), starts a new interval
        clock.t = 1061 + 180 + 5
        book._beat(1061 + 180 + 5)

        liveness = book.liveness()
        assert len(liveness) == 2
        assert liveness[0] == (1000.0, 1061.0)

    def test_attempt_trace_on_v5_record(self, tmp_path):
        """attempt_trace reads v5 records with observation and evidence."""
        db_path = tmp_path / "q.db"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE IF NOT EXISTS attempts (job TEXT NOT NULL, n INTEGER NOT NULL, rec TEXT NOT NULL, "
                    "PRIMARY KEY (job, n))")

        # Insert a v5 record with reads
        rec_v5 = {
            "v": 5,
            "reason": "ok",
            "job": {"started_at": 1000.0, "usable_at": 1005.0},
            "requested_at": 1000.0,
            "finished_at": 1005.0,
            "reads": [
                {"stage": "discover", "started_at": 1000.0, "ended_at": 1001.0, "outcome": "responded"},
                {"stage": "accounts", "started_at": 1001.0, "ended_at": 1005.0, "outcome": "responded"}
            ],
            "host": "rpc.example"
        }
        con.execute("INSERT INTO attempts (job, n, rec) VALUES (?, ?, ?)",
                    ("J1", 1, json.dumps(rec_v5)))
        con.commit()
        con.close()

        trace = q1.attempt_trace(db_path)
        assert len(trace) == 1
        assert trace[0]["observation"] == "up"
        assert "accounts read responded" in trace[0]["evidence"]
