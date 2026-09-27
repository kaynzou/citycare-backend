"""
Entry point that serves the existing CityCare API *plus* the Government
Portal analytics - without editing app/main.py.

    uvicorn app.analytics_app:app --port 8000

app/main.py keeps working on its own exactly as before (`uvicorn app.main:app`).
This module imports that same app object and adds routes to it:

  /api/complaints ...      existing intake, translation, photos (unchanged)
  /api/gov/...             jurisdiction-scoped complaints + analytics (new)
  /api/v2/...              citizen submit-with-routing, confirm/dispute fixes (new)
  /portal/                 the prototype, when PORTAL_DIR points at it (optional)
"""

import os

from fastapi.staticfiles import StaticFiles

from .analytics.config import ESCALATION_JOB_INTERVAL_SECONDS
from .analytics.router import router as analytics_router
from .analytics.store import get_repo, start_escalation_worker
from .main import app

app.include_router(analytics_router)

_portal_dir = os.environ.get("PORTAL_DIR")
if _portal_dir and os.path.isdir(_portal_dir):
    app.mount("/portal", StaticFiles(directory=_portal_dir, html=True), name="portal")

get_repo()  # create tables and load (or seed) the jurisdiction dataset at startup
start_escalation_worker(ESCALATION_JOB_INTERVAL_SECONDS)
