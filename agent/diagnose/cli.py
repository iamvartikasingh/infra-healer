"""    python -m agent.diagnose.cli capture --namespace demo --out tests/fixtures/incidents/x.json
    python -m agent.diagnose.cli diagnose tests/fixtures/incidents/x.json [--provider fake|anthropic]
"""
import argparse
import json
import sys
import time

from agent.diagnose.context import IncidentContext
from agent.diagnose.diagnoser import Diagnoser
from agent.diagnose.fake import FakeProvider


def capture(a) -> int:
    from kubernetes import client

    from agent.monitor.watcher import PodWatcher, _load_kube, snapshot_from_pod
    _load_kube()
    api = client.CoreV1Api()
    w = PodWatcher(api, a.namespace)
    deadline = time.time() + a.wait
    while True:
        findings = w.poll_once()
        if findings or time.time() > deadline:
            break
        time.sleep(2)
    if not findings:
        print("no findings within wait window", file=sys.stderr)
        return 1
    # Prefer the most severe / first finding for the incident.
    f = next((x for x in findings if x.severity.value == "CRITICAL"), findings[0])
    pod = next(p for p in api.list_namespaced_pod(a.namespace).items if p.metadata.name == f.pod)
    from agent.diagnose.context import collect_context
    ctx = collect_context(api, f, snapshot_from_pod(pod), w.window.history(f.uid))
    open(a.out, "w").write(ctx.to_json())
    print(f"captured {f.kind.value} for {f.pod} -> {a.out}")
    return 0


def diagnose(a) -> int:
    ctx = IncidentContext.model_validate_json(open(a.fixture).read())
    if a.provider == "ollama":
        from agent.diagnose.provider import OllamaProvider
        provider = OllamaProvider(model=a.model)
    elif a.provider == "anthropic":
        from agent.diagnose.provider import AnthropicProvider
        provider = AnthropicProvider(model=a.model, use_fallbacks=not a.no_fallbacks)
    else:
        provider = FakeProvider()
    r = Diagnoser(provider).diagnose(ctx)
    if r.ok:
        print(r.diagnosis.model_dump_json(by_alias=True, indent=2))
        return 0
    print(f"DIAGNOSIS FAILED after {r.attempts} attempt(s): {r.error}", file=sys.stderr)
    return 2


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--namespace", default="demo")
    c.add_argument("--out", required=True)
    c.add_argument("--wait", type=float, default=90)
    c.set_defaults(fn=capture)
    d = sub.add_parser("diagnose")
    d.add_argument("fixture")
    d.add_argument("--provider", choices=["fake", "ollama", "anthropic"], default="fake")
    d.add_argument("--model")
    d.add_argument("--no-fallbacks", action="store_true")
    d.set_defaults(fn=diagnose)
    a = p.parse_args()
    raise SystemExit(a.fn(a))


if __name__ == "__main__":
    main()
