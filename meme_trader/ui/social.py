"""Post to X and Telegram: from the dashboard's composer, or `python -m meme_trader.social post "text"`.

Every post is made by you, with a click or a command. The AI drafts at most; it never posts. Cards made from paper
trades say PAPER on them.

X runs a pay-per-use API since February 2026 with no free tier: about $0.015 per post, and $0.20 when the post
contains a link (prices as of October 2026; check console.x.com). Two ways to connect:

1. Keys (simplest; posts as the developer account's own user). In the X developer console, open your app, set its
   permissions to "Read and write", then under "Keys and tokens" copy the API key and secret and generate an access
   token and secret: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET.
2. Connect with X (OAuth 2.0, any account). Add X_CLIENT_ID (and X_CLIENT_SECRET for a "Web App" client), and put
   http://127.0.0.1:8787/x/callback in the app's callback URLs. The dashboard's Connect button signs you in on
   x.com. Tokens are kept in data/x_auth.json (readable only by you) and refreshed automatically.

Telegram: TELEGRAM_BOT_TOKEN plus TELEGRAM_CHANNEL_ID (an @channel name or a chat id). The bot must be an admin of
the channel, or a member of the group.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlencode

X_API = "https://api.x.com"
X_AUTHORIZE = "https://x.com/i/oauth2/authorize"
SCOPES = "tweet.read tweet.write users.read media.write offline.access"
POST_USD, LINK_POST_USD = 0.015, 0.20           # X pay-per-use, October 2026
MAX_X_PER_DAY = 25                              # a typo or a loop must not run up the bill
MAX_TG_PER_DAY = 200
# X turns bare domains into links too ("pump.fun/coin/…"), and a link makes the post cost 13x more
LINKISH = re.compile(r"https?://|\b[\w-]+\.(?:com|fun|io|xyz|ai|net|org|app|gg|so|co|me|dev|trade|finance|tv|"
                     r"gl|ly|to|sh|cc|info|bot)\b(?:/\S*)?", re.I)


class SocialError(ValueError):
    pass


def x_cost(text: str) -> float:
    return LINK_POST_USD if LINKISH.search(text or "") else POST_USD


def x_length(text: str) -> int:
    """X counts every link as 23 characters, and emoji and CJK as 2."""
    t = re.sub(r"https?://\S+", "x" * 23, text or "")
    return sum(2 if ord(c) > 0x2FFF else 1 for c in t)


def _enc(s) -> str:
    return quote(str(s), safe="~-._")


def oauth1_header(method: str, url: str, params: dict, ck: str, cs: str, tk: str, ts_: str,
                  nonce: str | None = None, timestamp: str | None = None) -> str:
    """OAuth 1.0a HMAC-SHA1 Authorization header. `params`: query and form fields only (a JSON or multipart body
    isn't part of the signature)."""
    oauth = {"oauth_consumer_key": ck, "oauth_nonce": nonce or secrets.token_hex(16),
             "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": timestamp or str(int(time.time())),
             "oauth_token": tk, "oauth_version": "1.0"}
    pairs = sorted((_enc(k), _enc(v)) for k, v in {**params, **oauth}.items())
    base = "&".join([method.upper(), _enc(url), _enc("&".join(f"{k}={v}" for k, v in pairs))])
    key = f"{_enc(cs)}&{_enc(ts_)}".encode()
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_enc(k)}="{_enc(v)}"' for k, v in sorted(oauth.items()))


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Social:
    def __init__(self, data_dir: Path):
        self.data = Path(data_dir)
        self.auth_path = self.data / "x_auth.json"
        self.log_path = self.data / "social_posts.jsonl"
        self._pending: dict[str, tuple[str, str, float]] = {}       # OAuth state -> (verifier, redirect, when)

    # ---------------------------------------------------------------- status
    @staticmethod
    def _env(name: str) -> str:
        return os.environ.get(name, "").strip()

    def _keys(self) -> tuple[str, str, str, str] | None:
        k = tuple(self._env(n) for n in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"))
        return k if all(k) else None

    def _auth(self) -> dict:
        try:
            return json.loads(self.auth_path.read_text()) if self.auth_path.exists() else {}
        except ValueError:
            return {}

    def _save_auth(self, d: dict) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        tmp = self.auth_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        tmp.replace(self.auth_path)

    def x_method(self) -> str:
        if self._auth().get("refresh_token") or self._auth().get("access_token"):
            return "oauth2"
        return "keys" if self._keys() else ""

    def _today(self) -> list[dict]:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        out = []
        if self.log_path.exists():
            for line in self.log_path.read_text().splitlines()[-1000:]:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ok") and time.strftime("%Y-%m-%d", time.gmtime(r.get("ts", 0))) == day:
                    out.append(r)
        return out

    def status(self) -> dict:
        a, today = self._auth(), self._today()
        xs = [r for r in today if r["to"] == "x"]
        return {
            "x": {"method": self.x_method(), "connected": bool(self.x_method()),
                  "user": (a.get("user") or {}).get("username") or self._env("X_USERNAME") or None,
                  "can_connect": bool(self._env("X_CLIENT_ID")), "posts_today": len(xs),
                  "spent_today_usd": round(sum(r.get("cost_usd", 0) for r in xs), 3), "max_per_day": MAX_X_PER_DAY,
                  "post_usd": POST_USD, "link_post_usd": LINK_POST_USD},
            "telegram": {"configured": bool(self._env("TELEGRAM_BOT_TOKEN") and self._env("TELEGRAM_CHANNEL_ID")),
                         "chat": self._env("TELEGRAM_CHANNEL_ID") or None,
                         "posts_today": sum(1 for r in today if r["to"] == "telegram")},
            "recent": self.recent(8),
        }

    def recent(self, n: int = 20) -> list[dict]:
        if not self.log_path.exists():
            return []
        rows = []
        for line in self.log_path.read_text().splitlines()[-n:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows[::-1]

    def _log(self, row: dict) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps({"ts": time.time(), **row}) + "\n")

    # ---------------------------------------------------------------- X: OAuth 2.0 with PKCE
    def oauth2_start(self, redirect_uri: str) -> str:
        cid = self._env("X_CLIENT_ID")
        if not cid:
            raise SocialError("add your X app's client ID (X_CLIENT_ID) in Controls → API keys first")
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v[2] < 600}
        verifier = secrets.token_urlsafe(64)[:96]
        state = secrets.token_urlsafe(24)
        self._pending[state] = (verifier, redirect_uri, now)
        return X_AUTHORIZE + "?" + urlencode({
            "response_type": "code", "client_id": cid, "redirect_uri": redirect_uri, "scope": SCOPES,
            "state": state, "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256"})

    async def _token_request(self, http, data: dict) -> dict:
        import aiohttp

        cid, secret = self._env("X_CLIENT_ID"), self._env("X_CLIENT_SECRET")
        auth = aiohttp.BasicAuth(cid, secret) if secret else None
        async with http.post(f"{X_API}/2/oauth2/token", data={**data, "client_id": cid}, auth=auth) as r:
            d = await r.json(content_type=None)
            if r.status != 200 or "access_token" not in d:
                raise SocialError(f"X refused the sign-in: {d.get('error_description') or d.get('error') or r.status}")
            return d

    async def oauth2_finish(self, code: str, state: str) -> dict:
        import aiohttp

        got = self._pending.pop(state or "", None)
        if not got or time.time() - got[2] > 600:
            raise SocialError("that sign-in link expired: press Connect X again")
        verifier, redirect, _ = got
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            d = await self._token_request(http, {"grant_type": "authorization_code", "code": code,
                                                 "redirect_uri": redirect, "code_verifier": verifier})
            auth = {"access_token": d["access_token"], "refresh_token": d.get("refresh_token", ""),
                    "expires_at": time.time() + float(d.get("expires_in", 7200)), "scope": d.get("scope", "")}
            async with http.get(f"{X_API}/2/users/me", headers={"Authorization": f"Bearer {auth['access_token']}"}) as r:
                me = (await r.json(content_type=None)).get("data") or {}
        auth["user"] = {k: me.get(k) for k in ("id", "username", "name")}
        self._save_auth(auth)
        return auth["user"]

    def disconnect(self) -> None:
        if self.auth_path.exists():
            self.auth_path.unlink()

    async def _bearer(self, http) -> str:
        a = self._auth()
        if a.get("access_token") and a.get("expires_at", 0) - 60 > time.time():
            return a["access_token"]
        if not a.get("refresh_token"):
            raise SocialError("the X sign-in expired: press Connect X again")
        d = await self._token_request(http, {"grant_type": "refresh_token", "refresh_token": a["refresh_token"]})
        a.update(access_token=d["access_token"], refresh_token=d.get("refresh_token", a["refresh_token"]),
                 expires_at=time.time() + float(d.get("expires_in", 7200)))
        self._save_auth(a)
        return a["access_token"]

    async def _x_headers(self, http, method: str, url: str) -> dict:
        if self.x_method() == "oauth2":
            return {"Authorization": f"Bearer {await self._bearer(http)}"}
        k = self._keys()
        if not k:
            raise SocialError("X isn't connected: add your X keys in Controls → API keys, or use Connect X")
        return {"Authorization": oauth1_header(method, url, {}, *k)}

    async def _x_upload(self, http, png: bytes) -> str:
        import aiohttp

        url = f"{X_API}/2/media/upload/initialize"
        async with http.post(url, json={"media_type": "image/png", "total_bytes": len(png),
                                        "media_category": "tweet_image"},
                             headers=await self._x_headers(http, "POST", url)) as r:
            d = await r.json(content_type=None)
            mid = (d.get("data") or {}).get("id")
            if r.status >= 300 or not mid:
                raise SocialError(f"image upload: {_x_error(d, r.status)}")
        url = f"{X_API}/2/media/upload/{mid}/append"
        form = aiohttp.FormData()
        form.add_field("segment_index", "0")
        form.add_field("media", png, filename="card.png", content_type="image/png")
        async with http.post(url, data=form, headers=await self._x_headers(http, "POST", url)) as r:
            if r.status >= 300:
                raise SocialError(f"image upload: {_x_error(await r.json(content_type=None), r.status)}")
        url = f"{X_API}/2/media/upload/{mid}/finalize"
        async with http.post(url, headers=await self._x_headers(http, "POST", url)) as r:
            if r.status >= 300:
                raise SocialError(f"image upload: {_x_error(await r.json(content_type=None), r.status)}")
        return str(mid)

    async def post_x(self, text: str, png: bytes | None = None) -> dict:
        import aiohttp

        text = (text or "").strip()
        if not text:
            raise SocialError("write something first")
        if x_length(text) > 280:
            raise SocialError(f"too long for X: {x_length(text)} of 280 characters")
        if len([r for r in self._today() if r["to"] == "x"]) >= MAX_X_PER_DAY:
            raise SocialError(f"daily X limit reached ({MAX_X_PER_DAY} posts): it protects your API credit")
        note = ""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
            body: dict = {"text": text}
            if png:
                try:
                    body["media"] = {"media_ids": [await self._x_upload(http, png)]}
                except SocialError as e:
                    note = f"posted without the card ({e})"
            url = f"{X_API}/2/tweets"
            async with http.post(url, json=body, headers=await self._x_headers(http, "POST", url)) as r:
                d = await r.json(content_type=None)
                pid = (d.get("data") or {}).get("id")
                if r.status >= 300 or not pid:
                    err = _x_error(d, r.status)
                    self._log({"to": "x", "ok": False, "error": err, "text": text[:280]})
                    raise SocialError(f"X refused the post: {err}")
        user = (self._auth().get("user") or {}).get("username") or self._env("X_USERNAME")
        link = f"https://x.com/{user}/status/{pid}" if user else f"https://x.com/i/status/{pid}"
        row = {"to": "x", "ok": True, "id": pid, "url": link, "cost_usd": x_cost(text), "text": text[:280],
               "image": bool(png) and not note}
        self._log(row)
        return {**row, "note": note}

    # ---------------------------------------------------------------- Telegram
    async def post_telegram(self, text: str, png: bytes | None = None) -> dict:
        import aiohttp

        token, chat = self._env("TELEGRAM_BOT_TOKEN"), self._env("TELEGRAM_CHANNEL_ID")
        if not (token and chat):
            raise SocialError("Telegram isn't set up: add the bot token and TELEGRAM_CHANNEL_ID in Controls → API keys")
        if sum(1 for r in self._today() if r["to"] == "telegram") >= MAX_TG_PER_DAY:
            raise SocialError(f"daily Telegram limit reached ({MAX_TG_PER_DAY})")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
            if png:
                form = aiohttp.FormData()
                form.add_field("chat_id", chat)
                form.add_field("caption", text[:1024])
                form.add_field("photo", png, filename="card.png", content_type="image/png")
                req = http.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=form)
            else:
                req = http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat, "text": text[:4096], "disable_web_page_preview": True})
            async with req as r:
                d = await r.json(content_type=None)
        if not d.get("ok"):
            err = str(d.get("description") or f"HTTP {r.status}")[:160]
            self._log({"to": "telegram", "ok": False, "error": err, "text": text[:280]})
            raise SocialError(f"Telegram refused it: {err}")
        res = d.get("result") or {}
        uname = (res.get("chat") or {}).get("username")
        link = f"https://t.me/{uname}/{res.get('message_id')}" if uname else ""
        row = {"to": "telegram", "ok": True, "id": res.get("message_id"), "url": link, "cost_usd": 0.0,
               "text": text[:280], "image": bool(png)}
        self._log(row)
        return row

    async def post(self, text: str, png: bytes | None = None, to: tuple[str, ...] = ("x",)) -> dict:
        """Post to each channel in `to`; one failing doesn't stop the other. {channel: result or {'error'}}."""
        out = {}
        for ch in to:
            try:
                out[ch] = await (self.post_x(text, png) if ch == "x" else self.post_telegram(text, png))
            except SocialError as e:
                out[ch] = {"ok": False, "error": str(e)}
            except Exception as e:                       # network: report, never crash the dashboard
                out[ch] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
        return out


def _x_error(d: dict, status: int) -> str:
    if isinstance(d, dict):
        if d.get("detail"):
            return f"{d.get('title', 'error')}: {d['detail']}"[:200]
        errs = d.get("errors") or []
        if errs:
            e = errs[0]
            return str(e.get("message") or e.get("detail") or e)[:200]
    hints = {401: "the keys or sign-in were rejected", 402: "your X API credit has run out (console.x.com)",
             403: "the app can't post: set its permissions to Read and write, then regenerate the access token",
             429: "X is rate-limiting this app"}
    return hints.get(status, f"HTTP {status}")
