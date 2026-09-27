"""Section 2/9/19/33: jurisdiction is enforced by the API, whatever the client sends."""

from conftest import login


def test_no_token_and_forged_token_are_rejected(client):
    assert client.get("/api/gov/analytics/dashboard").status_code == 401
    assert client.get("/api/gov/complaints").status_code == 401
    real = login(client, "wc-ward42")["Authorization"]
    body, sig = real.split(" ")[1].split(".")
    forged = {"Authorization": f"Bearer {body}.{sig[:-2]}xx"}
    assert client.get("/api/gov/complaints", headers=forged).status_code == 401
    # a citizen token is not a government token
    citizen = client.post("/api/v2/citizen/demo-login").json()["token"]
    assert client.get("/api/gov/complaints", headers={"Authorization": f"Bearer {citizen}"}).status_code == 401


def test_ward_councillor_sees_only_own_ward_complaints(client):
    h = login(client, "wc-ward42")
    data = client.get("/api/gov/complaints?limit=200", headers=h).json()
    assert data["total"] > 0
    assert {c["ward_id"] for c in data["items"]} == {"ward42"}
    # Ward 24's complaint, by id, by filter, or by status change
    assert client.get("/api/gov/complaints/CMP24001", headers=h).status_code == 403
    assert client.get("/api/gov/complaints?ward=ward24", headers=h).status_code == 403
    r = client.patch("/api/gov/complaints/CMP24001/status", headers=h, json={"status": "RESOLVED", "note": "x", "evidence": True})
    assert r.status_code == 403
    assert client.get("/api/gov/complaints/CMP11234", headers=h).status_code == 200


def test_ward_councillor_analytics_scope(client):
    h = login(client, "wc-ward42")
    assert client.get("/api/gov/analytics/dashboard", headers=h).json()["unit"]["id"] == "ward42"
    for unit in ("ward24", "UP-LKO-SADAR", "UP", "IN"):
        assert client.get(f"/api/gov/analytics/dashboard?unit={unit}", headers=h).status_code == 403
        assert client.get(f"/api/gov/analytics/sla?unit={unit}", headers=h).status_code == 403
        assert client.get(f"/api/gov/analytics/escalations?unit={unit}", headers=h).status_code == 403
    # peer comparison is allowed - aggregates only, and peers are not drillable
    r = client.get("/api/gov/analytics/rankings?level=WARD&within=UP-LKO-SADAR", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert {i["id"] for i in items} == {"ward24", "ward42", "ward17", "ward7", "ward15", "ward30"}
    assert [i["accessible"] for i in items if i["id"] != "ward42"] == [False] * 5
    assert all("title" not in i and "description" not in i for i in items)  # no complaint-level data
    # ...but not another SDM's wards, nor a different level of the tree
    assert client.get("/api/gov/analytics/rankings?level=WARD&within=UP-LKO-MLG", headers=h).status_code == 403
    assert client.get("/api/gov/analytics/rankings?level=SDM&within=UP-LKO", headers=h).status_code == 403


def test_sdm_sees_its_wards_not_another_sdms(client):
    h = login(client, "sdm-UP-LKO-SADAR")
    for unit in ("UP-LKO-SADAR", "ward24", "ward42", "ward7"):
        assert client.get(f"/api/gov/analytics/dashboard?unit={unit}", headers=h).status_code == 200
    for unit in ("UP-LKO-MLG-W51", "UP-LKO-MLG", "UP-LKO", "UP-KNP-SADAR"):
        assert client.get(f"/api/gov/analytics/dashboard?unit={unit}", headers=h).status_code == 403
    wards = {c["ward_id"] for c in client.get("/api/gov/complaints?limit=200", headers=h).json()["items"]}
    assert wards <= {"ward24", "ward42", "ward17", "ward7", "ward15", "ward30"}
    assert client.get("/api/gov/complaints?ward=UP-LKO-MLG-W51", headers=h).status_code == 403
    # own peer group (SDMs in Lucknow) is visible as aggregates
    r = client.get("/api/gov/analytics/rankings?level=SDM&within=UP-LKO", headers=h)
    assert r.status_code == 200 and len(r.json()["items"]) == 3


def test_dm_cm_pm_scopes(client):
    dm = login(client, "dm-UP-LKO")
    assert client.get("/api/gov/analytics/dashboard?unit=UP-LKO-MLG-W51", headers=dm).status_code == 200
    assert client.get("/api/gov/analytics/dashboard?unit=UP-KNP", headers=dm).status_code == 403
    cm = login(client, "cm-UP")
    assert client.get("/api/gov/analytics/dashboard?unit=UP-VNS-PND", headers=cm).status_code == 200
    assert client.get("/api/gov/analytics/dashboard?unit=MH", headers=cm).status_code == 403
    assert client.get("/api/gov/analytics/rankings?level=WARD&within=MH", headers=cm).status_code == 403
    r = client.get("/api/gov/analytics/rankings?level=STATE&within=IN", headers=cm)
    assert r.status_code == 200  # CM compares with the other CMs nationwide
    assert [i["accessible"] for i in r.json()["items"] if i["id"] != "UP"] == [False] * 4
    pm = login(client, "pm-IN")
    for unit in ("IN", "MH", "KA-BLR-STH", "ward30"):
        assert client.get(f"/api/gov/analytics/dashboard?unit={unit}", headers=pm).status_code == 200
    assert client.get("/api/gov/analytics/rankings?level=WARD&within=IN", headers=pm).json()["ranked"] > 50


def test_village_head_observes_its_region_only(client):
    h = login(client, "vh-UP-LKO-LOCAL")
    wards = {c["ward_id"] for c in client.get("/api/gov/complaints?limit=200", headers=h).json()["items"]}
    assert wards == {"ward24", "ward42", "ward17"}
    assert client.get("/api/gov/analytics/dashboard?unit=ward7", headers=h).status_code == 403


def test_update_blocked_once_escalated_beyond_your_level(fresh):
    h = login(fresh, "wc-ward42")
    # CMP11229 has been escalated from Ward 42 to the SDM
    r = fresh.patch("/api/gov/complaints/CMP11229/status", headers=h, json={"status": "IN_PROGRESS"})
    assert r.status_code == 403
    sdm = login(fresh, "sdm-UP-LKO-SADAR")
    r = fresh.patch("/api/gov/complaints/CMP11229/status", headers=sdm,
                    json={"status": "RESOLVED", "note": "Drain desilted", "evidence": True})
    assert r.status_code == 200 and r.json()["status"] == "RESOLVED"
