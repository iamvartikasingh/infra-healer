from types import SimpleNamespace as NS

import pytest
from kubernetes.client.rest import ApiException

from agent.diagnose.schema import Action
from agent.remediate.executor import REVISION, ExecStatus, Remediator
from agent.state.store import Store
from agent.verify.verifier import Verifier, workload_selector


def own(kind, name, uid="x"):
    return NS(kind=kind, name=name, uid=uid)


class Core:
    def __init__(self, uid="u1", exists=True):
        self.uid, self.exists, self.deleted = uid, exists, []

    def read_namespaced_pod(self, name, ns):
        if not self.exists:
            raise ApiException(status=404)
        return NS(metadata=NS(uid=self.uid, name=name, owner_references=[own("ReplicaSet", "rs1")]))

    def delete_namespaced_pod(self, name, ns, body=None):
        self.delete_options = body
        self.deleted.append(name)


class Apps:
    def __init__(self, replicas=1, revisions=(("rs-old", 1), ("rs-new", 2)), current=2):
        self.replicas, self.patches = replicas, []
        self.dep = NS(metadata=NS(uid="dep-uid", annotations={REVISION: str(current)}),
                      spec=NS(replicas=replicas, selector=NS(match_labels={"app": "x"})))
        self.sets = [NS(metadata=NS(name=n, annotations={REVISION: str(r)}, owner_references=[own("Deployment", "d", "dep-uid")]),
                        spec=NS(template={"metadata": {"labels": {"app": "x", "pod-template-hash": n}}, "spec": {"image": n}}))
                     for n, r in revisions]
        self.api_client = NS(sanitize_for_serialization=lambda t: __import__("copy").deepcopy(t))

    def read_namespaced_replica_set(self, name, ns):
        return NS(metadata=NS(owner_references=[own("Deployment", "d", "dep-uid")]))

    def read_namespaced_deployment(self, name, ns):
        return self.dep

    def list_namespaced_replica_set(self, ns, label_selector=None):
        return NS(items=self.sets)

    def patch_namespaced_deployment_scale(self, name, ns, body):
        self.patches.append(("scale", body))

    def patch_namespaced_deployment(self, name, ns, body):
        self.patches.append(("deploy", body))


def setup(core=None, apps=None, allowed=None, **kw):
    st = Store()
    iid = st.create_incident("u1", "demo", "pod1", "app=x", "CRASH_LOOP", "d")
    args = {"allowed_actions": allowed} if allowed else {}
    return st, iid, Remediator(core or Core(), apps or Apps(), st, **args, **kw)


def run(r, iid, action=Action.RESTART_POD, uid="u1"):
    return r.execute(iid, action, "demo", "pod1", uid, "autonomous")


def test_restart_deletes_pod():
    core = Core()
    _, iid, r = setup(core)
    res = run(r, iid)
    assert res.status is ExecStatus.EXECUTED and core.deleted == ["pod1"]
    assert core.delete_options.preconditions.uid == "u1"


def test_same_action_never_runs_twice_for_one_incident():
    core = Core()
    st, iid, r = setup(core)
    assert run(r, iid).status is ExecStatus.EXECUTED
    assert run(r, iid).status is ExecStatus.SKIPPED_DUPLICATE
    assert core.deleted == ["pod1"]  # exactly one delete


def test_restart_never_deletes_a_replacement_pod():
    core = Core(uid="DIFFERENT")
    _, iid, r = setup(core)
    res = run(r, iid)
    assert core.deleted == [] and "already replaced" in res.detail


def test_replacement_between_read_and_delete_fails_closed():
    class Replaced(Core):
        def delete_namespaced_pod(self, name, ns, body=None):
            assert body.preconditions.uid == "u1"
            raise ApiException(status=409, reason="UID precondition failed")
    core = Replaced()
    _, iid, r = setup(core)
    assert run(r, iid).status is ExecStatus.FAILED
    assert core.deleted == []


def test_restart_of_missing_pod_is_a_noop():
    core = Core(exists=False)
    _, iid, r = setup(core)
    assert run(r, iid).status is ExecStatus.EXECUTED and core.deleted == []


def test_remediator_enforces_its_own_whitelist_before_claiming():
    st, iid, r = setup(allowed={Action.RESTART_POD})
    res = run(r, iid, Action.ROLLBACK)
    assert res.status is ExecStatus.FAILED and st.attempted_actions(iid) == set()


@pytest.mark.parametrize("a", [Action.ESCALATE_TO_HUMAN, Action.NO_ACTION])
def test_advisory_actions_refused(a):
    _, iid, r = setup()
    assert run(r, iid, a).status is ExecStatus.FAILED


