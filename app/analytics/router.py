"""
Government Portal API: jurisdiction-scoped complaints, SLA escalation and
hierarchical performance analytics.

Every handler follows the same order (section 19):
  1. resolve the caller from the signed token   -> access.authority_from_header
  2. bring SLA escalations up to date           -> repo.tick()
  3. authorise role + jurisdiction + resource   -> access.require_*
  4. compute from the stored complaint records  -> engine.*
"""

from typing import List, Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

from . import access, engine
from .config import (CATEGORY_SLA_DAYS, ESCALATION_CHAIN, LEVEL_PEER_NOUN, LEVEL_UNIT_TYPE,
                     RESOLUTION_POLICY, SCORING)
from .engine import PolicyError
from .store import get_repo, now_ms

router = APIRouter()


def _fail(e: PolicyError):
    raise HTTPException(status_code=e.status, detail=e.message)


def _context(authorization):
    repo = get_repo()
    repo.tick()
    try:
        actor = access.authority_from_header(repo.ds, authorization)
    except PolicyError as e:
        _fail(e)
    return repo, actor


def _period(period, frm, to):
    try:
        return engine.period_range(period or "30d", now_ms(), frm, to)
    except (PolicyError, ValueError) as e:
        raise HTTPException(422, getattr(e, "message", "Dates must be YYYY-MM-DD."))


# ---------------------------------------------------------------------------
# Health, directory and login
# ---------------------------------------------------------------------------
@router.get("/api/gov/health")
def health():
    repo = get_repo()
    repo.tick()
    return {"ok": True, "mode": "live", "now": now_ms(), "complaints": len(repo.ds.complaints),
            "data_version": repo.version, "demo_login": access.DEMO_LOGIN_ENABLED}


@router.get("/api/gov/directory")
def directory():
    """Official accounts for the demo login picker - titles and jurisdictions only."""
    ds = get_repo().ds
    out = []
    for a in ds.authorities.values():
        out.append({"id": a["id"], "title": a["title"], "role": a["role"], "role_label": a["role_label"],
                    "level": a["level"], "unit_id": a["unit"], "path": [p["name"] for p in ds.path(a["unit"])]})
    order = {"COUNTRY": 0, "STATE": 1, "DISTRICT": 2, "SDM": 3, "REGION": 4, "WARD": 5}
    out.sort(key=lambda a: (order[a["level"]], a["path"]))
    return {"authorities": out}


class DemoLogin(BaseModel):
    authority_id: str


@router.post("/api/gov/auth/demo-login")
def demo_login(body: DemoLogin):
    if not access.DEMO_LOGIN_ENABLED:
        raise HTTPException(403, "Demo login is disabled on this server.")
    ds = get_repo().ds
    auth = ds.authorities.get(body.authority_id)
    if not auth:
        raise HTTPException(404, "Unknown official account.")
    token, exp = access.issue_token(auth["id"], "gov")
    return {"token": token, "expires_at": exp * 1000, "authority": auth}


@router.get("/api/gov/me")
def me(authorization: Optional[str] = Header(None)):
    repo, actor = _context(authorization)
    ds = repo.ds
    group = ds.peer_group(actor["unit"])
    peer = None
    if group:
        plural = LEVEL_PEER_NOUN[group[0]][1]
        peer = {"level": group[0], "within": group[1], "within_name": ds.units[group[1]]["name"], "noun": plural}
    ranking_levels = []
    for level in ("STATE", "DISTRICT", "SDM", "WARD"):
        utype = LEVEL_UNIT_TYPE[level]
        inside = any(ds.units[u]["type"] == utype and u != actor["unit"] for u in ds.subtree(actor["unit"]))
        if inside or (group and group[0] == level):
            ranking_levels.append(level)
    return {"authority": actor, "unit": engine.unit_brief(ds, actor["unit"]), "path": ds.path(actor["unit"]),
            "peer_group": peer, "ranking_levels": ranking_levels, "sections": access.SECTIONS,
            "ward_count": len(ds.wards_under(actor["unit"]))}


@router.get("/api/gov/hierarchy")
def hierarchy(unit: Optional[str] = None, authorization: Optional[str] = Header(None)):
    repo, actor = _context(authorization)
    ds = repo.ds
    unit = unit or actor["unit"]
    try:
        access.require_unit(ds, actor, unit)
    except PolicyError as e:
        _fail(e)
    kids = []
    for uid in engine.child_units(ds, unit):
        brief = engine.unit_brief(ds, uid)
        brief["child_count"] = len(engine.child_units(ds, uid))
        kids.append(brief)
    return {"unit": engine.unit_brief(ds, unit), "path": ds.path(unit), "children": kids}


