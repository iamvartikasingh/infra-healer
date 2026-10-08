"""    python -m agent.main run --mode AUTONOMOUS --provider fake
    python -m agent.main list
    python -m agent.main approve <id> | reject <id>
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading
from concurrent.futures import ThreadPoolExecutor

from kubernetes import client, config as kube_config

from agent.detect.reactive_rules import Finding
from agent.diagnose.diagnoser import Diagnoser
from agent.diagnose.fake import FakeProvider
from agent.diagnose.schema import Action
from agent.healer import Healer
from agent.monitor.metrics import MetricsSource
from agent.monitor.watcher import PodWatcher, _load_kube, snapshot_from_pod
from agent.policy.engine import Mode, PolicyConfig
from agent.remediate.executor import Remediator
from agent.state.store import Store
from agent.verify.verifier import Verifier, workload_selector


def _provider(name: str, model: str | None):
    if name == "ollama":
        from agent.diagnose.provider import OllamaProvider
        return OllamaProvider(model=model)
    if name == "anthropic":
        from agent.diagnose.provider import AnthropicProvider
        return AnthropicProvider(model=model)
    return FakeProvider()


def run(a) -> None:
    if a.context:
        kube_config.load_kube_config(context=a.context)
    else:
        _load_kube()
    core, apps = client.CoreV1Api(), client.AppsV1Api()
    store = Store(a.db)
    allowed = frozenset(Action(x) for x in a.allow.split(","))
    config = PolicyConfig(mode=Mode(a.mode), confidence_threshold=a.threshold, allowed_actions=allowed,
                          allow_autonomous_predictive=a.autonomous_predictive)
    healer = Healer(core, store, Diagnoser(_provider(a.provider, a.model)), config,
                    Remediator(core, apps, store, allowed_actions=allowed),
                    Verifier(core, stabilization_seconds=a.stabilization, timeout_seconds=a.verify_timeout))
    watcher = PodWatcher(core, a.namespace, label_selector=a.selector, metrics=MetricsSource(client.CustomObjectsApi()),
                         on_snapshot=lambda s: store.record_snapshot(s, workload_selector(s.labels)))
    pool, stop = ThreadPoolExecutor(max_workers=4), threading.Event()
    logging.info("InfraHealer running: mode=%s threshold=%.2f allowed=%s", config.mode.value,
                 config.confidence_threshold, sorted(x.value for x in allowed))

    def on_finding(f: Finding) -> None:
        pod = next((p for p in core.list_namespaced_pod(a.namespace).items if p.metadata.uid == f.uid), None)
        if pod is None:
            return
        history = watcher.window.history(f.uid)
        snap = history[-1] if history else snapshot_from_pod(pod)
        iid = healer.open_incident(f, snap)
        if iid:
            logging.info("incident %s opened: %s on %s", iid, f.kind.value, f.pod)
            pool.submit(healer.run_incident, iid, f, snap, watcher.window.history(f.uid))

    def approvals_loop() -> None:
        while not stop.wait(2):
            healer.process_approvals()

    threading.Thread(target=approvals_loop, daemon=True).start()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    watcher.run(on_finding, a.interval, stop)
    pool.shutdown(wait=False)


def main() -> None:
    p = argparse.ArgumentParser(prog="infra-healer")
    p.add_argument("--db", default="infra-healer.db")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--namespace", default="demo")
    r.add_argument("--context", help="explicit kubeconfig context")
    r.add_argument("--selector", help="limit watched pods to this label selector")
    r.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.HUMAN_APPROVAL.value)
    r.add_argument("--threshold", type=float, default=0.8)
    r.add_argument("--allow", default="RESTART_POD", help="comma list of whitelisted actions")
    r.add_argument("--autonomous-predictive", action="store_true",
                   help="let AUTONOMOUS mode act on predictions without human approval (off by default)")
    r.add_argument("--provider", choices=["fake", "ollama", "anthropic"], default="fake")
    r.add_argument("--model")
    r.add_argument("--interval", type=float, default=3.0)
    r.add_argument("--stabilization", type=float, default=15.0)
    r.add_argument("--verify-timeout", type=float, default=90.0)
    sub.add_parser("list")
    for name in ("approve", "reject"):
        sub.add_parser(name).add_argument("id")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if a.cmd == "run":
        run(a)
        return
    store = Store(a.db)
    if a.cmd == "list":
        for i in store.list_incidents():
            print(f"{i['id']}  {i['state']:<18} approval={i['approval']:<9} {i['finding_kind']:<17} {i['pod']}")
    else:
        ok = store.decide_approval(a.id, a.cmd == "approve")
        print(f"{a.cmd}d {a.id}" if ok else f"{a.id}: nothing awaiting a decision")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
