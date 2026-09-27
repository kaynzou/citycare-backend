"""
Analytics engine: jurisdiction tree, SLA escalation, metrics, scoring, ranking.

Pure functions over plain dicts - no database, no HTTP. The API layer
(router.py) authorises the caller first and only then calls in here, and
store.py persists whatever this module mutates.

Times are epoch milliseconds (ints). The offline demo engine in the
prototype (prototype-v2/analytics/engine.js) is a line-for-line port of this
file; tests/test_parity.py runs both on the same data and compares results.
"""

import calendar
import datetime
import math

from .config import (
    AT_RISK_HOURS,
    CATEGORY_SLA_DAYS,
    DEFAULT_SLA_DAYS,
    ESCALATION_CHAIN,
    LEVEL_PEER_NOUN,
    LEVEL_RANK,
    LEVEL_ROLE,
    LEVEL_UNIT_TYPE,
    RESOLUTION_POLICY,
    ROLE_LABEL,
    SCORING,
    TZ_OFFSET_MINUTES,
    UNIT_TYPE_LEVEL,
)

MIN_MS = 60 * 1000
HOUR_MS = 60 * MIN_MS
DAY_MS = 24 * HOUR_MS
TZ_MS = TZ_OFFSET_MINUTES * MIN_MS
FAR_FUTURE = 10 ** 15
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
CATEGORY_ORDER = ["roads", "garbage", "streetlights", "water", "drainage", "electricity", "sanitation",
                  "infrastructure", "publicsafety", "traffic", "community", "environment", "crime", "other"]
LEVEL_LABEL = {"WARD": "Ward", "REGION": "Local region", "SDM": "SDM", "DISTRICT": "District",
               "STATE": "State", "COUNTRY": "National"}
OPEN = ("PENDING", "VERIFIED", "IN_PROGRESS", "ESCALATED")