# ---------------------------------------------------------------------------
# Complaints (section 2: jurisdiction enforced here, not in the UI)
# ---------------------------------------------------------------------------
@router.get("/api/gov/complaints")
def complaints(sort: str = "priority", filter: str = "all", ward: Optional[str] = None,
               limit: int = 40, offset: int = 0, authorization: Optional[str] = Header(None)):
    repo, actor = _context(authorization)
    ds = repo.ds
    wards = access.scope_wards(ds, actor)
    if ward and ward not in wards:
        raise HTTPException(403, f"{ds.units[ward]['name'] if ward in ds.units else ward} is outside your jurisdiction.")
    limit = max(1, min(limit, 200))
    with repo.lock:
        return engine.list_complaints(ds, wards, now_ms(), sort, filter, ward, limit, max(0, offset))


@router.get("/api/gov/complaints/{complaint_id}")
def complaint(complaint_id: str, authorization: Optional[str] = Header(None)):
    repo, actor = _context(authorization)
    ds = repo.ds
    c = ds.complaints.get(complaint_id.lstrip("#"))
    try:
        access.require_complaint(ds, actor, c)
    except PolicyError as e:
        _fail(e)
    with repo.lock:
        out = engine.complaint_detail(ds, c, now_ms())
    ok, reason = access.update_permission(ds, actor, c)
    out["permissions"] = {"can_update": ok, "reason": reason}
    return out


class StatusChange(BaseModel):
    status: str
    note: Optional[str] = None
    evidence: bool = False


@router.patch("/api/gov/complaints/{complaint_id}/status")
def change_status(complaint_id: str, body: StatusChange, authorization: Optional[str] = Header(None)):
    repo, actor = _context(authorization)
    ds = repo.ds
    with repo.lock:
        c = ds.complaints.get(complaint_id.lstrip("#"))
        try:
            access.require_update(ds, actor, c)
            record = engine.apply_status(ds, actor, c, body.status.upper(), body.note, body.evidence, now_ms())
        except PolicyError as e:
            _fail(e)
        repo.save([c], [record] if record else [])
        out = engine.complaint_detail(ds, c, now_ms())
    ok, reason = access.update_permission(ds, actor, c)
    out["permissions"] = {"can_update": ok, "reason": reason}
    out["counts_towards_score"] = engine.is_valid_resolution(c) if c["status"] == "RESOLVED" else None
    return out


# ---------------------------------------------------------------------------
# Analytics (sections 3-28)
# ---------------------------------------------------------------------------
@router.get("/api/gov/analytics/config")
def analytics_config():
    return {"scoring": SCORING, "resolution_policy": RESOLUTION_POLICY, "category_sla_days": CATEGORY_SLA_DAYS,
            "escalation_chain": ESCALATION_CHAIN}


def _unit_view(fn, unit, period, frm, to, authorization):
    repo, actor = _context(authorization)
    ds = repo.ds
    unit = unit or actor["unit"]
    try:
        access.require_unit(ds, actor, unit)
    except PolicyError as e:
        _fail(e)
    p = _period(period, frm, to)
    with repo.lock:
        out = fn(ds, unit, p, now_ms())
    return ds, actor, out


@router.get("/api/gov/analytics/dashboard")
def analytics_dashboard(unit: Optional[str] = None, period: str = "30d", frm: Optional[str] = None,
                        to: Optional[str] = None, authorization: Optional[str] = Header(None)):
    ds, actor, out = _unit_view(engine.dashboard, unit, period, frm, to, authorization)
    out["viewer"] = {"authority_id": actor["id"], "unit": actor["unit"], "is_self": out["unit"]["id"] == actor["unit"]}
    if out["children"]:
        for item in out["children"]["items"]:
            item["accessible"] = access.can_view_unit(ds, actor, item["id"])
    return out


@router.get("/api/gov/analytics/rankings")
def analytics_rankings(level: str, within: Optional[str] = None, period: str = "30d", frm: Optional[str] = None,
                       to: Optional[str] = None, authorization: Optional[str] = Header(None)):
    repo, actor = _context(authorization)
    ds = repo.ds
    level = level.upper()
    if level not in LEVEL_PEER_NOUN:
        raise HTTPException(422, "level must be one of WARD, SDM, DISTRICT, STATE.")
    within = within or actor["unit"]
    try:
        access.require_ranking(ds, actor, level, within)
    except PolicyError as e:
        _fail(e)
    p = _period(period, frm, to)
    with repo.lock:
        out = engine.rankings_view(ds, level, within, p, now_ms(), highlight=actor["unit"])
    for item in out["items"]:
        item["accessible"] = access.can_view_unit(ds, actor, item["id"])
    return out


@router.get("/api/gov/analytics/sla")
def analytics_sla(unit: Optional[str] = None, period: str = "30d", frm: Optional[str] = None,
                  to: Optional[str] = None, authorization: Optional[str] = Header(None)):
    return _unit_view(engine.sla_view, unit, period, frm, to, authorization)[2]


@router.get("/api/gov/analytics/escalations")
def analytics_escalations(unit: Optional[str] = None, period: str = "30d", frm: Optional[str] = None,
                          to: Optional[str] = None, authorization: Optional[str] = Header(None)):
    return _unit_view(engine.escalation_view, unit, period, frm, to, authorization)[2]


