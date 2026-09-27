"""
Persistence for the jurisdiction/lifecycle layer.

New tables only - the existing `complaints` / `complaint_photos` tables from
app/models.py are untouched. A citizen complaint submitted through
/api/v2/complaints gets its intake row there (translation, photos) and its
jurisdiction + SLA lifecycle row here, linked by `intake_id`.

The working set lives in memory (a few thousand rows) and every mutation is
written through to the database, so reads are fast and nothing is lost on
restart. `complaint_escalations` is append-only - no code path updates or
deletes it, mirroring backend/migrations/001_schema.sql.
"""

import json
import os
import threading
import time

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Text, delete, insert, select

from ..database import Base, SessionLocal, engine as db_engine
from . import engine

SEED_PATH = os.path.join(os.path.dirname(__file__), "data", "seed.json")


class GovUnit(Base):
    __tablename__ = "gov_units"
    id = Column(String, primary_key=True)
    type = Column(String, nullable=False)       # country | state | district | sdm | ward | region
    name = Column(String, nullable=False)
    short = Column(String, nullable=False)
    parent_id = Column(String, index=True)
    geo_json = Column(Text)                     # {"center": [...], "polygon": [...], "ward_ids": [...]}


class GovAuthority(Base):
    __tablename__ = "gov_authorities"
    id = Column(String, primary_key=True)
    unit_id = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)


