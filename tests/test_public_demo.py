import pytest

from dashboard.demo import create_app


@pytest.fixture
def app():
    return create_app()


def inject(c, kind="CRASH_LOOP"):
    return c.post("/api/demo/incidents", json={"kind": kind})


@pytest.mark.parametrize("kind", ["CRASH_LOOP", "OOM_KILLED", "PREDICTED_OOM"])
def test_demo_approval_and_recovery(app, kind):
    c = app.test_client()
    iid = inject(c, kind).json["id"]
    assert c.get("/api/config").json["demo"]
    assert c.get("/api/incidents").json["incidents"][0]["can_decide"]
    assert c.post(f"/api/incidents/{iid}/approve", json={}).status_code == 200
    states = [c.get("/api/incidents").json["incidents"][0]["state"] for _ in range(3)]
    assert states == ["REMEDIATING", "VERIFYING", "RESOLVED"]


def test_demo_isolation_rejection_reset_and_limits(app):
    a, b = app.test_client(), app.test_client()
    iid = inject(a).json["id"]
    assert b.get("/api/incidents").json["incidents"] == []
    assert b.post(f"/api/incidents/{iid}/approve", json={}).status_code == 404
    assert a.post(f"/api/incidents/{iid}/reject", json={}).status_code == 200
    assert a.get("/api/incidents").json["incidents"][0]["state"] == "ESCALATED"
    for _ in range(19):
        assert inject(a).status_code == 201
    assert inject(a).status_code == 429
    assert a.post("/api/demo/reset", json={}).status_code == 200
    assert a.get("/api/incidents").json["incidents"] == []


def test_demo_guards_and_health(app):
    c = app.test_client()
    assert inject(c, "DELETE_NAMESPACE").status_code == 400
    assert c.post("/api/demo/incidents", json=[]).status_code == 400
    assert c.post("/api/demo/reset", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.get("/health").status_code == 200
    assert c.get("/health", headers={"Host": "evil.example"}).status_code == 400
    assert c.get("/stream").status_code == 404


def test_render_hostname_allowed(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "example.onrender.com")
    c = create_app().test_client()
    assert c.get("/health", headers={"Host": "example.onrender.com"}).status_code == 200
