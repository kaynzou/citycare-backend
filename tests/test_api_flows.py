"""End-to-end flows through the API: live updates, citizen routing, existing API untouched."""

import io

from conftest import login


def _score(client, h, unit=None, period="all"):
    q = f"?period={period}" + (f"&unit={unit}" if unit else "")
    return client.get(f"/api/gov/analytics/dashboard{q}", headers=h).json()


def test_resolving_updates_analytics_immediately(fresh):
    h = login(fresh, "wc-ward42")
    before = _score(fresh, h)["metrics"]
    r = fresh.patch("/api/gov/complaints/CMP11234/status", headers=h,
                    json={"status": "RESOLVED", "note": "Pothole patched", "evidence": True})
    assert r.status_code == 200 and r.json()["counts_towards_score"] is True
    after = _score(fresh, h)["metrics"]
    assert after["resolved"] == before["resolved"] + 1
    # the SDM's view of the same ward agrees
    sdm = login(fresh, "sdm-UP-LKO-SADAR")
    assert _score(fresh, sdm, "ward42")["metrics"]["resolved"] == after["resolved"]


def test_resolution_without_note_or_evidence(fresh):
    h = login(fresh, "wc-ward42")
    assert fresh.patch("/api/gov/complaints/CMP11234/status", headers=h, json={"status": "RESOLVED"}).status_code == 422
    r = fresh.patch("/api/gov/complaints/CMP11234/status", headers=h,
                    json={"status": "RESOLVED", "note": "done", "evidence": False})
    assert r.json()["counts_towards_score"] is False


def test_manual_escalation_is_recorded(fresh):
    h = login(fresh, "wc-ward42")
    r = fresh.patch("/api/gov/complaints/CMP11233/status", headers=h, json={"status": "ESCALATED"})
    assert r.status_code == 200
    d = r.json()
    assert d["level"] == "SDM" and d["escalations"][-1]["trigger"] == "MANUAL"
    assert d["permissions"]["can_update"] is False  # now the SDM's case


def test_citizen_submission_is_routed_by_coordinates(fresh):
    citizen = fresh.post("/api/v2/citizen/demo-login").json()["token"]
    form = {"category": "Garbage", "location": "claimed Ward 24", "description": "Bin overflowing",
            "category_key": "garbage", "latitude": "26.858", "longitude": "80.990", "severity": "3"}
    r = fresh.post("/api/v2/complaints", data=form, headers={"Authorization": f"Bearer {citizen}"},
                   files=[("photos", ("bin.jpg", io.BytesIO(b"fake"), "image/jpeg"))])
    assert r.status_code == 200, r.text
    life = r.json()["lifecycle"]
    assert life["ward_id"] == "ward42"  # detected on the server, not the client's claim
    assert life["assigned_to"].startswith("Ward Councillor · Ward 42")
    cid = life["id"]
    # the existing intake API still knows it (translation/photos pipeline reused)
    assert fresh.get(f"/api/complaints/{cid}").status_code == 200
    # Ward 42 sees it, Ward 24 does not
    assert fresh.get(f"/api/gov/complaints/{cid}", headers=login(fresh, "wc-ward42")).status_code == 200
    assert fresh.get(f"/api/gov/complaints/{cid}", headers=login(fresh, "wc-ward24")).status_code == 403
    # outside every onboarded ward -> rejected (non-crime complaints are geo-locked)
    far = dict(form, latitude="10.0", longitude="70.0")
    assert fresh.post("/api/v2/complaints", data=far).status_code == 422


def test_citizen_confirms_and_disputes(fresh):
    citizen = {"Authorization": "Bearer " + fresh.post("/api/v2/citizen/demo-login").json()["token"]}
    # CMP24002 was resolved with evidence and awaits the reporter's confirmation
    r = fresh.post("/api/v2/complaints/CMP24002/confirmation", json={"decision": "DISPUTED"}, headers=citizen)
    assert r.status_code == 200 and r.json()["status"] == "IN_PROGRESS" and r.json()["reopen_count"] == 1
    # someone else's complaint
    assert fresh.post("/api/v2/complaints/CMP11234/confirmation", json={"decision": "CONFIRMED"},
                      headers=citizen).status_code in (403, 409)


def test_existing_api_unchanged(client):
    assert client.get("/").json() == {"status": "CityCare API running"}
    assert client.get("/api/complaints").status_code == 200
