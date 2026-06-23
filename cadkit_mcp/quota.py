"""Count successful Onshape API calls so quota spend is visible, not a guess.

Only 2xx/3xx responses count against the annual budget (4xx/5xx are free). The client
raises on non-2xx (`raise_for_status`), so incrementing AFTER a call returns counts exactly
the quota-bearing requests — including feature POSTs that return 200 with featureStatus=ERROR
(those DO count). A per-session counter is logged; a cumulative total persists across sessions.

This module is the antidote to the failure mode that burned ~256 calls in one session:
flying blind with no running total. See the [[onshape-api-quota]] memory.
"""
import json
import pathlib

from loguru import logger

_STATE = pathlib.Path.home() / ".cadkit_api_calls.json"
_session = {"count": 0}


def _load() -> dict:
    try:
        return json.loads(_STATE.read_text())
    except Exception:
        return {"total": 0}


def record() -> None:
    _session["count"] += 1
    d = _load()
    d["total"] = d.get("total", 0) + 1
    try:
        _STATE.write_text(json.dumps(d))
    except Exception:
        pass
    n = _session["count"]
    if n % 10 == 0:                          # nudge every 10 so the spend stays felt
        logger.warning(f"[cadkit quota] {n} successful API calls this session "
                       f"(budget is 2,500/user/yr — that's {n / 25:.1f}% of the annual allowance)")


def counts() -> dict:
    return {"session": _session["count"], "trackedTotal": _load().get("total", 0)}


def instrument(client) -> None:
    """Wrap the client's get/post/delete so each successful call is recorded."""
    for method in ("get", "post", "delete"):
        orig = getattr(client, method)

        async def wrapped(*args, __orig=orig, **kwargs):
            result = await __orig(*args, **kwargs)
            record()                          # only reached on 2xx/3xx (non-2xx raised above)
            return result

        setattr(client, method, wrapped)
