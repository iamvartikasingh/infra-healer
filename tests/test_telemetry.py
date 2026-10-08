import json

from agent.state.store import Store
from dashboard.app import create_app
from tests.helpers import mem_series, snap
from tests.test_healer import build, go, reply


def test_telemetry_survives_connections_and_excludes_replaced_pod_from_fit(tmp_path):
    path = str(tmp_path / "evidence.db")
    st = Store(path)
    for s in mem_series(n=8):
        st.record_snapshot(s, "app=x")
    c = create_app(Store(path)).test_client()
    w = c.get("/api/workloads").json["workloads"][0]
    assert len(w["samples"]) == 8
    assert w["trend"]["n_points"] == 8
    assert w["prediction"] and w["trend"]["eta_seconds"] > 0
    st.record_snapshot(snap(uid="replacement", memory_bytes=20 * 1048576), "app=x")
    w = c.get("/api/workloads").json["workloads"][0]
    assert len(w["samples"]) == 9
    assert w["trend"] is None and w["prediction"] is None


def test_workloads_without_metrics_are_explicit_and_namespaces_isolated():
    st = Store()
    st.record_snapshot(snap(namespace="one"), "app=x")
    st.record_snapshot(snap(namespace="two"), "app=x")
    rows = create_app(st).test_client().get("/api/workloads").json["workloads"]
    assert len(rows) == 2
    assert all(w["trend"] is None and w["latest"]["memory_bytes"] is None for w in rows)
    assert len(st.observations("one", "app=x")) == 1


def test_actual_collected_context_is_preserved_and_exposed():
    h, api, st, f, s = build([reply(conf=0.4)])
    iid = go(h, f, s)
    context = json.loads(st.get(iid)["context_json"])
    assert "crash marker" in context["log_tail"]
    view = create_app(st).test_client().get("/api/incidents").json["incidents"][0]
    assert view["context"] == context