class PolicyError(Exception):
    """Raised when an action is not allowed; carries an HTTP status for the API layer."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def r1(x):
    """Round to one decimal, half up - identical to JS Math.round(x * 10) / 10."""
    return math.floor(x * 10 + 0.5) / 10


def pct(n, d):
    return r1(100.0 * n / d) if d else None


def sla_ms(category):
    return CATEGORY_SLA_DAYS.get(category, DEFAULT_SLA_DAYS) * DAY_MS


# ---------------------------------------------------------------------------
# Dataset: jurisdiction tree + authorities + complaints, with indexes
# ---------------------------------------------------------------------------
class Dataset:
    def __init__(self, units, authorities, complaints, escalations=None, descriptions=None):
        self.units = {u["id"]: u for u in units}
        self.children = {u["id"]: [] for u in units}
        for u in units:
            if u.get("parent") and u["type"] != "region":
                self.children[u["parent"]].append(u["id"])
        for k in self.children:
            self.children[k].sort()
        self.authorities = {}
        self.authority_by_unit = {}
        for a in authorities:
            level = UNIT_TYPE_LEVEL[self.units[a["unit"]]["type"]]
            role = LEVEL_ROLE[level]
            full = {"id": a["id"], "unit": a["unit"], "title": a["title"], "level": level,
                    "role": role, "role_label": ROLE_LABEL[role]}
            self.authorities[a["id"]] = full
            self.authority_by_unit[a["unit"]] = full
        self.complaints = {c["id"]: c for c in complaints}
        self.escalations = escalations if escalations is not None else []
        self.descriptions = descriptions or {}
        self._ward_cache = {}
        self.reindex()

    def reindex(self):
        self.by_ward = {}
        for cid in sorted(self.complaints):
            c = self.complaints[cid]
            self.by_ward.setdefault(c["ward_id"], []).append(c)

    def add_complaint(self, c):
        self.complaints[c["id"]] = c
        self.reindex()

    # --- tree helpers -----------------------------------------------------
    def level_of(self, unit_id):
        return UNIT_TYPE_LEVEL[self.units[unit_id]["type"]]

    def ancestor(self, ward_id, level):
        want = LEVEL_UNIT_TYPE[level]
        u = self.units[ward_id]
        while u["type"] != want:
            u = self.units[u["parent"]]
        return u["id"]

    def path(self, unit_id):
        out = []
        u = self.units[unit_id]
        while u is not None:
            out.append({"id": u["id"], "type": u["type"], "name": u["name"], "short": u["short"]})
            u = self.units.get(u["parent"]) if u.get("parent") else None
        out.reverse()
        return out

    def subtree(self, unit_id):
        """Every unit id in the jurisdiction of unit_id, including itself."""
        u = self.units[unit_id]
        if u["type"] == "region":
            return [unit_id] + sorted(u["ward_ids"])
        out, stack = [], [unit_id]
        while stack:
            uid = stack.pop()
            out.append(uid)
            stack.extend(self.children[uid])
        return sorted(out)

    def wards_under(self, unit_id):
        if unit_id not in self._ward_cache:
            self._ward_cache[unit_id] = [uid for uid in self.subtree(unit_id) if self.units[uid]["type"] == "ward"]
        return self._ward_cache[unit_id]

    def complaints_in(self, unit_id):
        out = []
        for w in self.wards_under(unit_id):
            out.extend(self.by_ward.get(w, []))
        out.sort(key=lambda c: c["id"])
        return out

    def peer_group(self, unit_id):
        """(level, within) of the authorised comparison group, or None."""
        u = self.units[unit_id]
        level = UNIT_TYPE_LEVEL[u["type"]]
        if level not in LEVEL_PEER_NOUN or not u.get("parent"):
            return None
        return level, u["parent"]


# ---------------------------------------------------------------------------
# Loading the seed file (relative minutes -> absolute ms at load time)
# ---------------------------------------------------------------------------
def dataset_from_seed(seed, now):
    units = seed["units"]
    authorities = seed["authorities"]
    descriptions = seed.get("descriptions", {})
    ds = Dataset(units, authorities, [], [], descriptions)
    complaints = []
    for row in seed["complaints"]:
        complaints.append(complaint_from_seed(ds, row, now))
    ds.complaints = {c["id"]: c for c in complaints}
    ds.reindex()
    ds.escalations = derive_escalations(ds, complaints)
    return ds


def complaint_from_seed(ds, row, now):
    (cid, category, title, ward_id, severity, upvotes, flags, created, status,
     asg_rows, res, conf, conf_at, reopen) = row

    def t(minutes):
        return now + minutes * MIN_MS

    window = sla_ms(category)
    assignments = []
    for i, a in enumerate(asg_rows):
        level, start, outcome, outcome_at = a[0], a[1], a[2], a[3]
        unit_id = ds.ancestor(ward_id, level)
        if i == 0:
            reason = "NEW"
        elif assignments[-1]["outcome"] == "REOPENED":
            reason = "REOPEN"
        else:
            reason = "ESCALATION"
        rec = {"level": level, "unit_id": unit_id, "authority_id": ds.authority_by_unit[unit_id]["id"],
               "assigned_at": t(start), "deadline": t(start) + window, "outcome": outcome,
               "outcome_at": t(outcome_at) if outcome else None, "reason": reason,
               "closed_at": None, "closed_evidence": None}
        if outcome == "REOPENED":
            rec["closed_at"] = t(a[4][0])
            rec["closed_evidence"] = bool(a[4][1])
        assignments.append(rec)
    resolution = None
    if res:
        unit_id = ds.ancestor(ward_id, res[1])
        resolution = {"at": t(res[0]), "level": res[1], "authority_id": ds.authority_by_unit[unit_id]["id"],
                      "evidence": bool(res[2]),
                      "note": "Resolved - site photo attached." if res[2] else "Marked resolved without photo evidence."}
    ward = ds.units[ward_id]
    return {
        "id": cid, "category": category, "title": title,
        "description": ds.descriptions.get(cid) or f"{title}. Reported by a resident of {ward['name']}.",
        "ward_id": ward_id, "severity": severity, "upvotes": upvotes,
        "community_verified": bool(flags & 1), "gov_verified": bool(flags & 2),
        "reporter": "citizen-demo" if flags & 4 else "anonymous",
        "created_at": t(created), "status": status, "level": assignments[-1]["level"],
        "assignments": assignments, "resolution": resolution,
        "confirmation": "CONFIRMED" if conf == 1 else None,
        "confirmation_at": t(conf_at) if conf == 1 else None,
        "reopen_count": reopen, "events": [], "source": "seed", "intake_id": None,
    }


def derive_escalations(ds, complaints):
    out = []
    for c in sorted(complaints, key=lambda x: x["id"]):
        asg = c["assignments"]
        for i, a in enumerate(asg):
            if a["outcome"] == "ESCALATED" and i + 1 < len(asg):
                out.append(escalation_record(c, a, asg[i + 1], "AUTO_SLA", auto_reason(c, a)))
    return out


def auto_reason(c, a):
    days = CATEGORY_SLA_DAYS.get(c["category"], DEFAULT_SLA_DAYS)
    return f"SLA of {days} days breached at {LEVEL_LABEL[a['level']]} level - escalated automatically"


def escalation_record(c, frm, to, trigger, reason):
    return {"complaint_id": c["id"], "from_level": frm["level"], "to_level": to["level"],
            "from_authority": frm["authority_id"], "to_authority": to["authority_id"],
            "escalated_at": to["assigned_at"], "sla_deadline": frm["deadline"],
            "elapsed_ms": to["assigned_at"] - frm["assigned_at"], "reason": reason, "trigger": trigger}


# ---------------------------------------------------------------------------
# SLA escalation (section 5/6) - runs on a schedule and before every read
# ---------------------------------------------------------------------------
def escalate(ds, c, at, trigger, reason):
    last = c["assignments"][-1]
    idx = ESCALATION_CHAIN.index(c["level"])
    next_level = ESCALATION_CHAIN[idx + 1]
    unit_id = ds.ancestor(c["ward_id"], next_level)
    auth = ds.authority_by_unit[unit_id]
    last["outcome"] = "ESCALATED"
    last["outcome_at"] = at
    nxt = {"level": next_level, "unit_id": unit_id, "authority_id": auth["id"], "assigned_at": at,
           "deadline": at + sla_ms(c["category"]), "outcome": None, "outcome_at": None,
           "reason": "ESCALATION", "closed_at": None, "closed_evidence": None}
    c["assignments"].append(nxt)
    c["level"] = next_level
    c["status"] = "ESCALATED"
    rec = escalation_record(c, last, nxt, trigger, reason)
    ds.escalations.append(rec)
    return rec


def run_escalations(ds, now):
    """Escalate every open complaint whose current SLA deadline has passed.

    The escalation is stamped at the deadline itself (the moment of breach),
    so the result does not depend on how often the job runs, and a server that
    was offline for a while catches up level by level.
    Returns (changed complaint ids, new escalation records)."""
    changed, records = [], []
    for cid in sorted(ds.complaints):
        c = ds.complaints[cid]
        while c["status"] in OPEN:
            last = c["assignments"][-1]
            if now <= last["deadline"] or c["level"] not in ESCALATION_CHAIN:
                break
            if ESCALATION_CHAIN.index(c["level"]) == len(ESCALATION_CHAIN) - 1:
                break  # top of the chain: stays overdue
            records.append(escalate(ds, c, last["deadline"], "AUTO_SLA", auto_reason(c, last)))
            if cid not in changed:
                changed.append(cid)
    return changed, records


# ---------------------------------------------------------------------------
# Periods (section 24) - calendar boundaries in IST
# ---------------------------------------------------------------------------
def _ymd(ms):
    d = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=ms + TZ_MS)
    return d.year, d.month, d.day


def _date_ms(y, m, d):
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    return calendar.timegm((y, m, d, 0, 0, 0)) * 1000 - TZ_MS


def _fmt_day(ms):
    y, m, d = _ymd(ms)
    return f"{d:02d} {MONTHS[m - 1][:3]} {y}"


def parse_date(s):
    y, m, d = (int(p) for p in s.split("-"))
    return _date_ms(y, m, d)


def period_range(key, now, frm=None, to=None):
    y, m, d = _ymd(now)
    today = _date_ms(y, m, d)
    if key == "today":
        return _period(key, "Today", today, today + DAY_MS, today - DAY_MS, today, "vs yesterday")
    if key == "7d":
        return _period(key, "Last 7 days", now - 7 * DAY_MS, now + 1, now - 14 * DAY_MS, now - 7 * DAY_MS, "vs previous 7 days")
    if key == "30d":
        return _period(key, "Last 30 days", now - 30 * DAY_MS, now + 1, now - 60 * DAY_MS, now - 30 * DAY_MS, "vs previous 30 days")
    if key == "this_month":
        return _period(key, f"This month ({MONTHS[m - 1]} {y})", _date_ms(y, m, 1), _date_ms(y, m + 1, 1),
                       _date_ms(y, m - 1, 1), _date_ms(y, m, 1), "vs last month")
    if key == "last_month":
        py, pm, _ = _ymd(_date_ms(y, m - 1, 1))
        return _period(key, f"Last month ({MONTHS[pm - 1]} {py})", _date_ms(y, m - 1, 1), _date_ms(y, m, 1),
                       _date_ms(y, m - 2, 1), _date_ms(y, m - 1, 1), "vs the month before")
    if key == "this_quarter":
        qm = ((m - 1) // 3) * 3 + 1
        label = f"This quarter ({MONTHS[qm - 1][:3]}-{MONTHS[qm + 1][:3]} {y})"
        return _period(key, label, _date_ms(y, qm, 1), _date_ms(y, qm + 3, 1),
                       _date_ms(y, qm - 3, 1), _date_ms(y, qm, 1), "vs last quarter")
    if key == "this_year":
        return _period(key, f"This year ({y})", _date_ms(y, 1, 1), _date_ms(y + 1, 1, 1),
                       _date_ms(y - 1, 1, 1), _date_ms(y, 1, 1), "vs last year")
    if key == "custom" and frm and to:
        f, t = parse_date(frm), parse_date(to) + DAY_MS
        if t <= f:
            raise PolicyError(422, "The custom range must end on or after its start date.")
        return _period(key, f"{_fmt_day(f)} - {_fmt_day(t - DAY_MS)}", f, t, f - (t - f), f, "vs the previous equal period")
    return _period("all", "All time", 0, FAR_FUTURE, None, None, None)


def _period(key, label, f, t, pf, pt, prev_label):
    return {"key": key, "label": label, "from": f, "to": t,
            "prev": {"from": pf, "to": pt, "label": prev_label} if pf is not None else None}


# ---------------------------------------------------------------------------
# Metrics (sections 4, 21, 22, 23, 27)
# ---------------------------------------------------------------------------
def is_valid_resolution(c):
    """Section 16: a closure counts only with photo evidence or citizen confirmation."""
    r = c["resolution"]
    if c["status"] != "RESOLVED" or r is None:
        return False
    if not RESOLUTION_POLICY["requires_evidence_or_confirmation"]:
        return True
    return bool(r["evidence"]) or c["confirmation"] == "CONFIRMED"


def _counts():
    return {"received": 0, "resolved": 0, "unverified": 0, "within": 0, "breached": 0, "determined": 0,
            "pending": 0, "overdue": 0, "escalated": 0, "esc_resolved": 0, "esc_pending": 0, "reopened": 0,
            "resolved_by_other": 0, "open_in_sla": 0, "evidence": 0, "confirmed": 0, "dur_sum": 0,
            "speed_sum": 0.0}


def level_counts(complaints, level, f, t, now):
    """What the authority at `level` did with the complaints assigned to it.

    Cohort = complaints whose first assignment at this level falls in [f, t).
    A Ward Councillor is credited only for complaints resolved at ward level;
    once a complaint escalates it counts as escalated (not resolved) for them.
    """
    m = _counts()
    for c in complaints:
        asg = [a for a in c["assignments"] if a["level"] == level]
        if not asg:
            continue
        first, last = asg[0], asg[-1]
        if not (f <= first["assigned_at"] < t):
            continue
        res = c["resolution"]
        resolved = c["status"] == "RESOLVED"
        resolved_here = resolved and res is not None and res["level"] == level
        valid_here = resolved_here and is_valid_resolution(c)
        escalated = any(a["outcome"] == "ESCALATED" for a in asg)
        reopened = any(a["outcome"] == "REOPENED" for a in asg)
        open_here = (not resolved) and c["level"] == level
        late = resolved_here and res["at"] > last["deadline"]
        breached = escalated or (open_here and now > last["deadline"]) or late
        m["received"] += 1
        if valid_here:
            m["resolved"] += 1
            dur = res["at"] - first["assigned_at"]
            m["dur_sum"] += dur
            m["speed_sum"] += max(0.0, 1.0 - dur / (first["deadline"] - first["assigned_at"]))
            if not late:
                m["within"] += 1
            if res["evidence"]:
                m["evidence"] += 1
            if c["confirmation"] == "CONFIRMED":
                m["confirmed"] += 1
        elif resolved_here:
            m["unverified"] += 1
        elif resolved and not escalated:
            m["resolved_by_other"] += 1
        if resolved_here or breached:
            m["determined"] += 1
        if breached:
            m["breached"] += 1
            if not resolved:
                m["overdue"] += 1
        if open_here:
            m["pending"] += 1
            if not breached:
                m["open_in_sla"] += 1
        if escalated:
            m["escalated"] += 1
            if resolved and is_valid_resolution(c):
                m["esc_resolved"] += 1
            elif not resolved:
                m["esc_pending"] += 1
        if reopened:
            m["reopened"] += 1
    return m


def system_counts(complaints, f, t, now):
    """Outcome of every complaint filed in a jurisdiction, whoever resolved it."""
    m = _counts()
    for c in complaints:
        if not (f <= c["created_at"] < t):
            continue
        res = c["resolution"]
        asg = c["assignments"]
        first, last = asg[0], asg[-1]
        resolved = c["status"] == "RESOLVED"
        valid = resolved and is_valid_resolution(c)
        escalated = any(a["outcome"] == "ESCALATED" for a in asg)
        late = resolved and res["at"] > last["deadline"]
        breached = escalated or ((not resolved) and now > last["deadline"]) or late
        m["received"] += 1
        if valid:
            m["resolved"] += 1
            dur = res["at"] - c["created_at"]
            m["dur_sum"] += dur
            m["speed_sum"] += max(0.0, 1.0 - dur / (first["deadline"] - first["assigned_at"]))
            if not breached:
                m["within"] += 1
            if res["evidence"]:
                m["evidence"] += 1
            if c["confirmation"] == "CONFIRMED":
                m["confirmed"] += 1
        elif resolved:
            m["unverified"] += 1
        if resolved or breached:
            m["determined"] += 1
        if breached:
            m["breached"] += 1
            if not resolved:
                m["overdue"] += 1
        if not resolved:
            m["pending"] += 1
            if not breached:
                m["open_in_sla"] += 1
        if escalated:
            m["escalated"] += 1
            if valid:
                m["esc_resolved"] += 1
            elif not resolved:
                m["esc_pending"] += 1
        if c["reopen_count"] > 0:
            m["reopened"] += 1
    return m


def finalize(m):
    """Counts -> the public metrics block (rates as percentages, 1 decimal).

    Rates use complaints that are *due* - resolved, or past their SLA - as the
    denominator. Complaints still inside their SLA window are reported as
    "in progress within SLA" and count neither for nor against the authority
    yet, so a ward that just received ten complaints is not penalised for them.
    """
    closures = m["resolved"] + m["unverified"]
    due = m["received"] - m["open_in_sla"]
    return {
        "received": m["received"], "due": due, "in_progress_within_sla": m["open_in_sla"],
        "resolved": m["resolved"], "unverified_closures": m["unverified"],
        "pending": m["pending"], "overdue": m["overdue"], "escalated": m["escalated"],
        "escalated_resolved": m["esc_resolved"], "escalated_pending": m["esc_pending"],
        "within_sla": m["within"], "sla_breached": m["breached"], "sla_determined": m["determined"],
        "reopened": m["reopened"], "resolved_by_other": m["resolved_by_other"],
        "resolution_rate": pct(m["resolved"], due),
        "sla_compliance": pct(m["within"], m["determined"]),
        "avg_resolution_days": r1(m["dur_sum"] / m["resolved"] / DAY_MS) if m["resolved"] else None,
        "escalation_rate": pct(m["escalated"], due),
        "escalation_resolution_rate": pct(m["esc_resolved"], m["escalated"]),
        "reopen_rate": pct(m["reopened"], due),
        "verified_closure_share": pct(m["resolved"], closures),
        "evidence_rate": pct(m["evidence"], m["resolved"]),
        "citizen_confirmation_rate": pct(m["confirmed"], m["resolved"]),
    }


def score_of(m):
    """Configurable weighted score (section 15). None = insufficient data (section 27)."""
    due = m["received"] - m["open_in_sla"]
    if m["received"] < SCORING["min_received"] or due < SCORING["min_due"] or m["determined"] < 1:
        return None
    w = SCORING["weights"]
    resolution = 100.0 * m["resolved"] / due
    sla = 100.0 * m["within"] / m["determined"]
    speed = 100.0 * m["speed_sum"] / m["resolved"] if m["resolved"] else 0.0
    escalation = 100.0 - 100.0 * m["escalated"] / due
    base = w["resolution"] * resolution + w["sla"] * sla + w["speed"] * speed + w["escalation"] * escalation
    penalty = SCORING["reopen_penalty"] * m["reopened"] / due
    value = min(100.0, max(0.0, base - penalty))
    return {
        "value": value,
        "components": [
            {"key": "resolution", "label": "Verified resolution rate", "value": r1(resolution),
             "weight": w["resolution"], "points": r1(w["resolution"] * resolution)},
            {"key": "sla", "label": "SLA compliance", "value": r1(sla), "weight": w["sla"],
             "points": r1(w["sla"] * sla)},
            {"key": "speed", "label": "Timeliness (SLA window left)", "value": r1(speed), "weight": w["speed"],
             "points": r1(w["speed"] * speed)},
            {"key": "escalation", "label": "Avoided escalation", "value": r1(escalation),
             "weight": w["escalation"], "points": r1(w["escalation"] * escalation)},
        ],
        "penalty": r1(penalty),
    }


def insufficient_reason(m):
    if m["received"] == 0:
        return "No complaints received during this period."
    if m["received"] < SCORING["min_received"]:
        return f"Only {m['received']} complaint(s) in this period - at least {SCORING['min_received']} are needed to score fairly."
    return "Most complaints in this period are still inside their SLA window - not enough outcomes to score yet."


def evaluate(ds, unit_id, f, t, now, complaints=None):
    """Performance of the authority that owns unit_id over the cohort [f, t)."""
    cs = complaints if complaints is not None else ds.complaints_in(unit_id)
    level = ds.level_of(unit_id)
    sys_c = system_counts(cs, f, t, now)
    own_c = None
    if level in ESCALATION_CHAIN and level != "COUNTRY":
        own_c = level_counts(cs, level, f, t, now)
    ss = score_of(sys_c)
    value, basis, detail, blend = None, None, None, None
    if level == "WARD":
        so = score_of(own_c)
        primary = own_c
        if so:
            value, basis, detail = so["value"], "own", so
    elif level == "REGION" or level == "COUNTRY":
        primary = sys_c
        if ss:
            value, basis, detail = ss["value"], "system", ss
    else:
        primary = sys_c
        weights = SCORING["level_blend"][level]
        so = score_of(own_c) if own_c["received"] >= SCORING["min_own_received"] else None
        if ss:
            if so and weights["own"] > 0:
                value = weights["own"] * so["value"] + weights["system"] * ss["value"]
                basis = "blend"
                blend = {"own_weight": weights["own"], "system_weight": weights["system"],
                         "own_score": r1(so["value"]), "system_score": r1(ss["value"])}
            else:
                value, basis = ss["value"], "system"
            detail = ss
    return {
        "unit_id": unit_id, "level": level,
        "score": r1(value) if value is not None else None,
        "basis": basis, "detail": detail, "blend": blend,
        "insufficient_reason": None if value is not None else insufficient_reason(primary),
        "primary": primary, "system": sys_c, "own": own_c,
    }


def band(score):
    if score is None:
        return "none"
    if score >= SCORING["bands"]["good"]:
        return "good"
    return "watch" if score >= SCORING["bands"]["watch"] else "critical"


# ---------------------------------------------------------------------------
# Ranking (sections 15, 25)
# ---------------------------------------------------------------------------
def ranking(ds, level, within_id, f, t, now):
    utype = LEVEL_UNIT_TYPE[level]
    units = [uid for uid in ds.subtree(within_id) if ds.units[uid]["type"] == utype and uid != within_id]
    rows = []
    for uid in sorted(units):
        ev = evaluate(ds, uid, f, t, now)
        pm = finalize(ev["primary"])
        rows.append({"unit_id": uid, "score": ev["score"], "resolution_rate": pm["resolution_rate"], "ev": ev, "pm": pm})
    ranked = [r for r in rows if r["score"] is not None]
    ranked = _sort_ranked(ranked)
    for i, r in enumerate(ranked):
        r["position"] = i + 1
    unranked = [r for r in rows if r["score"] is None]
    for r in unranked:
        r["position"] = None
    return ranked, unranked


def _sort_ranked(rows):
    # score desc, then resolution rate desc, then id - mirrored exactly in engine.js
    return sorted(rows, key=lambda r: (-r["score"], -(r["resolution_rate"] or 0), r["unit_id"]))


def group_label(ds, level, within_id, count):
    singular, plural = LEVEL_PEER_NOUN[level]
    noun = singular if count == 1 else plural
    within = ds.units[within_id]
    where = "nationwide" if within["type"] == "country" else f"in {within['name']}"
    return f"{count} {noun} {where}"


def rank_context(ds, unit_id, f, t, now):
    group = ds.peer_group(unit_id)
    if not group:
        return None
    level, within = group
    ranked, unranked = ranking(ds, level, within, f, t, now)
    me = next((r for r in ranked if r["unit_id"] == unit_id), None)
    singular, plural = LEVEL_PEER_NOUN[level]
    within_u = ds.units[within]
    where = "nationwide" if within_u["type"] == "country" else f"in {within_u['name']}"
    total = len(ranked)
    if me:
        label = f"Rank #{me['position']} of {total} {singular if total == 1 else plural} {where}"
    else:
        label = f"Not ranked - insufficient data ({total} {singular if total == 1 else plural} ranked {where})"
    return {"position": me["position"] if me else None, "of": total, "unranked": len(unranked),
            "level": level, "within": within, "within_name": within_u["name"], "label": label,
            "peer_average": r1(sum(r["score"] for r in ranked) / total) if total else None}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def unit_brief(ds, unit_id):
    u = ds.units[unit_id]
    a = ds.authority_by_unit.get(unit_id)
    return {"id": u["id"], "type": u["type"], "name": u["name"], "short": u["short"],
            "level": UNIT_TYPE_LEVEL[u["type"]],
            "authority": {"id": a["id"], "title": a["title"], "role": a["role"], "role_label": a["role_label"]} if a else None}


def child_units(ds, unit_id):
    u = ds.units[unit_id]
    if u["type"] == "region":
        return sorted(u["ward_ids"])
    return list(ds.children[unit_id])


def row_for(ds, uid, ev, position=None):
    pm = finalize(ev["primary"])
    sm = finalize(ev["system"])
    brief = unit_brief(ds, uid)
    return {**brief, "score": ev["score"], "band": band(ev["score"]), "position": position,
            "basis": ev["basis"], "insufficient_reason": ev["insufficient_reason"],
            "received": pm["received"], "resolved": pm["resolved"], "pending": sm["pending"],
            "overdue": sm["overdue"], "escalated": pm["escalated"],
            "resolution_rate": pm["resolution_rate"], "sla_compliance": pm["sla_compliance"],
            "avg_resolution_days": pm["avg_resolution_days"], "escalation_rate": pm["escalation_rate"],
            "reopen_rate": pm["reopen_rate"], "unverified_closures": pm["unverified_closures"]}


def children_view(ds, unit_id, f, t, now):
    kids = child_units(ds, unit_id)
    if not kids:
        return None
    level = ds.level_of(kids[0])
    rows = []
    for uid in kids:
        ev = evaluate(ds, uid, f, t, now)
        rows.append((uid, ev))
    ranked = _sort_ranked([{"unit_id": uid, "score": ev["score"],
                            "resolution_rate": finalize(ev["primary"])["resolution_rate"], "ev": ev}
                           for uid, ev in rows if ev["score"] is not None])
    items = [row_for(ds, r["unit_id"], r["ev"], i + 1) for i, r in enumerate(ranked)]
    items += [row_for(ds, uid, ev) for uid, ev in rows if ev["score"] is None]
    plural = LEVEL_PEER_NOUN.get(level, ("Ward", "Wards"))[1]
    unit = ds.units[unit_id]
    where = "nationwide" if unit["type"] == "country" else f"in {unit['name']}"
    return {"level": level, "title": f"{plural} {where}", "noun": plural, "unit_type": ds.units[kids[0]]["type"],
            "items": items}


def status_breakdown(complaints, f, t, now):
    out = {"resolved_verified": 0, "unverified_closure": 0, "open_within_sla": 0, "overdue": 0}
    for c in complaints:
        if not (f <= c["created_at"] < t):
            continue
        if c["status"] == "RESOLVED":
            out["resolved_verified" if is_valid_resolution(c) else "unverified_closure"] += 1
        else:
            escalated = any(a["outcome"] == "ESCALATED" for a in c["assignments"])
            if escalated or now > c["assignments"][-1]["deadline"]:
                out["overdue"] += 1
            else:
                out["open_within_sla"] += 1
    return out


def month_windows(now, count, span=3):
    """The last `count` months; each point covers a rolling `span`-month window
    ending with that month, which keeps small wards from swinging wildly."""
    y, m, _ = _ymd(now)
    out = []
    for i in range(count - 1, -1, -1):
        f = _date_ms(y, m - i - span + 1, 1)
        t = _date_ms(y, m - i + 1, 1)
        yy, mm, _ = _ymd(_date_ms(y, m - i, 1))
        out.append((f, t, f"{MONTHS[mm - 1][:3]} {str(yy)[2:]}"))
    return out


def trend(ds, unit_id, now, months=6):
    group = ds.peer_group(unit_id)
    points = []
    for f, t, label in month_windows(now, months):
        ev = evaluate(ds, unit_id, f, t, now)
        pm = finalize(ev["primary"])
        peer_avg = None
        if group:
            ranked, _ = ranking(ds, group[0], group[1], f, t, now)
            if ranked:
                peer_avg = r1(sum(r["score"] for r in ranked) / len(ranked))
        points.append({"label": label, "from": f, "to": t, "partial": t > now, "score": ev["score"],
                       "peer_average": peer_avg, "resolution_rate": pm["resolution_rate"],
                       "sla_compliance": pm["sla_compliance"], "received": pm["received"]})
    return points


def dashboard(ds, unit_id, period, now):
    f, t = period["from"], period["to"]
    cs = ds.complaints_in(unit_id)
    ev = evaluate(ds, unit_id, f, t, now, cs)
    delta = None
    if period["prev"] and ev["score"] is not None:
        prev = evaluate(ds, unit_id, period["prev"]["from"], period["prev"]["to"], now, cs)
        if prev["score"] is not None:
            delta = {"previous": prev["score"], "change": r1(ev["score"] - prev["score"]), "label": period["prev"]["label"]}
    own = None
    if ev["own"] is not None and ev["level"] != "WARD":
        own = finalize(ev["own"])
    primary = finalize(ev["primary"])
    return {
        "unit": unit_brief(ds, unit_id), "path": ds.path(unit_id), "period": period,
        "score": {"value": ev["score"], "band": band(ev["score"]), "basis": ev["basis"],
                  "insufficient_reason": ev["insufficient_reason"],
                  "components": ev["detail"]["components"] if ev["detail"] else None,
                  "penalty": ev["detail"]["penalty"] if ev["detail"] else None, "blend": ev["blend"]},
        "delta": delta,
        "rank": rank_context(ds, unit_id, f, t, now),
        "metrics": primary,
        "system": finalize(ev["system"]),
        "own": own,
        "status_breakdown": status_breakdown(cs, f, t, now),
        "trend": trend(ds, unit_id, now),
        "children": children_view(ds, unit_id, f, t, now),
    }


def rankings_view(ds, level, within_id, period, now, highlight=None):
    ranked, unranked = ranking(ds, level, within_id, period["from"], period["to"], now)
    items = [row_for(ds, r["unit_id"], r["ev"], r["position"]) for r in ranked]
    items += [row_for(ds, r["unit_id"], r["ev"]) for r in unranked]
    for it in items:  # the part of the path between the comparison group and the office itself
        ids = [p["id"] for p in ds.path(it["id"])]
        start = ids.index(within_id) + 1 if within_id in ids else 1
        it["path"] = [p["short"] for p in ds.path(it["id"])][start:-1]
    return {"level": level, "within": unit_brief(ds, within_id), "period": period,
            "label": group_label(ds, level, within_id, len(ranked)), "ranked": len(ranked),
            "unranked": len(unranked), "highlight": highlight, "items": items}


def sla_view(ds, unit_id, period, now, limit=25):
    f, t = period["from"], period["to"]
    cs = ds.complaints_in(unit_id)
    cohort = [c for c in cs if f <= c["created_at"] < t]
    overall = finalize(system_counts(cs, f, t, now))
    by_cat = []
    for cat in CATEGORY_ORDER:
        sub = [c for c in cohort if c["category"] == cat]
        if not sub:
            continue
        m = finalize(system_counts(sub, f, t, now))
        by_cat.append({"category": cat, "sla_days": CATEGORY_SLA_DAYS.get(cat, DEFAULT_SLA_DAYS),
                       "received": m["received"], "within_sla": m["within_sla"], "breached": m["sla_breached"],
                       "determined": m["sla_determined"], "compliance": m["sla_compliance"]})
    by_level = {lvl: 0 for lvl in ESCALATION_CHAIN}
    breaches = []
    for c in cohort:
        for a in c["assignments"]:
            if a["outcome"] == "ESCALATED":
                by_level[a["level"]] += 1
        last = c["assignments"][-1]
        escalated = [a for a in c["assignments"] if a["outcome"] == "ESCALATED"]
        overdue_now = c["status"] != "RESOLVED" and now > last["deadline"]
        if escalated or overdue_now:
            first_breach = escalated[0] if escalated else last
            breaches.append({**complaint_ref(ds, c), "breached_level": first_breach["level"],
                             "deadline": first_breach["deadline"], "status": c["status"],
                             "current_level": c["level"],
                             "current_authority": ds.authorities[last["authority_id"]]["title"],
                             "resolved_at": c["resolution"]["at"] if c["resolution"] else None})
    breaches.sort(key=lambda b: (-b["deadline"], b["id"]))
    at_risk = []
    for c in cs:
        if c["status"] not in OPEN:
            continue
        last = c["assignments"][-1]
        left = last["deadline"] - now
        if 0 < left <= AT_RISK_HOURS * HOUR_MS:
            at_risk.append({**complaint_ref(ds, c), "level": c["level"],
                            "authority": ds.authorities[last["authority_id"]]["title"],
                            "deadline": last["deadline"], "hours_left": r1(left / HOUR_MS)})
    at_risk.sort(key=lambda x: (x["deadline"], x["id"]))
    return {"unit": unit_brief(ds, unit_id), "period": period, "overall": overall,
            "target": SCORING["sla_target"], "by_category": by_cat,
            "by_level": [{"level": k, "label": LEVEL_LABEL[k], "breaches": v} for k, v in by_level.items() if k != "COUNTRY"],
            "breaches": breaches[:limit], "breach_total": len(breaches),
            "at_risk": at_risk[:limit], "at_risk_total": len(at_risk)}


def complaint_ref(ds, c):
    return {"id": c["id"], "title": c["title"], "category": c["category"], "ward_id": c["ward_id"],
            "ward_name": ds.units[c["ward_id"]]["name"]}


def escalation_view(ds, unit_id, period, now, limit=40):
    f, t = period["from"], period["to"]
    cs = ds.complaints_in(unit_id)
    level = ds.level_of(unit_id)
    cohort = [c for c in cs if f <= c["created_at"] < t]
    funnel = []
    for lvl in ESCALATION_CHAIN:
        reached = resolved = open_ = 0
        for c in cohort:
            if any(a["level"] == lvl for a in c["assignments"]):
                reached += 1
                if c["status"] == "RESOLVED" and c["resolution"]["level"] == lvl and is_valid_resolution(c):
                    resolved += 1
                if c["status"] in OPEN and c["level"] == lvl:
                    open_ += 1
        funnel.append({"level": lvl, "label": LEVEL_LABEL[lvl], "reached": reached, "resolved": resolved, "open": open_})
    system = finalize(system_counts(cs, f, t, now))
    own = None
    if level in ESCALATION_CHAIN and level not in ("WARD", "COUNTRY"):
        own = finalize(level_counts(cs, level, f, t, now))
    ids = {c["id"] for c in cs}
    records = []
    for e in ds.escalations:
        if e["complaint_id"] in ids and f <= e["escalated_at"] < t:
            c = ds.complaints[e["complaint_id"]]
            res = c["resolution"]
            records.append({**complaint_ref(ds, c), "from_level": e["from_level"], "to_level": e["to_level"],
                            "from_authority": ds.authorities[e["from_authority"]]["title"],
                            "to_authority": ds.authorities[e["to_authority"]]["title"],
                            "escalated_at": e["escalated_at"], "sla_deadline": e["sla_deadline"],
                            "elapsed_hours": r1(e["elapsed_ms"] / HOUR_MS), "reason": e["reason"],
                            "trigger": e["trigger"],
                            "resolved_at": res["at"] if (res and res["at"] >= e["escalated_at"]) else None,
                            "current_status": c["status"], "current_level": c["level"]})
    records.sort(key=lambda r: (-r["escalated_at"], r["id"]))
    by_child = []
    for uid in child_units(ds, unit_id):
        m = finalize(system_counts(ds.complaints_in(uid), f, t, now))
        if m["received"]:
            by_child.append({**unit_brief(ds, uid), "received": m["received"], "escalated": m["escalated"],
                             "escalation_rate": m["escalation_rate"], "escalated_pending": m["escalated_pending"]})
    by_child.sort(key=lambda r: (-(r["escalation_rate"] or 0), r["id"]))
    return {"unit": unit_brief(ds, unit_id), "period": period, "funnel": funnel, "system": system, "own": own,
            "records": records[:limit], "record_total": len(records), "by_child": by_child}


def geo_view(ds, unit_id, period, now):
    f, t = period["from"], period["to"]
    kids = child_units(ds, unit_id) or [unit_id]
    items = []
    for uid in kids:
        ev = evaluate(ds, uid, f, t, now)
        u = ds.units[uid]
        row = row_for(ds, uid, ev)
        row["center"] = u.get("center")
        row["polygon"] = u.get("polygon")
        items.append(row)
    u = ds.units[unit_id]
    return {"unit": unit_brief(ds, unit_id), "period": period, "center": u.get("center"), "items": items}


# ---------------------------------------------------------------------------
# Complaints for the government dashboard (jurisdiction already applied)
# ---------------------------------------------------------------------------
def days_pending(c, now):
    if c["status"] == "RESOLVED":
        return 0
    return int((now - c["created_at"]) // DAY_MS)


def priority_score(c, now):
    """The existing transparent formula (functions/priority_score.sql)."""
    days = days_pending(c, now)
    v = (c["severity"] * 10 + min(c["upvotes"] * 0.6, 60) + min(days * 3, 45)
         + (20 if c["gov_verified"] else 10 if c["community_verified"] else 0)
         + (30 if c["category"] == "crime" else 18 if c["category"] == "publicsafety" else 0))
    return int(math.floor(v + 0.5))


def complaint_summary(ds, c, now):
    last = c["assignments"][-1]
    auth = ds.authorities[last["authority_id"]]
    open_ = c["status"] in OPEN
    return {
        "id": c["id"], "category": c["category"], "title": c["title"], "description": c["description"],
        "ward_id": c["ward_id"], "ward_name": ds.units[c["ward_id"]]["name"],
        "severity": c["severity"], "upvotes": c["upvotes"],
        "community_verified": c["community_verified"], "gov_verified": c["gov_verified"],
        "status": c["status"], "level": c["level"], "level_label": LEVEL_LABEL[c["level"]],
        "assigned_to": auth["title"], "created_at": c["created_at"], "days_pending": days_pending(c, now),
        "escalation_count": sum(1 for a in c["assignments"] if a["outcome"] == "ESCALATED"),
        "priority_score": priority_score(c, now),
        "sla": {"days": CATEGORY_SLA_DAYS.get(c["category"], DEFAULT_SLA_DAYS), "deadline": last["deadline"],
                "remaining_ms": last["deadline"] - now if open_ else None,
                "breached": open_ and now > last["deadline"]},
        "resolution": c["resolution"], "resolution_valid": is_valid_resolution(c),
        "confirmation": c["confirmation"], "reopen_count": c["reopen_count"],
    }


def list_complaints(ds, ward_ids, now, sort="priority", filt="all", ward=None, limit=50, offset=0):
    items = []
    for w in sorted(ward_ids):
        if ward and w != ward:
            continue
        items.extend(ds.by_ward.get(w, []))
    kpis = {"total": 0, "pending": 0, "progress": 0, "resolved": 0, "escalated": 0, "high": 0,
            "open": 0, "at_risk": 0, "overdue": 0}
    for c in items:
        kpis["total"] += 1
        esc = any(a["outcome"] == "ESCALATED" for a in c["assignments"])
        left = c["assignments"][-1]["deadline"] - now
        if c["status"] == "PENDING":
            kpis["pending"] += 1
        if c["status"] == "IN_PROGRESS":
            kpis["progress"] += 1
        if c["status"] == "RESOLVED":
            kpis["resolved"] += 1
        if esc:
            kpis["escalated"] += 1
        if priority_score(c, now) >= 110:
            kpis["high"] += 1
        if c["status"] in OPEN:
            kpis["open"] += 1
            if 0 < left <= AT_RISK_HOURS * HOUR_MS:
                kpis["at_risk"] += 1
            if esc or left < 0:
                kpis["overdue"] += 1

    def keep(c):
        esc = any(a["outcome"] == "ESCALATED" for a in c["assignments"])
        left = c["assignments"][-1]["deadline"] - now
        if filt == "open":
            return c["status"] in OPEN
        if filt == "escalated":
            return esc
        if filt == "unverified":
            return not c["gov_verified"]
        if filt == "resolved":
            return c["status"] == "RESOLVED"
        if filt == "at_risk":
            return c["status"] in OPEN and 0 < left <= AT_RISK_HOURS * HOUR_MS
        if filt == "overdue":
            return c["status"] in OPEN and (esc or left < 0)
        return True

    items = [c for c in items if keep(c)]
    if sort == "upvoted":
        items.sort(key=lambda c: (-c["upvotes"], c["id"]))
    elif sort == "oldest":
        items.sort(key=lambda c: (c["created_at"], c["id"]))
    elif sort == "recent":
        items.sort(key=lambda c: (-c["created_at"], c["id"]))
    elif sort == "deadline":
        items.sort(key=lambda c: (0 if c["status"] in OPEN else 1, c["assignments"][-1]["deadline"], c["id"]))
    else:
        items.sort(key=lambda c: (0 if c["status"] in OPEN else 1, -priority_score(c, now), c["id"]))
    total = len(items)
    page = items[offset:offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "kpis": kpis,
            "items": [complaint_summary(ds, c, now) for c in page]}


def build_timeline(ds, c):
    days = CATEGORY_SLA_DAYS.get(c["category"], DEFAULT_SLA_DAYS)
    ev = [{"at": c["created_at"], "kind": "REPORTED", "title": "Reported by Anonymous Citizen"}]
    if c["community_verified"]:
        ev.append({"at": c["created_at"] + 6 * HOUR_MS, "kind": "VERIFIED", "title": "Community verification completed"})
    if c["gov_verified"]:
        ev.append({"at": c["created_at"] + 20 * HOUR_MS, "kind": "VERIFIED", "title": "Government authority verified"})
    for a in c["assignments"]:
        who = ds.authorities[a["authority_id"]]["title"]
        if a["reason"] == "NEW":
            ev.append({"at": a["assigned_at"], "kind": "ASSIGNED", "title": f"Assigned to {who} · SLA {days} days"})
        elif a["reason"] == "ESCALATION":
            ev.append({"at": a["assigned_at"], "kind": "ESCALATED", "title": f"Escalated to {who} · fresh {days}-day SLA", "escalated": True})
        else:
            ev.append({"at": a["assigned_at"], "kind": "REOPENED", "title": f"Reopened with {who} · fresh {days}-day SLA", "escalated": True})
        if a["outcome"] == "REOPENED":
            ev.append({"at": a["closed_at"], "kind": "CLOSED",
                       "title": "Marked resolved" + (" with photo evidence" if a["closed_evidence"] else " without evidence")})
            ev.append({"at": a["outcome_at"], "kind": "DISPUTED", "title": "Citizen reported the issue is not fixed", "escalated": True})
        if a["outcome"] == "ESCALATED":
            ev.append({"at": a["outcome_at"], "kind": "SLA_BREACH",
                       "title": f"{days}-day SLA breached at {LEVEL_LABEL[a['level']]} level", "escalated": True})
    for e in c["events"]:
        ev.append(dict(e))
    if c["resolution"]:
        r = c["resolution"]
        who = ds.authorities[r["authority_id"]]["title"] if r["authority_id"] in ds.authorities else r["level"]
        ev.append({"at": r["at"], "kind": "RESOLVED", "done": True,
                   "title": f"Resolved by {who}" + (" · photo evidence attached" if r["evidence"] else " · no evidence attached")})
    if c["confirmation"] == "CONFIRMED":
        ev.append({"at": c["confirmation_at"], "kind": "CONFIRMED", "title": "Citizen confirmed the fix", "done": True})
    return sorted(ev, key=lambda e: e["at"])


def complaint_detail(ds, c, now):
    out = complaint_summary(ds, c, now)
    out["assignments"] = [{**a, "unit_name": ds.units[a["unit_id"]]["name"],
                           "authority_title": ds.authorities[a["authority_id"]]["title"]} for a in c["assignments"]]
    out["escalations"] = [{**e, "from_authority_title": ds.authorities[e["from_authority"]]["title"],
                           "to_authority_title": ds.authorities[e["to_authority"]]["title"]}
                          for e in ds.escalations if e["complaint_id"] == c["id"]]
    out["timeline"] = build_timeline(ds, c)
    return out


# ---------------------------------------------------------------------------
# Mutations (authorisation is checked by access.py before these run)
# ---------------------------------------------------------------------------
def apply_status(ds, actor, c, status, note, evidence, now):
    if c["status"] == "RESOLVED":
        raise PolicyError(409, "This complaint is resolved. Only the citizen's dispute can reopen it.")
    if status == "RESOLVED":
        if not (note or "").strip():
            raise PolicyError(422, "A resolution note is required.")
        last = c["assignments"][-1]
        last["outcome"] = "RESOLVED"
        last["outcome_at"] = now
        c["resolution"] = {"at": now, "level": actor["level"], "authority_id": actor["id"],
                           "evidence": bool(evidence), "note": note.strip()}
        c["status"] = "RESOLVED"
        c["confirmation"] = None
        c["confirmation_at"] = None
        return None
    if status == "ESCALATED":
        if c["level"] not in ESCALATION_CHAIN or c["level"] == ESCALATION_CHAIN[-1]:
            raise PolicyError(409, "This complaint is already at the top of the escalation chain.")
        return escalate(ds, c, now, "MANUAL", f"Escalated manually by {actor['title']}")
    if status in ("PENDING", "VERIFIED", "IN_PROGRESS"):
        c["status"] = status
        c["events"].append({"at": now, "kind": "STATUS", "title": f"Status set to {status.replace('_', ' ').title()} by {actor['title']}",
                            "note": (note or "").strip() or None})
        if status == "VERIFIED":
            c["gov_verified"] = True
        return None
    raise PolicyError(422, f"Unknown status {status}.")


def apply_confirmation(ds, citizen_id, c, decision, now):
    if c["reporter"] != citizen_id:
        raise PolicyError(403, "Only the citizen who reported this complaint can confirm or dispute its resolution.")
    if c["status"] != "RESOLVED":
        raise PolicyError(409, "This complaint is not marked resolved.")
    if c["confirmation"] == "CONFIRMED":
        raise PolicyError(409, "You have already confirmed this resolution.")
    if decision == "CONFIRMED":
        c["confirmation"] = "CONFIRMED"
        c["confirmation_at"] = now
        return
    if decision != "DISPUTED":
        raise PolicyError(422, "decision must be CONFIRMED or DISPUTED.")
    last = c["assignments"][-1]
    res = c["resolution"]
    last["outcome"] = "REOPENED"
    last["outcome_at"] = now
    last["closed_at"] = res["at"]
    last["closed_evidence"] = bool(res["evidence"])
    unit_id = ds.ancestor(c["ward_id"], c["level"])
    c["assignments"].append({"level": c["level"], "unit_id": unit_id,
                             "authority_id": ds.authority_by_unit[unit_id]["id"], "assigned_at": now,
                             "deadline": now + sla_ms(c["category"]), "outcome": None, "outcome_at": None,
                             "reason": "REOPEN", "closed_at": None, "closed_evidence": None})
    c["resolution"] = None
    c["confirmation"] = None
    c["confirmation_at"] = None
    c["reopen_count"] += 1
    c["status"] = "IN_PROGRESS"


def point_in_polygon(lat, lng, poly):
    inside = False
    n = len(poly)
    p1 = poly[0]
    for i in range(1, n + 1):
        p2 = poly[i % n]
        if (p1[0] < lat <= p2[0]) or (p2[0] < lat <= p1[0]):
            if lng <= (p2[1] - p1[1]) * (lat - p1[0]) / (p2[0] - p1[0]) + p1[1]:
                inside = not inside
        p1 = p2
    return inside


def detect_ward(ds, lat, lng):
    """Server-side ward detection - the client's claimed ward is never trusted."""
    for uid in sorted(ds.units):
        u = ds.units[uid]
        if u["type"] == "ward" and u.get("polygon") and point_in_polygon(lat, lng, u["polygon"]):
            return uid
    return None


def new_complaint(ds, cid, category, title, description, ward_id, severity, reporter, now, intake_id=None):
    unit_id = ward_id
    auth = ds.authority_by_unit[unit_id]
    return {
        "id": cid, "category": category, "title": title, "description": description, "ward_id": ward_id,
        "severity": severity, "upvotes": 0, "community_verified": False, "gov_verified": False,
        "reporter": reporter, "created_at": now, "status": "PENDING", "level": "WARD",
        "assignments": [{"level": "WARD", "unit_id": unit_id, "authority_id": auth["id"], "assigned_at": now,
                         "deadline": now + sla_ms(category), "outcome": None, "outcome_at": None,
                         "reason": "NEW", "closed_at": None, "closed_evidence": None}],
        "resolution": None, "confirmation": None, "confirmation_at": None, "reopen_count": 0,
        "events": [], "source": "citizen", "intake_id": intake_id,
    }


def level_can_act(actor_level, complaint_level):
    return LEVEL_RANK[actor_level] >= LEVEL_RANK[complaint_level]
