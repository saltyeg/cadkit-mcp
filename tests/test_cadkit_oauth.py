"""Offline tests for cadkit OAuth2 auth-code flow and the bearer-capable client.

No network: the token endpoint and the loopback capture are monkeypatched, and
tokens are written to a tmp_path. Zero Onshape API calls.
"""
import time
from unittest.mock import AsyncMock, Mock

import pytest

from cadkit_mcp import oauth as oauth_mod
from cadkit_mcp.oauth import OnshapeOAuth
from onshape_mcp.api.client import OnshapeClient, OnshapeCredentials


def _make(tmp_path, **over):
    kw = dict(
        client_id="cid",
        client_secret="secret",
        token_path=tmp_path / "tok.json",
    )
    kw.update(over)
    return OnshapeOAuth(**kw)


# ── construction / env ──────────────────────────────────────────────────────

def test_requires_client_id_and_secret(tmp_path):
    with pytest.raises(ValueError):
        OnshapeOAuth(client_id="", client_secret="x", token_path=tmp_path / "t")


def test_from_env_none_when_unset(monkeypatch):
    monkeypatch.delenv("ONSHAPE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("ONSHAPE_OAUTH_CLIENT_SECRET", raising=False)
    assert OnshapeOAuth.from_env() is None


def test_from_env_builds_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("ONSHAPE_OAUTH_CLIENT_ID", "abc")
    monkeypatch.setenv("ONSHAPE_OAUTH_CLIENT_SECRET", "def")
    monkeypatch.setenv("ONSHAPE_OAUTH_TOKEN_PATH", str(tmp_path / "t.json"))
    o = OnshapeOAuth.from_env()
    assert o is not None
    assert o.client_id == "abc" and o.client_secret == "def"


# ── token file / status ─────────────────────────────────────────────────────

def test_status_unauthenticated(tmp_path):
    o = _make(tmp_path)
    assert o.has_token() is False
    assert o.status()["authenticated"] is False


def test_store_token_response_writes_and_computes_expiry(tmp_path):
    o = _make(tmp_path)
    o._store_token_response(
        {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "scope": "S"}
    )
    assert o.has_token()
    st = o.status()
    assert st["authenticated"] and st["has_refresh_token"]
    assert 3500 < st["access_token_expires_in"] <= 3600
    # persisted and re-readable
    assert _make(tmp_path).has_token()


def test_store_token_keeps_old_refresh_when_omitted(tmp_path):
    o = _make(tmp_path)
    o._store_token_response({"access_token": "AT1", "refresh_token": "RT1", "expires_in": 10})
    o._store_token_response({"access_token": "AT2", "expires_in": 10})  # no refresh_token
    assert o._token["refresh_token"] == "RT1"


def test_logout_deletes(tmp_path):
    o = _make(tmp_path)
    o._store_token_response({"access_token": "AT", "refresh_token": "RT", "expires_in": 10})
    assert o.logout() is True
    assert o.has_token() is False
    assert o.logout() is False  # already gone


# ── authorize URL ───────────────────────────────────────────────────────────

def test_authorize_url_has_params(tmp_path):
    o = _make(tmp_path)
    url = o.authorize_url("STATE123")
    assert url.startswith(oauth_mod.AUTHORIZE_URL)
    for frag in ("response_type=code", "client_id=cid", "state=STATE123", "scope=OAuth2"):
        assert frag in url


# ── authorization_header: valid / refresh / missing ─────────────────────────

@pytest.mark.asyncio
async def test_header_returns_bearer_when_valid(tmp_path):
    o = _make(tmp_path)
    o._store_token_response({"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
    assert await o.authorization_header() == "Bearer AT"


@pytest.mark.asyncio
async def test_header_refreshes_when_expired(tmp_path, monkeypatch):
    o = _make(tmp_path)
    o._store_token_response({"access_token": "OLD", "refresh_token": "RT", "expires_in": 1})
    o._token["expires_at"] = time.time() - 5  # force expiry

    async def fake_token_request(form):
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "RT"
        return {"access_token": "NEW", "refresh_token": "RT2", "expires_in": 3600}

    monkeypatch.setattr(o, "_token_request", fake_token_request)
    assert await o.authorization_header() == "Bearer NEW"
    assert o._token["refresh_token"] == "RT2"  # rotation persisted


@pytest.mark.asyncio
async def test_header_raises_when_no_token(tmp_path):
    o = _make(tmp_path)
    with pytest.raises(RuntimeError):
        await o.authorization_header()


# ── login flow (loopback + token exchange monkeypatched) ────────────────────

@pytest.mark.asyncio
async def test_login_happy_path(tmp_path, monkeypatch):
    o = _make(tmp_path)
    captured_state = {}

    def fake_capture(host, port, path):
        # echo back the state the flow generated, with a code
        return {"code": "CODE", "state": captured_state["state"], "error": None}

    # intercept authorize_url to grab the state the flow created
    real_authorize = o.authorize_url

    def spy_authorize(state):
        captured_state["state"] = state
        return real_authorize(state)

    async def fake_token_request(form):
        assert form["grant_type"] == "authorization_code"
        assert form["code"] == "CODE"
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

    monkeypatch.setattr(o, "authorize_url", spy_authorize)
    monkeypatch.setattr(oauth_mod, "_capture_one_request", fake_capture)
    monkeypatch.setattr(o, "_token_request", fake_token_request)
    monkeypatch.setattr(oauth_mod.webbrowser, "open", lambda *a, **k: True)

    status = await o.login(open_browser=False)
    assert status["authenticated"]
    assert o._token["access_token"] == "AT"


@pytest.mark.asyncio
async def test_login_state_mismatch_raises(tmp_path, monkeypatch):
    o = _make(tmp_path)

    def fake_capture(host, port, path):
        return {"code": "CODE", "state": "WRONG", "error": None}

    monkeypatch.setattr(oauth_mod, "_capture_one_request", fake_capture)
    monkeypatch.setattr(oauth_mod.webbrowser, "open", lambda *a, **k: True)
    with pytest.raises(RuntimeError, match="State mismatch"):
        await o.login(open_browser=False)


@pytest.mark.asyncio
async def test_login_error_param_raises(tmp_path, monkeypatch):
    o = _make(tmp_path)
    monkeypatch.setattr(
        oauth_mod, "_capture_one_request",
        lambda h, p, path: {"code": None, "state": None, "error": "access_denied"},
    )
    monkeypatch.setattr(oauth_mod.webbrowser, "open", lambda *a, **k: True)
    with pytest.raises(RuntimeError, match="access_denied"):
        await o.login(open_browser=False)


# ── OnshapeClient bearer support ────────────────────────────────────────────

def test_client_requires_some_auth():
    with pytest.raises(ValueError):
        OnshapeClient()


@pytest.mark.asyncio
async def test_client_uses_bearer_provider():
    async def provider():
        return "Bearer XYZ"

    client = OnshapeClient(auth_provider=provider, base_url="https://cad.onshape.com")

    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True}
    mock_resp.content = b'{"ok": true}'
    mock_resp.raise_for_status = Mock()
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    client._client = mock_client

    await client.get("/api/test")
    headers = mock_client.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer XYZ"


@pytest.mark.asyncio
async def test_client_credentials_still_basic():
    creds = OnshapeCredentials(access_key="k", secret_key="s")
    client = OnshapeClient(creds)
    assert (await client._auth_provider()).startswith("Basic ")