class ComplaintLifecycle(Base):
    __tablename__ = "complaint_lifecycle"
    id = Column(String, primary_key=True)       # complaint number, e.g. CMP11234
    intake_id = Column(String, nullable=True)   # -> complaints.id for citizen submissions
    source = Column(String, nullable=False, default="citizen")
    category = Column(String, nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    ward_id = Column(String, nullable=False, index=True)
    severity = Column(Integer, nullable=False)
    upvotes = Column(Integer, nullable=False, default=0)
    community_verified = Column(Boolean, default=False)
    gov_verified = Column(Boolean, default=False)
    reporter = Column(String, nullable=False)   # never returned to officials
    created_ms = Column(BigInteger, nullable=False)
    status = Column(String, nullable=False)
    level = Column(String, nullable=False)
    resolution_json = Column(Text)
    confirmation = Column(String)
    confirmation_ms = Column(BigInteger)
    reopen_count = Column(Integer, default=0)
    events_json = Column(Text)


class ComplaintAssignment(Base):
    __tablename__ = "complaint_assignments_v2"
    id = Column(Integer, primary_key=True, autoincrement=True)
    complaint_id = Column(String, nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    level = Column(String, nullable=False)
    unit_id = Column(String, nullable=False)
    authority_id = Column(String, nullable=False)
    assigned_ms = Column(BigInteger, nullable=False)
    deadline_ms = Column(BigInteger, nullable=False)
    outcome = Column(String)
    outcome_ms = Column(BigInteger)
    reason = Column(String, nullable=False)
    closed_ms = Column(BigInteger)
    closed_evidence = Column(Boolean)


class ComplaintEscalation(Base):
    __tablename__ = "complaint_escalations_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    complaint_id = Column(String, nullable=False, index=True)
    from_level = Column(String, nullable=False)
    to_level = Column(String, nullable=False)
    from_authority = Column(String, nullable=False)
    to_authority = Column(String, nullable=False)
    escalated_ms = Column(BigInteger, nullable=False)
    sla_deadline_ms = Column(BigInteger, nullable=False)
    elapsed_ms = Column(BigInteger, nullable=False)
    reason = Column(Text, nullable=False)
    trigger = Column(String, nullable=False)    # AUTO_SLA | MANUAL


TABLES = [GovUnit, GovAuthority, ComplaintLifecycle, ComplaintAssignment, ComplaintEscalation]


def now_ms():
    return int(time.time() * 1000)


def _lifecycle_row(c):
    return {"id": c["id"], "intake_id": c.get("intake_id"), "source": c.get("source", "citizen"),
            "category": c["category"], "title": c["title"], "description": c["description"],
            "ward_id": c["ward_id"], "severity": c["severity"], "upvotes": c["upvotes"],
            "community_verified": c["community_verified"], "gov_verified": c["gov_verified"],
            "reporter": c["reporter"], "created_ms": c["created_at"], "status": c["status"], "level": c["level"],
            "resolution_json": json.dumps(c["resolution"]) if c["resolution"] else None,
            "confirmation": c["confirmation"], "confirmation_ms": c["confirmation_at"],
            "reopen_count": c["reopen_count"], "events_json": json.dumps(c["events"])}


def _assignment_rows(c):
    return [{"complaint_id": c["id"], "seq": i, "level": a["level"], "unit_id": a["unit_id"],
             "authority_id": a["authority_id"], "assigned_ms": a["assigned_at"], "deadline_ms": a["deadline"],
             "outcome": a["outcome"], "outcome_ms": a["outcome_at"], "reason": a["reason"],
             "closed_ms": a["closed_at"], "closed_evidence": a["closed_evidence"]}
            for i, a in enumerate(c["assignments"])]


def _escalation_row(e):
    return {"complaint_id": e["complaint_id"], "from_level": e["from_level"], "to_level": e["to_level"],
            "from_authority": e["from_authority"], "to_authority": e["to_authority"],
            "escalated_ms": e["escalated_at"], "sla_deadline_ms": e["sla_deadline"],
            "elapsed_ms": e["elapsed_ms"], "reason": e["reason"], "trigger": e["trigger"]}


class Repo:
    """In-memory dataset + write-through persistence. One per process."""

    def __init__(self):
        self.lock = threading.RLock()
        self.version = 0
        Base.metadata.create_all(bind=db_engine, tables=[t.__table__ for t in TABLES])
        with SessionLocal() as s:
            has_data = s.execute(select(GovUnit.id).limit(1)).first() is not None
        if has_data:
            self.ds = self._load()
        else:
            self.reset()

    # --- lifecycle --------------------------------------------------------
    def reset(self):
        """Drop the analytics rows and reload the demo dataset relative to now."""
        with open(SEED_PATH) as f:
            seed = json.load(f)
        ds = engine.dataset_from_seed(seed, now_ms())
        with SessionLocal() as s:
            for t in TABLES:
                s.execute(delete(t))
            s.execute(insert(GovUnit), [
                {"id": u["id"], "type": u["type"], "name": u["name"], "short": u["short"], "parent_id": u.get("parent"),
                 "geo_json": json.dumps({k: u[k] for k in ("center", "polygon", "ward_ids") if u.get(k) is not None})}
                for u in seed["units"]])
            s.execute(insert(GovAuthority), [{"id": a["id"], "unit_id": a["unit"], "title": a["title"]}
                                             for a in seed["authorities"]])
            cs = list(ds.complaints.values())
            s.execute(insert(ComplaintLifecycle), [_lifecycle_row(c) for c in cs])
            s.execute(insert(ComplaintAssignment), [r for c in cs for r in _assignment_rows(c)])
            if ds.escalations:
                s.execute(insert(ComplaintEscalation), [_escalation_row(e) for e in ds.escalations])
            s.commit()
        with self.lock:
            self.ds = ds
            self.version += 1

    def _load(self):
        with SessionLocal() as s:
            units = []
            for u in s.execute(select(GovUnit)).scalars():
                geo = json.loads(u.geo_json or "{}")
                units.append({"id": u.id, "type": u.type, "name": u.name, "short": u.short,
                              "parent": u.parent_id, **geo})
            auths = [{"id": a.id, "unit": a.unit_id, "title": a.title} for a in s.execute(select(GovAuthority)).scalars()]
            asg = {}
            for a in s.execute(select(ComplaintAssignment).order_by(ComplaintAssignment.complaint_id,
                                                                     ComplaintAssignment.seq)).scalars():
                asg.setdefault(a.complaint_id, []).append({
                    "level": a.level, "unit_id": a.unit_id, "authority_id": a.authority_id,
                    "assigned_at": a.assigned_ms, "deadline": a.deadline_ms, "outcome": a.outcome,
                    "outcome_at": a.outcome_ms, "reason": a.reason, "closed_at": a.closed_ms,
                    "closed_evidence": a.closed_evidence})
            complaints = []
            for r in s.execute(select(ComplaintLifecycle)).scalars():
                complaints.append({
                    "id": r.id, "category": r.category, "title": r.title, "description": r.description,
                    "ward_id": r.ward_id, "severity": r.severity, "upvotes": r.upvotes,
                    "community_verified": bool(r.community_verified), "gov_verified": bool(r.gov_verified),
                    "reporter": r.reporter, "created_at": r.created_ms, "status": r.status, "level": r.level,
                    "assignments": asg.get(r.id, []),
                    "resolution": json.loads(r.resolution_json) if r.resolution_json else None,
                    "confirmation": r.confirmation, "confirmation_at": r.confirmation_ms,
                    "reopen_count": r.reopen_count or 0, "events": json.loads(r.events_json or "[]"),
                    "source": r.source, "intake_id": r.intake_id})
            escalations = [{"complaint_id": e.complaint_id, "from_level": e.from_level, "to_level": e.to_level,
                            "from_authority": e.from_authority, "to_authority": e.to_authority,
                            "escalated_at": e.escalated_ms, "sla_deadline": e.sla_deadline_ms,
                            "elapsed_ms": e.elapsed_ms, "reason": e.reason, "trigger": e.trigger}
                           for e in s.execute(select(ComplaintEscalation).order_by(ComplaintEscalation.id)).scalars()]
        return engine.Dataset(units, auths, complaints, escalations)

    # --- writes -----------------------------------------------------------
    def save(self, complaints, new_escalations=()):
        """Persist the given (already mutated) complaints and append escalation records."""
        if not complaints and not new_escalations:
            return
        with SessionLocal() as s:
            for c in complaints:
                s.merge(ComplaintLifecycle(**_lifecycle_row(c)))
                s.execute(delete(ComplaintAssignment).where(ComplaintAssignment.complaint_id == c["id"]))
                s.execute(insert(ComplaintAssignment), _assignment_rows(c))
            if new_escalations:
                s.execute(insert(ComplaintEscalation), [_escalation_row(e) for e in new_escalations])
            s.commit()
        self.version += 1

    def tick(self, now=None):
        """Run the SLA escalation job. Called on a timer and before every read."""
        now = now or now_ms()
        with self.lock:
            changed, records = engine.run_escalations(self.ds, now)
            if changed:
                self.save([self.ds.complaints[cid] for cid in changed], records)
            return changed, records


_repo = None
_repo_lock = threading.Lock()


def get_repo():
    global _repo
    if _repo is None:
        with _repo_lock:
            if _repo is None:
                _repo = Repo()
    return _repo


def start_escalation_worker(interval_seconds):
    """Background thread: breaches are escalated even when nobody is looking."""

    def loop():
        while True:
            time.sleep(interval_seconds)
            try:
                get_repo().tick()
            except Exception as exc:  # keep the worker alive; next tick retries
                print(f"[analytics] escalation job failed: {exc}")

    t = threading.Thread(target=loop, name="sla-escalation-job", daemon=True)
    t.start()
    return t
