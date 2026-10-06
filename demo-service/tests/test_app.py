import pytest

from app import create_app


@pytest.fixture
def exits():
    return []


@pytest.fixture
def client(tmp_path, exits):
    app = create_app({
        "TESTING": True,
        "DEMO_ENDPOINTS_ENABLED": True,
        "CRASH_MARKER": str(tmp_path / "state" / "crash.marker"),
        "EXIT_FN": exits.append,
    })
    return app.test_client()


def test_health(client):
    assert client.get("/health").json["status"] == "ok"


def test_payment_roundtrip(client):
    r = client.post("/api/payments", json={"amount": 12.5})
    assert r.status_code == 201
    assert client.get("/api/payments").json[0]["id"] == r.json["id"]


@pytest.mark.parametrize("body", [{}, {"amount": -1}, {"amount": "5"}, {"amount": True}])
def test_payment_validation(client, body):
    assert client.post("/api/payments", json=body).status_code == 400


def test_crash_once_leaves_no_marker(client, exits, tmp_path):
    assert client.post("/crash").status_code == 202
    assert exits == [1]
    assert not (tmp_path / "state" / "crash.marker").exists()


def test_crash_persist_writes_marker(client, exits, tmp_path):
    client.post("/crash?persist=true")
    assert (tmp_path / "state" / "crash.marker").exists()


def test_demo_endpoints_disabled_by_default(tmp_path):
    c = create_app({"TESTING": True, "DEMO_ENDPOINTS_ENABLED": False}).test_client()
    assert c.post("/crash").status_code == 404
    assert c.post("/leak").status_code == 404


def test_leak_bounds(client):
    assert client.post("/leak?mb=0").status_code == 400
    assert client.post("/leak?mb=abc").status_code == 400
    assert client.post("/leak?mb=1").json["leaked_mb"] >= 1
