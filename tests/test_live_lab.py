import functools
import time

import pytest

from agent.state.store import Store
from lab.runtime import Experiment
from dashboard.live import create_app


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('live experiment did not reach the expected state')


@pytest.mark.parametrize('kind', ['errors', 'latency'])
def test_real_http_fault_restart_and_verified_recovery(kind):
    st = Store()
    releases = []
    e = Experiment(kind, st, lambda: releases.append(True), interval=.01, lifetime=10)
    e.thread.start()
    try:
        wait_for(lambda: e.view()['phase'] == 'AWAITING_APPROVAL')
        before = e.view()
        assert all(s['status']==200 and s['latency_ms']<200 for s in before['samples'][:5])
        assert any(s['status']==503 if kind=='errors' else s['latency_ms']>=200 for s in before['samples'][5:])
        assert st.decide_approval(e.incident_id, True)
        e.thread.join(timeout=8)
        assert not e.thread.is_alive()
        after = e.view()
        assert after['phase']=='COMPLETE'
        assert st.get(e.incident_id)['state']=='RESOLVED'
        assert len({s['pid'] for s in after['samples']})==2
        assert all(s['status']==200 and s['latency_ms']<200 for s in after['samples'][-3:])
        assert after['worker_pid'] is None
        assert releases == [True]
    finally:
        e.stop()
        e.thread.join(timeout=5)


def test_rejection_stops_without_claiming_recovery():
    st = Store()
    e = Experiment('errors', st, lambda: None, interval=.01, lifetime=8)
    e.thread.start()
    try:
        wait_for(lambda: e.incident_id is not None)
        assert st.decide_approval(e.incident_id, False)
        e.thread.join(timeout=5)
        assert e.view()['phase']=='REJECTED'
        assert st.get(e.incident_id)['state']=='ESCALATED'
        assert st.attempted_actions(e.incident_id)==set()
        assert len({s['pid'] for s in e.view()['samples']})==1
        assert e.view()['worker_pid'] is None
    finally:
        e.stop()
        e.thread.join(timeout=5)


def test_live_routes_isolate_visitors_and_enforce_guards(monkeypatch):
    import dashboard.live
    monkeypatch.setattr(dashboard.live, 'Experiment', functools.partial(Experiment, interval=.01))
    app = create_app()
    a, b = app.test_client(), app.test_client()
    assert a.get('/api/config').json == {'demo': False, 'lab': True}
    assert a.post('/api/lab/start', json={'kind':'unknown'}).status_code==400
    assert a.post('/api/lab/start', json={'kind':'errors'}, headers={'Origin':'https://evil.example'}).status_code==403
    assert a.post('/api/lab/start', json={'kind':'errors'}).status_code==201
    try:
        wait_for(lambda: a.get('/api/lab').json['experiment']['phase']=='AWAITING_APPROVAL')
        iid = a.get('/api/lab').json['experiment']['incident_id']
        assert b.get('/api/lab').json['experiment'] is None
        assert b.post(f'/api/incidents/{iid}/approve',json={}).status_code==404
        assert a.post('/api/lab/start',json={'kind':'latency'}).status_code==409
        assert a.post('/api/lab/checkout',json={}).json['status']==503
        assert a.post(f'/api/incidents/{iid}/approve',json={}).status_code==200
        wait_for(lambda: a.get('/api/lab').json['experiment']['phase']=='COMPLETE')
    finally:
        a.post('/api/lab/stop',json={})
    assert a.get('/lab.js').status_code==200
    assert a.get('/stream').status_code==404


def test_expiry_cleans_up_worker_and_releases_capacity():
    released = []
    e = Experiment('errors', Store(), lambda: released.append(True), interval=.01, lifetime=.2)
    e.thread.start()
    e.thread.join(timeout=5)
    assert not e.thread.is_alive()
    assert e.view()['phase']=='EXPIRED'
    assert e.view()['worker_pid'] is None
    assert released==[True]


def test_global_experiment_capacity_is_bounded(monkeypatch):
    import dashboard.live
    monkeypatch.setattr(dashboard.live, 'Experiment', functools.partial(Experiment, interval=.05, lifetime=5))
    app = create_app()
    clients = [app.test_client() for _ in range(5)]
    try:
        for client in clients[:4]:
            assert client.post('/api/lab/start',json={'kind':'errors'}).status_code==201
        assert clients[4].post('/api/lab/start',json={'kind':'errors'}).status_code==429
    finally:
        for client in clients[:4]:
            client.post('/api/lab/stop',json={})
        for client in clients[:4]:
            wait_for(lambda c=client: c.get('/api/lab').json['experiment']['worker_pid'] is None)
