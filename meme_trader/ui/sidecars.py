"""The Desk's view of things that run beside the bot: the wallet-study recorder (and its on/off control) and the
research tests' progress. Read from their files; the recorder runs as its own service."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import ROOT
from ..wallets.recorder import DATA as WALLET_DATA, control_state, read_control

POLICIES = ROOT / "research" / "policies"
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _json(p: Path):
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except ValueError:
        return None


def recorder_info(data_dir: Path = WALLET_DATA, now: float | None = None) -> dict:
    now = now or time.time()
    st = _json(Path(data_dir) / "status.json") or {}
    ctl = read_control(data_dir)
    active, why = control_state(ctl, now)
    age = now - st["updated"] if st.get("updated") else None
    files = sorted(Path(data_dir).glob("trades-*.jsonl*"))
    pauses = []
    p = Path(data_dir) / "pauses.jsonl"
    if p.exists():
        for line in p.read_text().splitlines()[-5:]:
            try:
                pauses.append(json.loads(line))
            except ValueError:
                pass
    gb = st.get("stream_gb_by_day") or {}
    return {"running": age is not None and age < 90, "status_age_s": age, "mode": st.get("mode"),
            "active": active, "pause_reason": why, "control": ctl, "pools": st.get("pools_eligible"),
            "rows_by_day": st.get("rows_by_day") or {}, "coverage": st.get("coverage_est"),
            "gb_today": gb.get(datetime.now(timezone.utc).strftime("%Y-%m-%d")),
            "mb_on_disk": round(sum(f.stat().st_size for f in files) / 1e6, 1), "days_of_files": len(files),
            "errors": (st.get("errors") or [])[-3:], "pauses": pauses}


def set_recorder(cmd: dict, data_dir: Path = WALLET_DATA, now: float | None = None) -> dict:
    """op: pause | resume | pause_for (hours) | quiet (start, end HH:MM local) | quiet_off."""
    now = now or time.time()
    ctl = read_control(data_dir)
    op = cmd.get("op")
    if op == "pause":
        ctl.update(paused=True, pause_until=None)
    elif op == "resume":
        ctl.update(paused=False, pause_until=None)
    elif op == "pause_for":
        h = float(cmd.get("hours") or 0)
        if not 0 < h <= 72:
            raise ValueError("pause for 0-72 hours")
        ctl.update(paused=False, pause_until=now + h * 3600)
    elif op == "quiet":
        a, b = str(cmd.get("start", "")), str(cmd.get("end", ""))
        if not (HHMM.match(a) and HHMM.match(b)) or a == b:
            raise ValueError("quiet hours need a start and an end, HH:MM")
        ctl["quiet"] = {"start": a, "end": b}
    elif op == "quiet_off":
        ctl["quiet"] = None
    else:
        raise ValueError(f"unknown recorder command {op!r}")
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    tmp = Path(data_dir) / "control.json.tmp"
    tmp.write_text(json.dumps(ctl))
    tmp.replace(Path(data_dir) / "control.json")
    return recorder_info(data_dir, now)


def research_info(now: float | None = None) -> list[dict]:
    """Each registered test's stage and how far along it is."""
    now = now or time.time()
    out = []
    try:
        import yaml

        g = yaml.safe_load((POLICIES / "graduation-v1.yaml").read_text())
        fz = g.get("frozen_at")
        if fz:
            t0 = (fz.timestamp() if hasattr(fz, "timestamp")
                  else datetime.fromisoformat(str(fz).replace("Z", "+00:00")).timestamp())
            days = (now - t0) / 86400
            gates = g.get("gates") or {}
            need = gates.get("min_days", 14)
            out.append({"name": "graduation-v1", "what": "graduation plays, frozen rules",
                        "stage": "holdout", "progress": min(1.0, days / need),
                        "text": f"holdout day {days:.1f} of {need}, then `research final graduation-v1`"
                                f" (also needs {gates.get('min_trades', 150)} trades)"})
    except Exception:
        pass
    lock = _json(POLICIES / "wallets-v1.lock.json")
    if lock:
        start = datetime.fromisoformat(lock["registered_at"].replace("Z", "+00:00")).timestamp()
        if lock.get("frozen_at"):
            fz = datetime.fromisoformat(lock["frozen_at"].replace("Z", "+00:00")).timestamp()
            d = (now - fz) / 86400
            out.append({"name": "wallets-v1", "what": "follow repeat-early wallets", "stage": "period B",
                        "progress": min(1.0, d / 14),
                        "text": f"testing {len(lock.get('wallets') or [])} frozen wallets: day {d:.1f} of 14+"})
        else:
            d = (now - start) / 86400
            out.append({"name": "wallets-v1", "what": "follow repeat-early wallets", "stage": "period A",
                        "progress": min(1.0, d / 14),
                        "text": f"picking wallets: day {d:.1f} of 14, then `python -m meme_trader.wallets freeze wallets-v1`"})
    return out
