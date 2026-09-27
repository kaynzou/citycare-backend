"""SLA escalation, scoring and anti-gaming rules on small hand-built datasets."""

import copy

from app.analytics import engine as E
from app.analytics.config import SCORING

DAY = E.DAY_MS
NOW = 1_790_000_000_000  # fixed clock

UNITS = [
    {"id": "IN", "type": "country", "name": "India", "short": "India", "parent": None},
    {"id": "S", "type": "state", "name": "State", "short": "State", "parent": "IN"},
    {"id": "D", "type": "district", "name": "District", "short": "District", "parent": "S"},
    {"id": "SD", "type": "sdm", "name": "Sub-District", "short": "Sub-District", "parent": "D"},
    {"id": "W1", "type": "ward", "name": "Ward 1", "short": "Ward 1", "parent": "SD"},
    {"id": "W2", "type": "ward", "name": "Ward 2", "short": "Ward 2", "parent": "SD"},
]
AUTHS = [{"id": f"a-{u['id']}", "unit": u["id"], "title": f"Head of {u['name']}"} for u in UNITS]


def make(ds, cid, ward, category, created_days_ago):
    c = E.new_complaint(ds, cid, category, cid, cid, ward, 3, "citizen-demo", NOW - created_days_ago * DAY)
    ds.add_complaint(c)
    return c


def resolve(ds, c, days_after_creation, evidence=True, level="WARD"):
    actor = ds.authority_by_unit[ds.ancestor(c["ward_id"], level)]
    E.apply_status(ds, actor, c, "RESOLVED", "fixed", evidence, c["created_at"] + days_after_creation * DAY)


def fresh_ds():
    return E.Dataset(copy.deepcopy(UNITS), AUTHS, [])


def test_auto_escalation_follows_chain_and_is_stamped_at_deadline():
    ds = fresh_ds()
    c = make(ds, "C1", "W1", "garbage", 15)  # 7-day SLA -> breached at ward (day 7) and SDM (day 14)
    changed, records = E.run_escalations(ds, NOW)
    assert changed == ["C1"]
    assert [(r["from_level"], r["to_level"]) for r in records] == [("WARD", "SDM"), ("SDM", "DISTRICT")]
    assert records[0]["escalated_at"] == c["created_at"] + 7 * DAY == records[0]["sla_deadline"]
    assert records[1]["elapsed_ms"] == 7 * DAY
    assert records[0]["from_authority"] == "a-W1" and records[1]["to_authority"] == "a-D"
    assert c["level"] == "DISTRICT" and c["status"] == "ESCALATED"
    # running again changes nothing (idempotent)
    assert E.run_escalations(ds, NOW) == ([], [])


def test_top_of_chain_stays_overdue():
    ds = fresh_ds()
    c = make(ds, "C1", "W1", "crime", 40)  # 3-day SLA x 5 levels = 15 days
    E.run_escalations(ds, NOW)
    assert c["level"] == "COUNTRY" and c["status"] in E.OPEN
    m = E.finalize(E.system_counts([c], 0, E.FAR_FUTURE, NOW))
    assert m["overdue"] == 1 and m["escalated"] == 1


def test_no_complaints_is_insufficient_not_100_percent():
    ds = fresh_ds()
    ev = E.evaluate(ds, "W1", 0, E.FAR_FUTURE, NOW)
    assert ev["score"] is None
    assert "No complaints" in ev["insufficient_reason"]
    ranked, unranked = E.ranking(ds, "WARD", "SD", 0, E.FAR_FUTURE, NOW)
    assert ranked == [] and len(unranked) == 2


def test_open_complaints_within_sla_do_not_count_yet():
    ds = fresh_ds()
    for i in range(4):
        make(ds, f"N{i}", "W1", "roads", 2)  # 14-day SLA, still running
    ev = E.evaluate(ds, "W1", 0, E.FAR_FUTURE, NOW)
    assert ev["score"] is None
    m = E.finalize(ev["primary"])
    assert m["in_progress_within_sla"] == 4 and m["due"] == 0 and m["resolution_rate"] is None


def test_example_from_spec_four_of_five():
    ds = fresh_ds()
    cs = [make(ds, f"C{i}", "W1", "roads", 30) for i in range(5)]
    for c in cs[:4]:
        resolve(ds, c, 3)
    E.run_escalations(ds, NOW)  # the fifth breaches its 14-day SLA
    m = E.finalize(E.evaluate(ds, "W1", 0, E.FAR_FUTURE, NOW)["primary"])
    assert (m["received"], m["resolved"], m["escalated"]) == (5, 4, 1)
    assert m["resolution_rate"] == 80.0 and m["sla_compliance"] == 80.0


def test_score_uses_configured_weights():
    ds = fresh_ds()
    cs = [make(ds, f"C{i}", "W1", "roads", 30) for i in range(4)]
    for c in cs:
        resolve(ds, c, 7)  # half the 14-day window used -> timeliness 50
    ev = E.evaluate(ds, "W1", 0, E.FAR_FUTURE, NOW)
    w = SCORING["weights"]
    expected = w["resolution"] * 100 + w["sla"] * 100 + w["speed"] * 50 + w["escalation"] * 100
    assert ev["score"] == E.r1(expected)


