"""Keep credentials out of everything the bot writes down: logs, the journal, the dashboard's messages, research
records and review packages. RPC and API URLs carry keys (a query string, a path segment, a user:password), and
exception texts carry URLs: an HTTP error's message includes the whole request URL (an eleventh review).

- `redact(text)`: every URL keeps only its scheme, host and short path segments (its query, userinfo and key-like
  path segments become <redacted>); `key=`/`token=`-style pairs and every known secret value are replaced too.
- `safe_error(ex)`: an exception as {error class, HTTP status, host, bounded reason} - never an HTTP error's own text.
- `secret_values()`: the known secrets (from .env names that look like keys or endpoints, and URLs registered by
  the code that uses them), and every key-like piece of those URLs.
- `install()`: every log record redacted as it's created; `clean(obj)`: a record's strings redacted.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from urllib.parse import parse_qsl, urlsplit

MARK = "<redacted>"
_URL = re.compile(r"\b(?:https?|wss?)://[^\s\"'<>`]+", re.I)
# key=value pairs outside URLs: explicit credential names with = or :, generic ones (token, key) only with =, so
# ordinary text ("token: <mint>") stays readable; and bearer tokens
_PAIR = re.compile(r"((?:\b(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|password|passwd)\b[\"']?\s*[=:]\s*"
                   r"|\b(?:token|key)=)[\"']?)[^\s\"'&,;)}\]]{6,}"
                   r"|(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I)
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|_URL|_URI|ENDPOINT|WEBHOOK)", re.I)
_lock = threading.Lock()
_known: set[str] = set()
_env_loaded: tuple | None = None


def secret_pieces(value: str) -> set[str]:
    """The secret-bearing parts of a value: the whole of a non-URL value; a URL's query values, password and
    key-like path segments."""
    v = (value or "").strip().strip("'\"")
    if not v:
        return set()
    if not _URL.fullmatch(v):
        return {v} if len(v) >= 12 else set()
    u = urlsplit(v)
    out = {x for _, x in parse_qsl(u.query, keep_blank_values=True) if len(x) >= 8}
    out |= {seg for seg in u.path.split("/") if _keylike(seg)}
    if u.password and len(u.password) >= 6:
        out.add(u.password)
    if u.query and len(u.query) >= 12:
        out.add(u.query)
    return out


def _keylike(seg: str) -> bool:
    return len(seg) >= 20


def register(value: str) -> None:
    """A URL or secret the code is about to use: its pieces are redacted wherever they appear."""
    p = secret_pieces(value)
    if p:
        with _lock:
            _known.update(p)


_env_checked = 0.0


def _load_env() -> None:
    global _env_loaded, _env_checked
    import time
    now = time.monotonic()
    if _env_loaded is not None and now - _env_checked < 5:
        return
    _env_checked = now
    snap = tuple(sorted((k, v) for k, v in os.environ.items() if _SECRET_NAME.search(k)))
    if snap == _env_loaded:
        return
    pieces = set()
    for _, v in snap:
        pieces |= secret_pieces(v)
    with _lock:
        _known.update(pieces)
        _env_loaded = snap


def secret_values() -> list[str]:
    _load_env()
    with _lock:
        return sorted(_known, key=len, reverse=True)


def _url(m: re.Match) -> str:
    raw = m.group(0)
    tail = ""
    while raw and raw[-1] in ".,;:)]}":                  # sentence punctuation after a URL isn't part of it
        tail, raw = raw[-1] + tail, raw[:-1]
    try:
        u = urlsplit(raw)
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
    except ValueError:
        return MARK + tail
    path = "/".join(MARK if _keylike(seg) else seg for seg in u.path.split("/"))
    return f"{u.scheme}://{host}{port}{path}{'?' + MARK if u.query else ''}{'#' + MARK if u.fragment else ''}{tail}"


def redact(text) -> str:
    """`text` with every credential it may carry replaced by <redacted> (see the module notes)."""
    if not isinstance(text, str):
        text = str(text)
    for s in secret_values():
        if s in text:
            text = text.replace(s, MARK)
    text = _URL.sub(_url, text)
    return _PAIR.sub(lambda m: (m.group(1) or m.group(2)) + MARK, text)


def safe_error(ex: BaseException, limit: int = 160) -> dict:
    """An exception as data safe to keep: its class, its HTTP status and host when it's an HTTP error (whose own
    message embeds the request URL - never kept), else a bounded, redacted reason."""
    out: dict = {"error": type(ex).__name__}
    resp = getattr(ex, "response", None)
    status = getattr(resp, "status_code", None)
    if status is None and isinstance(getattr(ex, "status", None), int):
        status = ex.status                              # aiohttp's ClientResponseError
    url = None
    req = getattr(ex, "_request", None) or (getattr(resp, "request", None) if resp is not None else None)
    if req is not None:
        url = getattr(req, "url", None)
    if url is None and getattr(ex, "request_info", None) is not None:
        url = getattr(ex.request_info, "url", None)
    if url is not None:
        try:
            out["host"] = urlsplit(str(url)).hostname or ""
        except ValueError:
            pass
    if isinstance(status, int):
        out["status"] = status
        out["reason"] = f"HTTP {status}"
    else:
        out["reason"] = redact(str(ex))[:limit]
    return out


def describe(ex: BaseException, limit: int = 160) -> str:
    """safe_error as one line: 'HTTPStatusError: HTTP 403 (host)' / 'ReadTimeout: ...'."""
    e = safe_error(ex, limit)
    host = f" ({e['host']})" if e.get("host") else ""
    return f"{e['error']}: {e['reason']}{host}"


_installed = False


def _clean_record(r: logging.LogRecord) -> logging.LogRecord:
    try:
        msg = r.getMessage()
    except Exception:                                    # noqa: BLE001 - a bad format string: logging reports it
        return r
    red = redact(msg)
    if red != msg:                                       # (untouched otherwise: some formatters read record.args)
        r.msg, r.args = red, None
    if r.exc_info and r.exc_info[1] is not None:         # the traceback stays, its texts redacted (handlers use a
        r.exc_text = redact(logging.Formatter().formatException(r.exc_info))   # record's exc_text as it is)
    if r.stack_info:
        r.stack_info = redact(r.stack_info)
    return r


def install() -> None:
    """Redact every log record as it's created - every logger and handler (uvicorn's, the last-resort stderr
    handler) - before anything formats or writes it. Idempotent."""
    global _installed
    if _installed:
        return
    base = logging.getLogRecordFactory()

    def factory(*a, **kw):
        return _clean_record(base(*a, **kw))
    logging.setLogRecordFactory(factory)
    _installed = True


def clean(obj):
    """A JSON-able value with every string in it redacted (for records written to files)."""
    if isinstance(obj, str):
        return redact(obj) if len(obj) >= 8 else obj
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj
