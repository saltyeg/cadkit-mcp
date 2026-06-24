"""`cadkit-auth` — manage the Onshape OAuth2 session.

Usage:
    cadkit-auth login     # one-time browser handshake; stores tokens locally
    cadkit-auth status    # show whether a token exists and when it expires
    cadkit-auth logout    # delete the stored token

Requires ONSHAPE_OAUTH_CLIENT_ID / ONSHAPE_OAUTH_CLIENT_SECRET in the
environment (or a loaded .env). See README "OAuth2 (bring your own account)".
"""
import asyncio
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from .oauth import OnshapeOAuth


def _require_oauth() -> OnshapeOAuth:
    oauth = OnshapeOAuth.from_env()
    if oauth is None:
        sys.exit(
            "OAuth is not configured. Set ONSHAPE_OAUTH_CLIENT_ID and "
            "ONSHAPE_OAUTH_CLIENT_SECRET (register a 'Connected desktop app' in "
            "the Onshape dev portal with redirect URI "
            f"{os.getenv('ONSHAPE_OAUTH_REDIRECT', 'http://localhost:8910/callback')})."
        )
    return oauth


def _print_status(status: dict) -> None:
    if not status.get("authenticated"):
        print(f"Not authenticated. Token path: {status['token_path']}")
        return
    secs = status["access_token_expires_in"]
    state = "EXPIRED (will refresh on next call)" if status["expired"] else f"valid for {secs}s"
    print("Authenticated.")
    print(f"  token path : {status['token_path']}")
    print(f"  scope      : {status['scope']}")
    print(f"  access tok : {state}")
    print(f"  refresh tok: {'present' if status['has_refresh_token'] else 'MISSING'}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return

    oauth = _require_oauth()

    if cmd == "login":
        try:
            status = asyncio.run(oauth.login())
        except Exception as e:  # noqa: BLE001 — surface any flow error to the user
            sys.exit(f"Login failed: {e}")
        print("\nLogin successful.\n")
        _print_status(status)
    elif cmd == "status":
        _print_status(oauth.status())
    elif cmd == "logout":
        removed = oauth.logout()
        print("Logged out (token deleted)." if removed else "No stored token to remove.")
    else:
        sys.exit(f"Unknown command '{cmd}'. Try: login | status | logout")


if __name__ == "__main__":
    main()
