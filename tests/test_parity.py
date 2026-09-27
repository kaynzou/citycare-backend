"""
The prototype's offline engine (prototype-v2/analytics/engine.js) must produce
exactly what the backend produces. This runs the same scenario - reads across
the hierarchy, the SLA job, then official and citizen actions - through both
and compares the JSON.

Set CIVIC_PROTOTYPE_DIR to the prototype-v2 folder if it is not at the default
location; the test is skipped when node or the prototype is unavailable.
"""

import json
import os
import shutil
import subprocess

import pytest

from app.analytics import engine as E

HERE = os.path.dirname(__file__)
SEED = os.path.join(HERE, "..", "app", "analytics", "data", "seed.json")
PROTO = os.environ.get("CIVIC_PROTOTYPE_DIR",
                       os.path.expanduser("~/Downloads/Civic-Connect/civicconnect-repo/prototype-v2"))
ENGINE_JS = os.path.join(PROTO, "analytics", "engine.js")
NOW = 1_790_494_200_000

UNITS = ["IN", "UP", "MH", "UP-LKO", "UP-LKO-SADAR", "ward42", "ward30", "ward15", "UP-LKO-LOCAL", "KA-BLR-STH"]
PERIODS = [("30d", None, None), ("this_month", None, None), ("all", None, None), ("today", None, None),
           ("this_quarter", None, None), ("custom", "2026-06-01", "2026-08-15")]
RANKINGS = [("WARD", "IN"), ("SDM", "UP"), ("STATE", "IN"), ("DISTRICT", "MH"), ("WARD", "UP-LKO-SADAR")]

SCENARIO = [
    ("status", "wc-ward42", "CMP11234", "RESOLVED", "Pothole patched", True),
    ("status", "wc-ward42", "CMP11232", "RESOLVED", "Cleared", False),
    ("status", "wc-ward42", "CMP11233", "ESCALATED", None, False),
    ("status", "sdm-UP-LKO-SADAR", "CMP11229", "IN_PROGRESS", "Team sent", False),
    ("confirm", "citizen-demo", "CMP24002", "DISPUTED"),
]

NODE_DRIVER = r"""
const fs = require('fs');
const E = require(process.argv[2]);
const seed = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const spec = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const out = {};
let now = spec.now;
const ds = E.datasetFromSeed(seed, now);
function reads(tag) {
  spec.units.forEach(u => spec.periods.forEach(([k, f, t]) => {
    out[`${tag}|dash|${u}|${k}`] = E.dashboard(ds, u, E.periodRange(k, now, f, t), now);
  }));
  spec.rankings.forEach(([lvl, w]) => ['30d', 'all'].forEach(k => {
    out[`${tag}|rank|${lvl}|${w}|${k}`] = E.rankingsView(ds, lvl, w, E.periodRange(k, now), now, 'ward42');
  }));
  ['UP-LKO-SADAR', 'IN', 'ward42'].forEach(u => {
    const p = E.periodRange('all', now);
    out[`${tag}|sla|${u}`] = E.slaView(ds, u, p, now);
    out[`${tag}|esc|${u}`] = E.escalationView(ds, u, p, now);
    out[`${tag}|geo|${u}`] = E.geoView(ds, u, E.periodRange('30d', now), now);
  });
  ['priority', 'deadline', 'recent'].forEach(s => ['all', 'overdue', 'at_risk'].forEach(f => {
    out[`${tag}|list|${s}|${f}`] = E.listComplaints(ds, ds.wardsUnder('UP-LKO-SADAR'), now, s, f, null, 30, 0);
  }));
  ['CMP11229', 'CMP11219', 'CMP24002'].forEach(id => { out[`${tag}|detail|${id}`] = E.complaintDetail(ds, ds.complaints[id], now); });
}
reads('t0');
now += 3 * E.DAY_MS + 7 * E.HOUR_MS;
out['job'] = E.runEscalations(ds, now);
spec.scenario.forEach(step => {
  if (step[0] === 'status') E.applyStatus(ds, ds.authorities[step[1]], ds.complaints[step[2]], step[3], step[4], step[5], now);
  else E.applyConfirmation(ds, step[1], ds.complaints[step[2]], step[3], now);
});
reads('t1');
process.stdout.write(JSON.stringify(out));
"""


