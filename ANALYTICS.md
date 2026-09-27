# Government Portal: Hierarchical Analytics, SLA Escalation & Performance Ranking

This adds the accountability layer to CityCare:

    Citizen → Ward Councillor → SDM → DM → CM → PM

Complaints are routed to a ward by GPS, each level gets the category's SLA,
breaches auto-escalate up the chain, and every office is scored and ranked
from the real complaint records, with access enforced by the API.

**Nothing in the existing backend was edited.** `app/main.py`, `models.py`,
`database.py`, `schemas.py` and `translation.py` are untouched; this is all
new files.

| New file | What it does |
|---|---|
| `app/analytics_app.py` | Entry point: imports the existing `app.main:app` and adds the new routes |
| `app/analytics/config.py` | **Every tunable rule**: score weights, category SLAs, chain, thresholds |
| `app/analytics/engine.py` | Metrics, scoring, ranking, SLA escalation job (pure functions) |
| `app/analytics/access.py` | Signed session tokens + the jurisdiction policy |
| `app/analytics/store.py` | New SQLite tables + write-through persistence + background SLA job |
| `app/analytics/router.py` | `/api/gov/...` and `/api/v2/...` endpoints |
| `app/analytics/data/seed.json` | Demo jurisdiction tree + ~2,500 complaint histories |
| `scripts/generate_analytics_seed.py` | Reproducible generator for that dataset |
| `scripts/run_portal.sh` | One command to run API + prototype locally |
| `tests/` | Access control, SLA engine, scoring, API flows, JS/Python parity |

## Run it

```bash
pip install -r requirements.txt          # no new dependencies
bash scripts/run_portal.sh               # add --fresh to reload the demo data
```

- Prototype: http://localhost:8000/portal/ (serves `Civic-Connect/civicconnect-repo/prototype-v2`; set `PORTAL_DIR` if it lives elsewhere)
- API docs: http://localhost:8000/docs
- Tests: `python3 -m pytest tests -q`

The existing app still runs on its own: `uvicorn app.main:app`.

## How it works

**Hierarchy.** `gov_units` holds India → 5 states → 12 districts → 25
sub-districts → 100 wards (Lucknow Sadar's six wards are the ones already in
the prototype, with their real polygons). `gov_authorities` holds one office
per unit: PM, CMs, DMs, SDMs, Ward Councillors, plus the prototype's Village
Head as an observer of a three-ward local region.

**Access control (sections 2, 9, 19, 33).** Login returns an HMAC-signed
token naming an office. Every request re-derives that office's jurisdiction
on the server and checks the requested unit or complaint against it:

| Caller | Complaints | Analytics | Rankings |
|---|---|---|---|
| Ward Councillor | own ward | own ward | own ward's peers in the same sub-district (aggregates only) |
| SDM | its wards | sub-district + wards | its wards; peer SDMs in the district |
| DM | its district | district, SDMs, wards | SDMs/wards in district; peer DMs in the state |
| CM | its state | state and below | districts/SDMs/wards in state; all CMs nationwide |
| PM | all | all | everything |

Changing a URL, ID or query parameter gets `403`. Peers in a comparison group
come back as scores only, flagged `accessible: false`.

**SLA escalation (sections 5, 6).** SLAs per category come from the existing
`escalation_rules` seed (crime 3 days … roads 14 days). A background thread
runs every 30 s, and every read runs it too: any open complaint past its
deadline moves to the next level, stamped at the deadline itself. Each
escalation writes an append-only row to `complaint_escalations_log` with
previous/new authority, time, reason, SLA deadline and elapsed time. Escalation
is automatic; manual escalation is also possible and is recorded as `MANUAL`.

**Scoring (sections 4, 15, 23, 27).** In `config.py`:

    score = 40% verified resolution rate + 30% SLA compliance
          + 20% timeliness (share of SLA window left) + 10% (100 − escalation rate)
          − up to 40 points × citizen-dispute rate

- Rates are over complaints that are **due** (resolved or past SLA). Complaints
  still inside their SLA window don't count for or against anyone yet.
- A Ward Councillor is credited only for what was resolved at ward level; an
  escalated complaint counts as escalated for them even if the SDM fixes it.
- SDM/DM/CM scores blend the escalated cases their office handled ("own",
  section 22) with the outcomes of their whole jurisdiction ("system").
  Weights: SDM 50/50, DM 40/60, CM 30/70. The PM gets the national aggregate.
- Fewer than 3 complaints, or no outcomes yet, gives **Insufficient data**, not
  0% or 100%. Unscored offices are listed separately and never ranked #1.

**Anti-gaming (section 16).** A closure counts only with photo evidence or
citizen confirmation. The reporter can confirm or dispute the fix
(`/api/v2/complaints/{id}/confirmation`). A dispute reopens the complaint at
the same level with a fresh SLA and adds a penalty. In the demo data, Ward 30
closes most complaints within about 2 days but mostly without evidence, and
ranks last all-time.

**Periods (section 24).** Today, 7/30 days, this/last month, quarter, year,
all time, custom range. Calendar periods use IST. Trends use a rolling
3-month window per point so small wards don't swing wildly.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/gov/directory` | Demo official accounts (titles only) |
| POST | `/api/gov/auth/demo-login` | `{authority_id}` → token |
| GET | `/api/gov/me` | Office, jurisdiction path, peer group |
| GET | `/api/gov/complaints` | Jurisdiction-scoped list (`sort`, `filter`, `ward`) |
| GET | `/api/gov/complaints/{id}` | Detail + SLA chain + escalations + timeline |
| PATCH | `/api/gov/complaints/{id}/status` | `{status, note, evidence}` |
| GET | `/api/gov/analytics/dashboard` | Score, rank, KPIs, trend, children (`unit`, `period`) |
| GET | `/api/gov/analytics/rankings` | `level`, `within`, `period` |
| GET | `/api/gov/analytics/sla` | Compliance, by category, at-risk, breaches |
| GET | `/api/gov/analytics/escalations` | Funnel, own handling, records |
| GET | `/api/gov/analytics/geo` | Child units with geometry + scores |
| GET | `/api/gov/analytics/config` | The scoring model |
| POST | `/api/v2/complaints` | Existing intake + server-side ward routing |
| POST | `/api/v2/complaints/{id}/confirmation` | Citizen confirms/disputes a fix |

## Deploying to Render

Merge this branch into the repo Render builds from, then set the service's
**Start Command** to:

    uvicorn app.analytics_app:app --host 0.0.0.0 --port $PORT

Optional environment variables:

- `GOV_TOKEN_SECRET`: keeps sessions valid across restarts.
- `GOV_DEMO_LOGIN=0`: disables the demo account picker and reset endpoint.

SQLite on Render's free tier is wiped on each deploy/restart; the demo data
reloads automatically, with times relative to the restart.

## Production notes

- Demo login stands in for real credentials. In production, map a verified
  identity (e.g. Supabase Auth JWT → the `roles` table in
  `civicconnect-repo/backend/migrations`) to an authority. Nothing after login
  changes.
- The prototype keeps an in-browser mirror of this engine for when the server
  is unreachable (static GitHub Pages). It is badged "Offline demo" in the UI.
  `tests/test_parity.py` fails if the two implementations disagree.
