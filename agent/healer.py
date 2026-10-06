"""Closes the loop: detect -> diagnose -> policy gate -> act -> verify.

Every step records its state in SQLite first, and every failure path ends in
ESCALATED. The loop never lets an exception leave an incident half-handled.
"""
from __future__ import annotations

import logging

from agent.detect.reactive_rules import PREDICTIVE_KINDS, Finding, FindingKind
from agent.diagnose.context import collect_context
from agent.diagnose.diagnoser import Diagnoser
from agent.diagnose.schema import Action, Diagnosis
from agent.monitor.models import PodSnapshot
from agent.policy.engine import Mode, Outcome, PolicyConfig, PolicyContext, PolicyDecision, evaluate
from agent.remediate.executor import ExecStatus, Remediator
from agent.state.machine import State
from agent.state.store import StaleState, Store
from agent.verify.verifier import Verifier, workload_selector

log = logging.getLogger("healer")


class Healer:
    def __init__(self, core_api, store: Store, diagnoser: Diagnoser, config: PolicyConfig,
                 remediator: Remediator, verifier: Verifier):
        self._api, self.store, self._diagnoser = core_api, store, diagnoser
        self._config, self._remediator, self._verifier = config, remediator, verifier

    # --- entry points ----------------------------------------------------
    def open_incident(self, finding: Finding, snapshot: PodSnapshot) -> str | None:
        iid = self.store.create_incident(snapshot.uid, snapshot.namespace, snapshot.name,
                                         workload_selector(snapshot.labels), finding.kind.value, finding.detail)
        if iid is None:
            iid = self._maybe_supersede(finding, snapshot)
        if iid is None:
            log.info("pod %s already has an open incident; ignoring %s", snapshot.name, finding.kind.value)
        return iid

    def _maybe_supersede(self, finding: Finding, snapshot: PodSnapshot) -> str | None:
        """A real failure outranks a prediction that is still only awaiting a human: close the
        prediction and open a reactive incident, so the failure isn't ignored behind a stale proposal."""
        old = self.store.open_for_pod(snapshot.uid)
        if (old is None or finding.predictive or FindingKind(old["finding_kind"]) not in PREDICTIVE_KINDS
                or old["state"] != State.ACTION_PROPOSED.value):
            return None
        try:
            self.store.transition(old["id"], State.ESCALATED, f"superseded: {finding.kind.value} actually occurred", approval="NONE")
        except StaleState:
            return None
        return self.store.create_incident(snapshot.uid, snapshot.namespace, snapshot.name,
                                          workload_selector(snapshot.labels), finding.kind.value, finding.detail)

    def run_incident(self, iid: str, finding: Finding, snapshot: PodSnapshot, history=None) -> None:
        """DETECTED -> ... Safe to call from a worker thread; never raises."""
        try:
            self.store.transition(iid, State.DIAGNOSING, "collecting context and asking the model")
            ctx = collect_context(self._api, finding, snapshot, history)
            result = self._diagnoser.diagnose(ctx)
            diagnosis = result.diagnosis
            decision = evaluate(diagnosis, self._config, self._policy_ctx(iid, predictive=finding.predictive))
            fields = {"policy_json": _dumps(decision.to_json())}
            if diagnosis:
                fields["diagnosis_json"] = diagnosis.model_dump_json(by_alias=True)
            self._after_decision(iid, decision, fields, error=result.error)
        except StaleState:
            log.info("incident %s changed concurrently; skipping", iid)
        except Exception as e:
            self._fail_safe(iid, f"unexpected error: {type(e).__name__}: {e}")

    def process_approvals(self) -> None:
        """Pick up human decisions recorded in the DB (by the CLI or, later, the dashboard)."""
        for inc in self.store.decided_approvals():
            iid = inc["id"]
            try:
                if inc["approval"] == "REJECTED":
                    self.store.transition(iid, State.ESCALATED, "human rejected the proposed action")
                    continue
                diagnosis = Diagnosis.model_validate_json(inc["diagnosis_json"])
                # Approval is re-checked against policy: it can never bypass the whitelist,
                # observe-only mode, or the already-attempted guard.
                decision = evaluate(diagnosis, self._config, self._policy_ctx(iid, human_approved=True,
                                                                         predictive=FindingKind(inc["finding_kind"]) in PREDICTIVE_KINDS))
                if decision.outcome is not Outcome.EXECUTE:
                    self.store.transition(iid, State.ESCALATED, f"approved but policy denies: {decision.rule}")
                    continue
                self._execute_and_verify(iid, diagnosis.recommended_action, inc, "human")
            except StaleState:
                continue  # another worker took it
            except Exception as e:
                self._fail_safe(iid, f"unexpected error: {type(e).__name__}: {e}")

    # --- internals -------------------------------------------------------
    def _policy_ctx(self, iid: str, human_approved: bool = False, predictive: bool = False) -> PolicyContext:
        return PolicyContext(
            attempted_actions=frozenset(Action(a) for a in self.store.attempted_actions(iid)),
            autonomous_in_window=self.store.autonomous_count_since(self._config.window_seconds),
            human_approved=human_approved, predictive=predictive)

    def _after_decision(self, iid: str, d: PolicyDecision, fields: dict, error: str | None) -> None:
        note = f"policy {d.outcome.value} [{d.rule}]: {d.reason}"
        inc = self.store.get(iid)
        if d.outcome is Outcome.DENY:
            if d.action is None:  # no usable diagnosis: nothing was proposed
                self.store.transition(iid, State.ESCALATED, f"{error or 'no usable diagnosis'}; {note}", **fields)
                return
            # A real proposal that policy refused: record it for the timeline, then hand to a human.
            self.store.transition(iid, State.ACTION_PROPOSED, f"model proposed {d.action.value} ({d.confidence:.2f})",
                                  approval="NONE", **fields)
            self.store.transition(iid, State.ESCALATED, note)
            return
        needs_human = d.outcome is Outcome.REQUIRE_APPROVAL
        self.store.transition(iid, State.ACTION_PROPOSED,
                              f"model proposed {d.action.value} ({d.confidence:.2f}); {note}",
                              approval="PENDING" if needs_human else "NONE", **fields)
        if needs_human:
            log.info("incident %s awaiting human approval: %s", iid, d.action.value)
            return
        self._execute_and_verify(iid, d.action, inc, "autonomous")

    def _execute_and_verify(self, iid: str, action: Action, inc: dict, initiator: str) -> None:
        self.store.transition(iid, State.REMEDIATING, f"{initiator} executing {action.value}")
        res = self._remediator.execute(iid, action, inc["namespace"], inc["pod"], inc["pod_uid"], initiator)
        self.store.add_note(iid, f"remediation {res.status.value}: {res.detail}")
        if res.status is not ExecStatus.EXECUTED:
            self.store.transition(iid, State.REMEDIATION_FAILED, res.detail)
            self.store.transition(iid, State.ESCALATED, "remediation did not run; needs a human")
            return
        self.store.transition(iid, State.VERIFYING, "waiting for stabilization, then rechecking health")
        v = self._verifier.verify(inc["namespace"], inc["selector"])
        if v.healthy:
            self.store.transition(iid, State.RESOLVED, v.detail)
        else:
            self.store.transition(iid, State.REMEDIATION_FAILED, v.detail)
            self.store.transition(iid, State.ESCALATED, "verification failed; needs a human")

    def _fail_safe(self, iid: str, why: str) -> None:
        log.exception("incident %s: %s", iid, why)
        try:
            self.store.transition(iid, State.ESCALATED, why)
        except Exception:
            log.exception("could not escalate incident %s", iid)


def _dumps(d: dict) -> str:
    import json
    return json.dumps(d)