def test_k8s_error_is_reported_not_raised_and_still_claims():
    class Boom(Core):
        def delete_namespaced_pod(self, *a, **kw):
            raise ApiException(status=500, reason="boom")
    st, iid, r = setup(Boom())
    res = run(r, iid)
    assert res.status is ExecStatus.FAILED and "ApiException" in res.detail
    assert run(r, iid).status is ExecStatus.SKIPPED_DUPLICATE  # at-most-once, even after failure


def test_scale_up_adds_one_replica():
    apps = Apps(replicas=2)
    _, iid, r = setup(apps=apps)
    assert run(r, iid, Action.SCALE_UP).status is ExecStatus.EXECUTED
    assert apps.patches == [("scale", {"spec": {"replicas": 3}})]


def test_scale_up_respects_max_replicas():
    apps = Apps(replicas=5)
    _, iid, r = setup(apps=apps, max_replicas=5)
    res = run(r, iid, Action.SCALE_UP)
    assert res.status is ExecStatus.FAILED and apps.patches == []


def test_rollback_applies_previous_template_without_pod_template_hash():
    apps = Apps()
    _, iid, r = setup(apps=apps)
    res = run(r, iid, Action.ROLLBACK)
    assert res.status is ExecStatus.EXECUTED and "revision 1" in res.detail
    kind, body = apps.patches[0]
    tpl = body["spec"]["template"]
    assert tpl["spec"]["image"] == "rs-old" and "pod-template-hash" not in tpl["metadata"]["labels"]


def test_rollback_with_no_earlier_revision_fails():
    apps = Apps(revisions=(("rs-new", 1),), current=1)
    _, iid, r = setup(apps=apps)
    assert run(r, iid, Action.ROLLBACK).status is ExecStatus.FAILED and apps.patches == []


def test_rollback_picks_most_recent_older_revision():
    apps = Apps(revisions=(("r1", 1), ("r2", 2), ("r3", 3)), current=3)
    _, iid, r = setup(apps=apps)
    assert "revision 2" in run(r, iid, Action.ROLLBACK).detail


# ---------------- verifier ----------------
def vpod(name="p", ready=True, waiting=None, restarts=0, last=None, deleting=False):
    cs = NS(ready=ready, restart_count=restarts, state=NS(waiting=NS(reason=waiting) if waiting else None),
            last_state=NS(terminated=NS(reason=last, exit_code=1) if last else None))
    return NS(metadata=NS(uid=name, namespace="demo", name=name, labels={"app": "x"},
                          deletion_timestamp="t" if deleting else None),
              status=NS(phase="Running", container_statuses=[cs]))


class Seq:
    """list_namespaced_pod returns successive scripted pod lists."""
    def __init__(self, *states):
        self.states, self.i = list(states), 0

    def list_namespaced_pod(self, ns, label_selector=None):
        s = self.states[min(self.i, len(self.states) - 1)]
        self.i += 1
        return NS(items=s)


def verifier(api, **kw):
    t = [0.0]
    sleeps = []
    def sleep(s):
        sleeps.append(s); t[0] += s
    return Verifier(api, sleep=sleep, clock=lambda: t[0], **kw), sleeps


def test_healthy_workload_resolves_after_stabilization():
    v, sleeps = verifier(Seq([vpod()]), stabilization_seconds=20)
    assert v.verify("demo", "app=x").healthy and sleeps[0] == 20


def test_requires_consecutive_healthy_checks():
    # healthy blip between crashes must not count as recovery
    api = Seq([vpod()], [vpod(ready=False, waiting="CrashLoopBackOff")], [vpod()], [vpod()])
    v, _ = verifier(api, consecutive_ok=2)
    assert v.verify("demo", "app=x").healthy and api.i == 4


def test_still_crashlooping_fails_at_timeout():
    v, _ = verifier(Seq([vpod(ready=False, waiting="CrashLoopBackOff")]), timeout_seconds=30, poll_seconds=5)
    r = v.verify("demo", "app=x")
    assert not r.healthy and "CRASH_LOOP" in r.detail


def test_no_pods_is_unhealthy():
    v, _ = verifier(Seq([]), timeout_seconds=10)
    assert not v.verify("demo", "app=x").healthy


def test_terminating_pods_are_ignored():
    v, _ = verifier(Seq([vpod("old", ready=False, waiting="CrashLoopBackOff", deleting=True), vpod("new")]))
    assert v.verify("demo", "app=x").healthy


def test_oom_killed_pod_is_unhealthy_even_if_ready():
    v, _ = verifier(Seq([vpod(last="OOMKilled", restarts=1)]), timeout_seconds=6, poll_seconds=3)
    assert not v.verify("demo", "app=x").healthy


def test_workload_selector_drops_per_replica_labels():
    assert workload_selector({"app": "x", "pod-template-hash": "abc", "tier": "web"}) == "app=x,tier=web"
