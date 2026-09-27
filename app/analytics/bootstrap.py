"""Attach the Government Portal analytics to a FastAPI app. Safe to call twice."""

import os

from fastapi.staticfiles import StaticFiles

from .config import ESCALATION_JOB_INTERVAL_SECONDS


def install(app):
    if getattr(app.state, "gov_analytics_installed", False):
        return
    from .router import router
    from .store import get_repo, start_escalation_worker

    app.include_router(router)
    portal_dir = os.environ.get("PORTAL_DIR")
    if portal_dir and os.path.isdir(portal_dir):
        app.mount("/portal", StaticFiles(directory=portal_dir, html=True), name="portal")
    get_repo()  # create tables and load (or seed) the jurisdiction dataset
    start_escalation_worker(ESCALATION_JOB_INTERVAL_SECONDS)
    app.state.gov_analytics_installed = True
