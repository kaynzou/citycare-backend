import os
import sys
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

# app/database.py uses a relative SQLite path, so run the whole test session
# from a throwaway directory - the developer's citycare.db is never touched.
_TMP = tempfile.mkdtemp(prefix="citycare-analytics-tests-")
os.chdir(_TMP)


@pytest.fixture(scope="session")
def client():
    import app.main as main
    main.translate_text = lambda text, target="en": ("en", text)  # no network in tests
    from fastapi.testclient import TestClient
    from app.analytics_app import app
    return TestClient(app)


@pytest.fixture()
def fresh(client):
    """Reset the demo dataset so each test starts from the same state."""
    from app.analytics.store import get_repo
    get_repo().reset()
    return client


def login(client, authority_id):
    r = client.post("/api/gov/auth/demo-login", json={"authority_id": authority_id})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}
