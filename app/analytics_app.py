"""
Entry point that serves the CityCare API *plus* the Government Portal analytics.

    uvicorn app.analytics_app:app --port 8000

app/main.py now attaches the analytics itself (so `uvicorn app.main:app`, the
command Render already runs, serves them too); this module is kept for the
local runner and anyone already using it.

  /api/complaints ...      existing intake, translation, photos
  /api/gov/...             jurisdiction-scoped complaints + analytics
  /api/v2/...              citizen submit-with-routing, confirm/dispute fixes
  /portal/                 the prototype, when PORTAL_DIR points at it (optional)
"""

from .analytics.bootstrap import install
from .main import app

install(app)  # no-op when app/main.py already installed it