@router.get("/api/gov/analytics/geo")
def analytics_geo(unit: Optional[str] = None, period: str = "30d", frm: Optional[str] = None,
                  to: Optional[str] = None, authorization: Optional[str] = Header(None)):
    return _unit_view(engine.geo_view, unit, period, frm, to, authorization)[2]


@router.post("/api/gov/demo/reset")
def demo_reset(authorization: Optional[str] = Header(None)):
    repo, actor = _context(authorization)
    if not access.DEMO_LOGIN_ENABLED:
        raise HTTPException(403, "Demo reset is disabled on this server.")
    repo.reset()
    return {"ok": True, "complaints": len(repo.ds.complaints)}


# ---------------------------------------------------------------------------
# Citizen side: jurisdiction routing on submit, confirmation of fixes
# ---------------------------------------------------------------------------
@router.post("/api/v2/citizen/demo-login")
def citizen_demo_login():
    if not access.DEMO_LOGIN_ENABLED:
        raise HTTPException(403, "Demo login is disabled on this server.")
    token, exp = access.issue_token("citizen-demo", "citizen")
    return {"token": token, "expires_at": exp * 1000, "citizen": "citizen-demo"}


def _nearest_ward(ds, lat, lng, max_deg=0.3):
    best, best_d = None, None
    for uid in sorted(ds.units):
        u = ds.units[uid]
        if u["type"] != "ward" or not u.get("center"):
            continue
        d = (u["center"][0] - lat) ** 2 + (u["center"][1] - lng) ** 2
        if best_d is None or d < best_d:
            best, best_d = uid, d
    return best if best_d is not None and best_d <= max_deg ** 2 else None


@router.post("/api/v2/complaints")
async def submit_complaint(
    category: str = Form(...),
    location: str = Form(...),
    description: str = Form(...),
    target_language: str = Form("en"),
    category_key: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    severity: int = Form(3),
    title: Optional[str] = Form(None),
    photos: List[UploadFile] = File(default=[]),
    authorization: Optional[str] = Header(None),
):
    """Existing intake (translation + photos, app/main.py) + server-side ward routing and SLA start."""
    from ..database import SessionLocal
    from ..main import create_complaint

    repo = get_repo()
    ds = repo.ds
    if category_key not in CATEGORY_SLA_DAYS:
        raise HTTPException(422, "Unknown category.")
    ward_id = engine.detect_ward(ds, latitude, longitude)
    if ward_id is None and category_key == "crime":
        ward_id = _nearest_ward(ds, latitude, longitude)  # crime reports are not geo-locked
    if ward_id is None:
        raise HTTPException(422, "This location is outside the wards onboarded on CivicConnect.")
    reporter = "anonymous"
    if authorization:
        try:
            reporter = access.citizen_from_header(authorization)
        except PolicyError as e:
            _fail(e)

    db = SessionLocal()
    try:
        intake = await create_complaint(category=category, location=location, description=description,
                                        target_language=target_language, photos=photos, db=db)
    finally:
        db.close()
    cid = intake.id.lstrip("#")
    now = now_ms()
    with repo.lock:
        c = engine.new_complaint(ds, cid, category_key, title or f"{category} Report",
                                 intake.translated_text or description, ward_id,
                                 max(1, min(5, severity)), reporter, now, intake_id=cid)
        ds.add_complaint(c)
        repo.save([c])
        summary = engine.complaint_summary(ds, c, now)
    return {"intake": intake, "lifecycle": summary}


class Confirmation(BaseModel):
    decision: str


@router.post("/api/v2/complaints/{complaint_id}/confirmation")
def confirm_resolution(complaint_id: str, body: Confirmation, authorization: Optional[str] = Header(None)):
    repo = get_repo()
    repo.tick()
    try:
        citizen = access.citizen_from_header(authorization)
    except PolicyError as e:
        _fail(e)
    ds = repo.ds
    with repo.lock:
        c = ds.complaints.get(complaint_id.lstrip("#"))
        if c is None:
            raise HTTPException(404, "No such complaint.")
        try:
            engine.apply_confirmation(ds, citizen, c, body.decision.upper(), now_ms())
        except PolicyError as e:
            _fail(e)
        repo.save([c])
        return engine.complaint_summary(ds, c, now_ms())


@router.get("/api/v2/complaints/{complaint_id}")
def citizen_complaint(complaint_id: str, authorization: Optional[str] = Header(None)):
    """A citizen's own complaint: status, SLA and timeline (never other citizens' complaints)."""
    repo = get_repo()
    repo.tick()
    try:
        citizen = access.citizen_from_header(authorization)
    except PolicyError as e:
        _fail(e)
    ds = repo.ds
    c = ds.complaints.get(complaint_id.lstrip("#"))
    if c is None or c["reporter"] != citizen:
        raise HTTPException(404, "No such complaint in your account.")
    with repo.lock:
        out = engine.complaint_summary(ds, c, now_ms())
        out["timeline"] = engine.build_timeline(ds, c)
    return out
