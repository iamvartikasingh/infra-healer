"""The whole loop with fakes: detect -> diagnose -> gate -> act -> verify."""
import json
from types import SimpleNamespace as NS

import pytest

from agent.detect.reactive_rules import evaluate as detect
from agent.diagnose.diagnoser import Diagnoser
from agent.diagnose.provider import ProviderError
from agent.healer import Healer
from agent.policy.engine import Mode, PolicyConfig
from agent.remediate.executor import Remediator
from agent.state.store import Store
from agent.verify.verifier import VerifyResult
from tests.helpers import snap
from tests.test_diagnoser import Scripted
from tests.test_diagnose_context import Api as ContextApi


def reply(action="RESTART_POD", conf=0.9):
    return json.dumps({"rootCause": "marker file", "confidence": conf, "recommendedAction": action, "reasoning": "logs"})


class K8s(ContextApi):
    """Context API + the pod/deployment calls the Remediator makes."""
    def __init__(self):
        super().__init__(log=b"ERROR crash marker present\n")
        self.deleted = []

    def read_namespaced_pod(self, name, ns):
        return NS(metadata=NS(uid="uid-1", name=name, owner_references=[]))

    def delete_namespaced_pod(self, name, ns, body=None):
        self.deleted.append(name)


class FakeVerifier:
    def __init__(self, healthy=True):
        self.healthy, self.calls = healthy, 0

    def verify(self, ns, selector):
        self.calls += 1
        return VerifyResult(self.healthy, "ok" if self.healthy else "still crashlooping")


def build(replies, mode=Mode.AUTONOMOUS, healthy=True, **cfgkw):
    api, store = K8s(), Store()
    cfg = PolicyConfig(mode=mode, **cfgkw)
    healer = Healer(api, store, Diagnoser(Scripted(*replies)), cfg,
                    Remediator(api, None, store, allowed_actions=cfg.allowed_actions), FakeVerifier(healthy))
    s = snap(uid="uid-1", name="pod-1", waiting_reason="CrashLoopBackOff", restart_count=2, labels={"app": "x"})
    return healer, api, store, detect(s)[0], s


def go(healer, finding, s):
    iid = healer.open_incident(finding, s)
    healer.run_incident(iid, finding, s)
    return iid


def state(store, iid):
    return store.get(iid)["state"]


def test_autonomous_confident_restart_resolves():
    h, api, st, f, s = build([reply(conf=0.95)])
    iid = go(h, f, s)
    assert state(st, iid) == "RESOLVED" and api.deleted == ["pod-1"]
    assert [e["to_state"] for e in st.events(iid) if e["to_state"]] == [
        "DETECTED", "DIAGNOSING", "ACTION_PROPOSED", "REMEDIATING", "VERIFYING", "RESOLVED"]
    assert json.loads(st.get(iid)["policy_json"])["rule"] == "AUTONOMOUS_OK"


def test_low_confidence_waits_for_human_then_approval_resolves():
    h, api, st, f, s = build([reply(conf=0.4)])
    iid = go(h, f, s)
    inc = st.get(iid)
    assert inc["state"] == "ACTION_PROPOSED" and inc["approval"] == "PENDING" and api.deleted == []
    h.process_approvals()
    assert api.deleted == [] and state(st, iid) == "ACTION_PROPOSED"  # nothing happens without a decision
    st.decide_approval(iid, True)
    h.process_approvals()
    assert state(st, iid) == "RESOLVED" and api.deleted == ["pod-1"]
    h.process_approvals()
    assert api.deleted == ["pod-1"]  # processing again never repeats the action


def test_rejection_escalates_without_acting():
    h, api, st, f, s = build([reply(conf=0.4)])
    iid = go(h, f, s)
    st.decide_approval(iid, False)
    h.process_approvals()
    assert state(st, iid) == "ESCALATED" and api.deleted == []


def test_human_approval_mode_always_waits_even_at_full_confidence():
    h, api, st, f, s = build([reply(conf=1.0)], mode=Mode.HUMAN_APPROVAL)
    iid = go(h, f, s)
    assert state(st, iid) == "ACTION_PROPOSED" and api.deleted == []


def test_observe_only_escalates_and_never_acts_even_if_someone_approves():
    h, api, st, f, s = build([reply(conf=1.0)], mode=Mode.OBSERVE_ONLY)
    iid = go(h, f, s)
    assert state(st, iid) == "ESCALATED" and api.deleted == []
    assert not st.decide_approval(iid, True)  # nothing to approve


def test_non_whitelisted_proposal_escalates_without_acting():
    h, api, st, f, s = build([reply("ROLLBACK", 1.0)])
    iid = go(h, f, s)
    assert state(st, iid) == "ESCALATED" and api.deleted == []
    assert json.loads(st.get(iid)["policy_json"])["rule"] == "NOT_WHITELISTED"
    assert st.get(iid)["diagnosis_json"]  # the proposal is still on record


def test_advisory_escalate_recommendation_goes_to_human():
    h, api, st, f, s = build([reply("ESCALATE_TO_HUMAN", 1.0)])
    iid = go(h, f, s)
    assert state(st, iid) == "ESCALATED" and api.deleted == []


def test_invalid_model_output_fails_closed():
    h, api, st, f, s = build(["garbage", '{"recommendedAction":"DELETE_EVERYTHING"}'])
    iid = go(h, f, s)
    assert state(st, iid) == "ESCALATED" and api.deleted == []
    assert st.get(iid)["diagnosis_json"] is None


