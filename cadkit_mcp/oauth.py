"""OAuth2 authorization-code flow for Onshape (localhost loopback).

Lets cadkit authenticate as the *user* via their own Onshape account instead of
API keys. This is the piece that makes the "bring-your-own-agent" / public App
Store model possible: each end user authorizes through their own seat, and once
the app is published those calls are exempt from the per-user annual quota.

Caveat worth remembering: a *private* (unpublished) OAuth app's calls still
count against the cap exactly like API keys — the exemption is a property of the
published app, not of OAuth itself. So this buys the right architecture, not
quota relief, until launch.

Design:
  - Authorization-code grant with a fixed `http://localhost:<port>/callback`
    redirect (must match what's registered in the Onshape dev portal).
  - `login()` opens the browser, runs a one-shot loopback server to catch the
    code, exchanges it for tokens, and stores them at ~/.cadkit/onshape_token.json
    (chmod 600).
  - `authorization_header()` is the async provider handed to OnshapeClient: it
    refreshes silently when the access token is near expiry and returns a
    "Bearer <token>" header. Onshape rotates refresh tokens, so each refresh
    re-saves the file.

No live API calls happen here except the token endpoint itself (which does not
count against the modeling quota).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

# Onshape OAuth endpoints (stable; documented at onshape-public.github.io/docs/auth/oauth).
AUTHORIZE_URL = "https://oauth.onshape.com/oauth/authorize"
TOKEN_URL = "https://oauth.onshape.com/oauth/token"
DEFAULT_API_BASE = "https://cad.onshape.com"
DEFAULT_REDIRECT = "http://localhost:8910/callback"
# Read + write + delete: cadkit creates features and can delete them. Trim if you
# don't want delete. PII scope is deliberately omitted.
DEFAULT_SCOPE = "OAuth2Read OAuth2Write OAuth2Delete"

# Refresh this many seconds before the access token actually expires, so an
# in-flight request never races the boundary.
_EXPIRY_SKEW = 90


def _default_token_path() -> pathlib.Path:
    override = os.getenv("ONSHAPE_OAUTH_TOKEN_PATH")
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path("~/.cadkit/onshape_token.json").expanduser()


class OnshapeOAuth:
    """Holds OAuth client config + cached tokens, and refreshes on demand."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str = DEFAULT_REDIRECT,
        scope: str = DEFAULT_SCOPE,
        base_url: str = DEFAULT_API_BASE,
        token_path: Optional[pathlib.Path] = None,
    ):
        if not client_id or not client_secret:
            raise ValueError("OnshapeOAuth requires client_id and client_secret")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scope = scope
        self.base_url = base_url
        self.token_path = token_path or _default_token_path()
        self._token: Dict[str, Any] = self._read_token_file()

    # -- construction -------------------------------------------------------

    @classmethod
    def from_env(cls) -> Optional["OnshapeOAuth"]:
        """Build from env vars, or return None if OAuth isn't configured.

        Reads ONSHAPE_OAUTH_CLIENT_ID / ONSHAPE_OAUTH_CLIENT_SECRET and the
        optional ONSHAPE_OAUTH_REDIRECT / ONSHAPE_OAUTH_SCOPE / ONSHAPE_BASE_URL.
        Returns None (not an error) when the client id/secret are absent, so the
        server can cleanly fall back to API-key auth.
        """
        cid = os.getenv("ONSHAPE_OAUTH_CLIENT_ID", "").strip()
        sec = os.getenv("ONSHAPE_OAUTH_CLIENT_SECRET", "").strip()
        if not (cid and sec):
            return None
        return cls(
            client_id=cid,
            client_secret=sec,
            redirect_uri=os.getenv("ONSHAPE_OAUTH_REDIRECT", DEFAULT_REDIRECT).strip(),
            scope=os.getenv("ONSHAPE_OAUTH_SCOPE", DEFAULT_SCOPE).strip(),
            base_url=os.getenv("ONSHAPE_BASE_URL", DEFAULT_API_BASE).strip(),
        )

    # -- token file ---------------------------------------------------------

    def _read_token_file(self) -> Dict[str, Any]:
        try:
            return json.loads(self.token_path.read_text())
        except Exception:
            return {}

    def _write_token_file(self, token: Dict[str, Any]) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(token, indent=2))
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:
            pass  # best effort (e.g. Windows)
        self._token = token

    def has_token(self) -> bool:
        return bool(self._token.get("access_token"))

    def logout(self) -> bool:
        """Delete the stored token. Returns True if a file was removed."""
        self._token = {}
        try:
            self.token_path.unlink()
            return True
        except FileNotFoundError:
            return False

    def status(self) -> Dict[str, Any]:
        t = self._token
        if not t.get("access_token"):
            return {"authenticated": False, "token_path": str(self.token_path)}
        remaining = int(t.get("expires_at", 0) - time.time())
        return {
            "authenticated": True,
            "token_path": str(self.token_path),
            "scope": t.get("scope", self.scope),
            "access_token_expires_in": remaining,
            "expired": remaining <= 0,
            "has_refresh_token": bool(t.get("refresh_token")),
        }

    # -- token endpoint -----------------------------------------------------

    async def _token_request(self, form: Dict[str, str]) -> Dict[str, Any]:
        """POST to the token endpoint with HTTP Basic client authentication."""
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            resp = await c.post(TOKEN_URL, data=form, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def _store_token_response(self, data: Dict[str, Any]) -> Dict[str, Any]:
        expires_in = int(data.get("expires_in", 0))
        token = {
            "access_token": data["access_token"],
            "token_type": data.get("token_type", "Bearer"),
            "expires_at": time.time() + expires_in,
            "scope": data.get("scope", self.scope),
        }
        # Onshape rotates refresh tokens — keep the new one, or reuse the old if
        # the response omitted it.
        token["refresh_token"] = data.get("refresh_token") or self._token.get(
            "refresh_token", ""
        )
        self._write_token_file(token)
        return token

    async def _refresh(self) -> None:
        rt = self._token.get("refresh_token")
        if not rt:
            raise RuntimeError(
                "No refresh token available — run `cadkit-auth login` first."
            )
        data = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": rt}
        )
        self._store_token_response(data)

    # -- the provider handed to OnshapeClient -------------------------------

    async def authorization_header(self) -> str:
        """Return a valid 'Bearer <token>' header, refreshing if near expiry."""
        if not self._token.get("access_token"):
            raise RuntimeError(
                "Not authenticated — run `cadkit-auth login` (OAuth) or set "
                "ONSHAPE_ACCESS_KEY/SECRET (API key)."
            )
        if time.time() >= self._token.get("expires_at", 0) - _EXPIRY_SKEW:
            await self._refresh()
        return f"Bearer {self._token['access_token']}"

    # -- interactive login --------------------------------------------------

    def authorize_url(self, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def login(self, open_browser: bool = True) -> Dict[str, Any]:
        """Run the full authorization-code flow via a one-shot loopback server.

        Returns the stored token status. Raises on mismatch/error.
        """
        parsed = urlparse(self.redirect_uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 80
        path = parsed.path or "/callback"
        state = secrets.token_urlsafe(24)

        url = self.authorize_url(state)
        print(f"Opening browser to authorize cadkit with Onshape...\n  {url}\n")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        print(
            f"If the browser didn't open, paste the URL above.\n"
            f"Waiting for the redirect on http://{host}:{port}{path} ...\n"
        )

        captured = await asyncio.to_thread(_capture_one_request, host, port, path)

        if captured.get("error"):
            raise RuntimeError(f"Authorization failed: {captured['error']}")
        if not captured.get("code"):
            raise RuntimeError("No authorization code received.")
        if captured.get("state") != state:
            raise RuntimeError("State mismatch — possible CSRF; aborting.")

        data = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": captured["code"],
                "redirect_uri": self.redirect_uri,
            }
        )
        self._store_token_response(data)
        return self.status()


def _capture_one_request(host: str, port: int, path: str) -> Dict[str, Optional[str]]:
    """Serve exactly one HTTP request and return the parsed query params."""
    captured: Dict[str, Optional[str]] = {"code": None, "state": None, "error": None}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            parsed = urlparse(self.path)
            if parsed.path != path:
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(parsed.query)
            captured["code"] = (qs.get("code") or [None])[0]
            captured["state"] = (qs.get("state") or [None])[0]
            captured["error"] = (qs.get("error") or [None])[0]
            body = (
                b"<html><body style='font-family:sans-serif'>"
                b"<h2>cadkit \xe2\x80\x94 authorization received.</h2>"
                b"<p>You can close this tab and return to the terminal.</p>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence stdlib request logging
            pass

    with HTTPServer((host, port), _Handler) as httpd:
        httpd.handle_request()
    return captured
