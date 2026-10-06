"""Pod CPU/memory from the Kubernetes Metrics API (metrics-server).

Best-effort by design: if metrics-server is missing, the agent still detects and
heals reactively; only the predictive rules go quiet.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from kubernetes.client.rest import ApiException
from kubernetes.utils import parse_quantity

log = logging.getLogger("metrics")


@dataclass(frozen=True)
class PodMetrics:
    memory_bytes: int
    cpu_millicores: int
    timestamp: datetime  # when metrics-server sampled the kubelet


class MetricsSource:
    def __init__(self, custom_api):
        self._api = custom_api
        self._warned = False

    def fetch(self, namespace: str) -> dict[str, PodMetrics]:
        try:
            items = self._api.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", namespace, "pods")["items"]
        except (ApiException, OSError) as e:
            if not self._warned:  # once, not every poll
                log.warning("metrics unavailable (%s); predictive rules disabled until it is", getattr(e, "status", e))
                self._warned = True
            return {}
        self._warned = False
        out: dict[str, PodMetrics] = {}
        for it in items:
            try:
                usage = [c["usage"] for c in it["containers"]]
                out[it["metadata"]["name"]] = PodMetrics(
                    memory_bytes=int(sum(parse_quantity(u["memory"]) for u in usage)),
                    cpu_millicores=int(sum(parse_quantity(u["cpu"]) for u in usage) * 1000),
                    timestamp=datetime.fromisoformat(it["timestamp"].replace("Z", "+00:00")))
            except (KeyError, ValueError, TypeError):
                log.warning("skipping malformed metrics item %s", it.get("metadata", {}).get("name"))
        return out
