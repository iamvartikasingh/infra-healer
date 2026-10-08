"""Maps approved actions to Kubernetes calls, at most once per incident.

The Remediator does not trust its caller: it re-checks its own whitelist, claims
the (incident, action) pair in SQLite BEFORE touching the cluster, and never raises.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from kubernetes.client.rest import ApiException
from kubernetes.client import V1DeleteOptions, V1Preconditions

from agent.diagnose.schema import Action
from agent.policy.engine import EXECUTABLE
from agent.state.store import Store

log = logging.getLogger("remediate")
REVISION = "deployment.kubernetes.io/revision"


class ExecStatus(str, Enum):
    EXECUTED = "EXECUTED"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RemediationResult:
    status: ExecStatus
    detail: str


class Remediator:
    def __init__(self, core_api, apps_api, store: Store, allowed_actions=EXECUTABLE, max_replicas: int = 5):
        self._core, self._apps, self._store = core_api, apps_api, store
        self._allowed = frozenset(allowed_actions)
        self._max_replicas = max_replicas

    def execute(self, incident_id: str, action: Action, namespace: str, pod: str, pod_uid: str,
                initiator: str) -> RemediationResult:
        if action not in EXECUTABLE or action not in self._allowed:
            return RemediationResult(ExecStatus.FAILED, f"{action} refused: not an allowed executable action")
        if not self._store.claim_execution(incident_id, action.value, initiator):
            log.warning("duplicate %s for incident %s suppressed", action.value, incident_id)
            return RemediationResult(ExecStatus.SKIPPED_DUPLICATE, f"{action.value} already ran for this incident")
        try:
            detail = {Action.RESTART_POD: self._restart_pod, Action.SCALE_UP: self._scale_up,
                      Action.ROLLBACK: self._rollback}[action](namespace, pod, pod_uid)
            res = RemediationResult(ExecStatus.EXECUTED, detail)
        except Exception as e:  # report, don't raise: the caller must always reach a recorded state
            log.exception("%s failed", action.value)
            res = RemediationResult(ExecStatus.FAILED, f"{type(e).__name__}: {e}")
        self._store.finish_execution(incident_id, action.value, res.status.value, res.detail)
        return res

    # --- actions ---------------------------------------------------------
    def _restart_pod(self, ns: str, name: str, uid: str) -> str:
        try:
            pod = self._core.read_namespaced_pod(name, ns)
        except ApiException as e:
            if e.status == 404:
                return f"pod {name} already gone; nothing to delete"
            raise
        if pod.metadata.uid != uid:  # never delete a replacement that merely reuses the name
            return f"pod {name} was already replaced (uid changed); nothing to delete"
        self._core.delete_namespaced_pod(name, ns, body=V1DeleteOptions(preconditions=V1Preconditions(uid=uid)))
        return f"deleted pod {name}; its controller will recreate it"

    def _owner_deployment(self, ns: str, name: str) -> str:
        pod = self._core.read_namespaced_pod(name, ns)
        rs_name = next((o.name for o in pod.metadata.owner_references or [] if o.kind == "ReplicaSet"), None)
        if not rs_name:
            raise RuntimeError(f"pod {name} is not owned by a ReplicaSet")
        rs = self._apps.read_namespaced_replica_set(rs_name, ns)
        dep = next((o.name for o in rs.metadata.owner_references or [] if o.kind == "Deployment"), None)
        if not dep:
            raise RuntimeError(f"ReplicaSet {rs_name} is not owned by a Deployment")
        return dep

    def _scale_up(self, ns: str, name: str, uid: str) -> str:
        dep = self._owner_deployment(ns, name)
        current = self._apps.read_namespaced_deployment(dep, ns).spec.replicas or 0
        target = min(current + 1, self._max_replicas)
        if target <= current:
            raise RuntimeError(f"deployment {dep} already at max replicas ({self._max_replicas})")
        self._apps.patch_namespaced_deployment_scale(dep, ns, {"spec": {"replicas": target}})
        return f"scaled deployment {dep} {current} -> {target}"

    def _rollback(self, ns: str, name: str, uid: str) -> str:
        dep_name = self._owner_deployment(ns, name)
        dep = self._apps.read_namespaced_deployment(dep_name, ns)
        selector = ",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
        sets = [r for r in self._apps.list_namespaced_replica_set(ns, label_selector=selector).items
                if any(o.uid == dep.metadata.uid for o in r.metadata.owner_references or [])]
        current = int(dep.metadata.annotations.get(REVISION, "0"))
        older = sorted((r for r in sets if int(r.metadata.annotations.get(REVISION, "0")) < current),
                       key=lambda r: int(r.metadata.annotations[REVISION]), reverse=True)
        if not older:
            raise RuntimeError(f"deployment {dep_name} has no earlier revision to roll back to")
        template = self._apps.api_client.sanitize_for_serialization(older[0].spec.template)
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        self._apps.patch_namespaced_deployment(dep_name, ns, {"spec": {"template": template}})
        return f"rolled deployment {dep_name} back to revision {older[0].metadata.annotations[REVISION]}"