def test_closure_without_evidence_does_not_count_unless_citizen_confirms():
    ds = fresh_ds()
    cs = [make(ds, f"C{i}", "W1", "roads", 30) for i in range(4)]
    for c in cs:
        resolve(ds, c, 1, evidence=False)
    m = E.finalize(E.evaluate(ds, "W1", 0, E.FAR_FUTURE, NOW)["primary"])
    assert m["resolved"] == 0 and m["unverified_closures"] == 4 and m["sla_compliance"] == 0.0
    E.apply_confirmation(ds, "citizen-demo", cs[0], "CONFIRMED", NOW)
    m = E.finalize(E.evaluate(ds, "W1", 0, E.FAR_FUTURE, NOW)["primary"])
    assert m["resolved"] == 1 and m["unverified_closures"] == 3


def test_fast_fake_closures_score_below_genuine_work():
    """Section 16: closing everything instantly must not beat genuine resolution."""
    ds = fresh_ds()
    gamer = [make(ds, f"G{i}", "W1", "roads", 40) for i in range(6)]
    honest = [make(ds, f"H{i}", "W2", "roads", 40) for i in range(6)]
    for c in gamer:
        resolve(ds, c, 0.1, evidence=False)  # closed within hours, no proof
    for c in gamer[:3]:
        E.apply_confirmation(ds, "citizen-demo", c, "DISPUTED", c["created_at"] + 2 * DAY)
    for c in honest:
        resolve(ds, c, 9, evidence=True)  # slower, but genuinely fixed
    E.run_escalations(ds, NOW)
    g = E.evaluate(ds, "W1", 0, E.FAR_FUTURE, NOW)
    h = E.evaluate(ds, "W2", 0, E.FAR_FUTURE, NOW)
    assert h["score"] > g["score"]
    gm = E.finalize(g["primary"])
    assert gm["reopened"] == 3 and gm["escalated"] == 3


def test_dispute_reopens_at_same_level_with_fresh_sla():
    ds = fresh_ds()
    c = make(ds, "C1", "W1", "water", 5)
    resolve(ds, c, 1, evidence=True)
    E.apply_confirmation(ds, "citizen-demo", c, "DISPUTED", NOW - DAY)
    assert c["status"] == "IN_PROGRESS" and c["reopen_count"] == 1
    assert c["assignments"][-1]["reason"] == "REOPEN"
    assert c["assignments"][-1]["deadline"] == NOW - DAY + 7 * DAY


def test_only_reporter_can_confirm():
    ds = fresh_ds()
    c = make(ds, "C1", "W1", "water", 5)
    resolve(ds, c, 1)
    try:
        E.apply_confirmation(ds, "someone-else", c, "CONFIRMED", NOW)
        assert False, "expected PolicyError"
    except E.PolicyError as e:
        assert e.status == 403


def test_sdm_credited_for_escalated_cases_it_resolves():
    ds = fresh_ds()
    cs = [make(ds, f"C{i}", "W1", "garbage", 12) for i in range(4)]
    E.run_escalations(ds, NOW - 4 * DAY)  # all breached at ward level on day 7 -> SDM
    for c in cs[:3]:
        resolve(ds, c, 9, level="SDM")
    own = E.finalize(E.level_counts(ds.complaints_in("SD"), "SDM", 0, E.FAR_FUTURE, NOW))
    # the fourth is still inside the SDM's own 7-day window: not due yet
    assert own["received"] == 4 and own["resolved"] == 3 and own["in_progress_within_sla"] == 1
    later = NOW + 3 * DAY
    E.run_escalations(ds, later)  # ...until it breaches at SDM level too and moves to the DM
    own = E.finalize(E.level_counts(ds.complaints_in("SD"), "SDM", 0, E.FAR_FUTURE, later))
    assert own["resolved"] == 3 and own["escalated"] == 1 and own["resolution_rate"] == 75.0
    assert own["escalation_rate"] == 25.0
    ward = E.finalize(E.level_counts(ds.complaints_in("W1"), "WARD", 0, E.FAR_FUTURE, NOW))
    assert ward["resolved"] == 0 and ward["escalated"] == 4 and ward["escalated_resolved"] == 3


def test_periods_use_ist_calendar():
    # 2026-09-27 00:30 IST == 2026-09-26 19:00 UTC
    now = E._date_ms(2026, 9, 27) + 30 * E.MIN_MS
    p = E.period_range("this_month", now)
    assert p["from"] == E._date_ms(2026, 9, 1) and p["to"] == E._date_ms(2026, 10, 1)
    assert E.period_range("today", now)["from"] == E._date_ms(2026, 9, 27)
    q = E.period_range("this_quarter", now)
    assert q["from"] == E._date_ms(2026, 7, 1) and q["prev"]["from"] == E._date_ms(2026, 4, 1)