def python_side(seed):
    out = {}
    now = NOW
    ds = E.dataset_from_seed(seed, now)

    def reads(tag):
        for u in UNITS:
            for k, f, t in PERIODS:
                out[f"{tag}|dash|{u}|{k}"] = E.dashboard(ds, u, E.period_range(k, now, f, t), now)
        for lvl, w in RANKINGS:
            for k in ("30d", "all"):
                out[f"{tag}|rank|{lvl}|{w}|{k}"] = E.rankings_view(ds, lvl, w, E.period_range(k, now), now, "ward42")
        for u in ("UP-LKO-SADAR", "IN", "ward42"):
            p = E.period_range("all", now)
            out[f"{tag}|sla|{u}"] = E.sla_view(ds, u, p, now)
            out[f"{tag}|esc|{u}"] = E.escalation_view(ds, u, p, now)
            out[f"{tag}|geo|{u}"] = E.geo_view(ds, u, E.period_range("30d", now), now)
        for s in ("priority", "deadline", "recent"):
            for f in ("all", "overdue", "at_risk"):
                out[f"{tag}|list|{s}|{f}"] = E.list_complaints(ds, ds.wards_under("UP-LKO-SADAR"), now, s, f, None, 30, 0)
        for cid in ("CMP11229", "CMP11219", "CMP24002"):
            out[f"{tag}|detail|{cid}"] = E.complaint_detail(ds, ds.complaints[cid], now)

    reads("t0")
    now += 3 * E.DAY_MS + 7 * E.HOUR_MS
    out["job"] = list(E.run_escalations(ds, now))
    for step in SCENARIO:
        if step[0] == "status":
            E.apply_status(ds, ds.authorities[step[1]], ds.complaints[step[2]], step[3], step[4], step[5], now)
        else:
            E.apply_confirmation(ds, step[1], ds.complaints[step[2]], step[3], now)
    reads("t1")
    return json.loads(json.dumps(out))


def _diff(a, b, path="", out=None):
    out = [] if out is None else out
    if len(out) > 20:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: missing on {'python' if k not in a else 'js'} side")
            else:
                _diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} (python) != {len(b)} (js)")
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}[{i}]", out)
    elif isinstance(a, float) or isinstance(b, float):
        if a is None or b is None or abs(a - b) > 1e-9:
            out.append(f"{path}: {a!r} (python) != {b!r} (js)")
    elif a != b:
        out.append(f"{path}: {a!r} (python) != {b!r} (js)")
    return out


@pytest.mark.skipif(not shutil.which("node") or not os.path.exists(ENGINE_JS), reason="node or prototype-v2 not available")
def test_offline_engine_matches_backend(tmp_path):
    from scripts.generate_analytics_seed import SEED_JS_PREFIX, SEED_JS_SUFFIX
    with open(SEED) as f:
        seed_text = f.read()
    with open(os.path.join(PROTO, "analytics", "seed.js")) as f:
        assert f.read() == SEED_JS_PREFIX + seed_text + SEED_JS_SUFFIX, \
            "prototype-v2/analytics/seed.js is stale - rerun scripts/generate_analytics_seed.py --copy-to <prototype-v2/analytics>"
    seed = json.loads(seed_text)
    spec = {"now": NOW, "units": UNITS, "periods": [list(p) for p in PERIODS],
            "rankings": [list(r) for r in RANKINGS], "scenario": [list(s) for s in SCENARIO]}
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    (tmp_path / "driver.js").write_text(NODE_DRIVER)
    js = subprocess.run(["node", str(tmp_path / "driver.js"), ENGINE_JS, SEED, str(tmp_path / "spec.json")],
                        capture_output=True, text=True, timeout=120)
    assert js.returncode == 0, js.stderr
    js_out = json.loads(js.stdout)
    py_out = python_side(seed)
    assert set(py_out) == set(js_out)
    problems = _diff(py_out, js_out)
    assert not problems, "\n".join(problems)
    # the scenario really exercised the job and the mutations
    assert py_out["job"][0], "expected the SLA job to escalate something after the clock moved"
    assert py_out["t1|detail|CMP24002"]["reopen_count"] == 1
