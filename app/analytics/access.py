"""
Authentication and jurisdiction policy for the Government Portal APIs.

Every endpoint resolves the caller from a signed bearer token and then asks
this module whether that authority may see or change the requested resource.
The caller's role and jurisdiction are always looked up server-side from the
authority record the token names - never taken from query parameters, request
bodies or anything else the client controls (section 19 / 33).
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from .config import LEVEL_RANK
from .engine import PolicyError

# A fresh secret per process unless one is configured, so tokens die with the
# server. Set GOV_TOKEN_SECRET in production to keep sessions across restarts.
_SECRET = (os.environ.get("GOV_TOKEN_SECRET") or secrets.token_hex(32)).encode()
TOKEN_TTL_SECONDS = 8 * 3600

# Demo login lets anyone pick an official account, exactly like the existing
# prototype's "Demo Role (development only)" selector. Production replaces
# /auth/demo-login with government-issued credentials (e.g. Supabase Auth JWTs
# mapped to the `roles` table); everything after login stays the same.
DEMO_LOGIN_ENABLED = os.environ.get("GOV_DEMO_LOGIN", "1") == "1"


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(subject, kind, now_s=None):
    now_s = int(now_s or time.time())
    payload = {"sub": subject, "kind": kind, "iat": now_s, "exp": now_s + TOKEN_TTL_SECONDS}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}", payload["exp"]


def verify_token(token, kind):
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        payload = json.loads(_unb64(body))
    except Exception:
        raise PolicyError(401, "Invalid or expired session. Please log in again.")
    if payload.get("kind") != kind or payload.get("exp", 0) < time.time():
        raise PolicyError(401, "Invalid or expired session. Please log in again.")
    return payload["sub"]


def bearer(header):
    if not header or not header.lower().startswith("bearer "):
        raise PolicyError(401, "Government login required.")
    return header.split(" ", 1)[1].strip()


def authority_from_header(ds, header):
    authority_id = verify_token(bearer(header), "gov")
    auth = ds.authorities.get(authority_id)
    if not auth:
        raise PolicyError(401, "This official account no longer exists.")
    return auth


def citizen_from_header(header):
    return verify_token(bearer(header), "citizen")


# ---------------------------------------------------------------------------
# Jurisdiction policy
# ---------------------------------------------------------------------------
def scope_units(ds, actor):
    return set(ds.subtree(actor["unit"]))


def scope_wards(ds, actor):
    return set(ds.wards_under(actor["unit"]))


def require_unit(ds, actor, unit_id):
    """The caller may open analytics for its own unit and anything beneath it."""
    if unit_id not in ds.units:
        raise PolicyError(404, "Unknown jurisdiction.")
    if unit_id not in scope_units(ds, actor):
        raise PolicyError(403, f"{ds.units[unit_id]['name']} is outside your jurisdiction ({ds.units[actor['unit']]['name']}).")


def require_ranking(ds, actor, level, within_id):
    """Rankings inside your jurisdiction, or your own peer group (aggregates only)."""
    if within_id not in ds.units:
        raise PolicyError(404, "Unknown jurisdiction.")
    if within_id in scope_units(ds, actor):
        return
    group = ds.peer_group(actor["unit"])
    if group and group == (level, within_id):
        return
    raise PolicyError(403, f"You can compare authorities inside {ds.units[actor['unit']]['name']} or your own peer group only.")


def can_view_unit(ds, actor, unit_id):
    return unit_id in scope_units(ds, actor)


def require_complaint(ds, actor, complaint):
    if complaint is None:
        raise PolicyError(404, "No such complaint.")
    if complaint["ward_id"] not in scope_wards(ds, actor):
        raise PolicyError(403, f"Complaint #{complaint['id']} belongs to {ds.units[complaint['ward_id']]['name']}, "
                               f"outside your jurisdiction ({ds.units[actor['unit']]['name']}).")


def update_permission(ds, actor, complaint):
    """(allowed, reason) for status changes. Viewing is checked separately."""
    if complaint["status"] == "RESOLVED":
        return False, "Resolved - awaiting citizen confirmation. Only a citizen dispute can reopen it."
    if LEVEL_RANK[actor["level"]] < LEVEL_RANK[complaint["level"]]:
        holder = ds.authorities[complaint["assignments"][-1]["authority_id"]]["title"]
        return False, f"Escalated beyond your level - now handled by {holder}."
    return True, None


def require_update(ds, actor, complaint):
    require_complaint(ds, actor, complaint)
    ok, reason = update_permission(ds, actor, complaint)
    if not ok:
        raise PolicyError(403 if complaint["status"] != "RESOLVED" else 409, reason)


# Analytics sections (section 30). Every role gets all five; what each one
# contains is scoped by the jurisdiction checks above (a Ward Councillor's
# "Rankings" is their peer group, their "Geographic" view is their own ward).
SECTIONS = ["my_performance", "rankings", "sla", "escalations", "geographic"]