def test_provider_outage_fails_closed():
    h, api, st, f, s = build([ProviderError("down")])
    iid = go(h, f, s)
    assert state(st, iid) == "ESCALATED" and api.deleted == []


def test_prompt_injected_logs_cannot_widen_what_runs():
    # Even if hostile log text fully hijacks the model, the reply is just an enum + number.
    h, api, st, f, s = build([reply("kubectl delete ns prod", 1.0)] * 2)
    iid = go(h, f, s)
    assert state(st, iid) == "ESCALATED" and api.deleted == []


def test_failed_verification_escalates_via_remediation_failed():
    h, api, st, f, s = build([reply(conf=0.95)], healthy=False)
    iid = go(h, f, s)
    path = [e["to_state"] for e in st.events(iid) if e["to_state"]]
    assert state(st, iid) == "ESCALATED" and "REMEDIATION_FAILED" in path and "RESOLVED" not in path


def test_remediation_error_escalates():
    h, api, st, f, s = build([reply(conf=0.95)])
    api.delete_namespaced_pod = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("api down"))
    iid = go(h, f, s)
    assert state(st, iid) == "ESCALATED" and h._verifier.calls == 0  # no verify after a failed action


def test_unexpected_exception_still_ends_in_a_recorded_state():
    h, api, st, f, s = build([reply()])
    h._diagnoser = NS(diagnose=lambda ctx: (_ for _ in ()).throw(RuntimeError("bug")))
    iid = go(h, f, s)
    assert state(st, iid) == "ESCALATED"


def test_duplicate_findings_for_one_pod_open_one_incident():
    h, api, st, f, s = build([reply(conf=0.4)])
    assert h.open_incident(f, s) is not None
    assert h.open_incident(f, s) is None


def test_approved_action_already_attempted_is_not_repeated():
    h, api, st, f, s = build([reply(conf=0.4)])
    iid = go(h, f, s)
    st.claim_execution(iid, "RESTART_POD", "autonomous")  # something already ran it
    st.decide_approval(iid, True)
    h.process_approvals()
    assert api.deleted == [] and state(st, iid) == "ESCALATED"


def test_rate_limit_stops_runaway_autonomy():
    h, api, st, f, s = build([reply(conf=0.95)], max_autonomous_per_window=1)
    first = go(h, f, s)
    assert state(st, first) == "RESOLVED"
    s2 = snap(uid="uid-2", name="pod-2", waiting_reason="CrashLoopBackOff", restart_count=2, labels={"app": "x"})
    h._diagnoser = Diagnoser(Scripted(reply(conf=0.95)))
    second = go(h, detect(s2)[0], s2)
    assert state(st, second) == "ACTION_PROPOSED" and st.get(second)["approval"] == "PENDING"


# ---------------- predictive incidents ----------------
from agent.detect.reactive_rules import Finding, FindingKind, Severity  # noqa: E402


def predicted(s, conf=0.9):
    return Finding(FindingKind.PREDICTED_OOM, Severity.WARNING, s.namespace, s.name, s.uid,
                   "memory rising; limit in ~80s", s.observed_at, f"{s.uid}:PREDICTED_OOM", confidence=conf)


def test_autonomous_mode_still_asks_a_human_about_a_prediction():
    h, api, st, f, s = build([reply(conf=0.99)])
    iid = go(h, predicted(s), s)
    inc = st.get(iid)
    assert inc["state"] == "ACTION_PROPOSED" and inc["approval"] == "PENDING" and api.deleted == []
    assert json.loads(inc["policy_json"])["rule"] == "PREDICTIVE_NEEDS_HUMAN"


def test_approved_prediction_acts():
    h, api, st, f, s = build([reply(conf=0.99)])
    iid = go(h, predicted(s), s)
    st.decide_approval(iid, True)
    h.process_approvals()
    assert st.get(iid)["state"] == "RESOLVED" and api.deleted == ["pod-1"]


def test_predictive_autonomy_opt_in():
    h, api, st, f, s = build([reply(conf=0.99)], allow_autonomous_predictive=True)
    iid = go(h, predicted(s), s)
    assert st.get(iid)["state"] == "RESOLVED" and api.deleted == ["pod-1"]


def test_real_failure_supersedes_a_pending_prediction():
    h, api, st, f, s = build([reply(conf=0.99), reply(conf=0.95)])
    first = go(h, predicted(s), s)
    assert st.get(first)["state"] == "ACTION_PROPOSED"
    second = go(h, f, s)  # the pod actually fails
    assert st.get(first)["state"] == "ESCALATED" and "superseded" in st.events(first)[-1]["note"]
    assert st.get(first)["approval"] == "NONE"  # no dangling approval request on a closed incident
    assert second and second != first and st.get(second)["state"] == "RESOLVED"


def test_prediction_does_not_supersede_a_reactive_incident():
    h, api, st, f, s = build([reply(conf=0.4)])
    first = go(h, f, s)
    assert h.open_incident(predicted(s), s) is None
    assert st.get(first)["state"] == "ACTION_PROPOSED"


def test_reactive_does_not_disturb_an_incident_that_is_already_remediating():
    h, api, st, f, s = build([reply(conf=0.4)])
    first = go(h, predicted(s), s)
    st.decide_approval(first, True)
    st.transition(first, __import__("agent.state.machine", fromlist=["State"]).State.REMEDIATING)
    assert h.open_incident(f, s) is None  # leave in-flight work alone
