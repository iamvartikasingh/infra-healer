"""Bounded fault injection into the local kind demo; stop at prediction or replacement."""
import argparse
import json
import subprocess
import time
from pathlib import Path

from agent.state.store import Store


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="real-demo.db")
    p.add_argument("--cluster", default="infra-healer")
    a = p.parse_args()
    if not Path(a.db).is_file():
        p.error("start the agent with this DB before injecting a leak")
    base = ["kubectl", "--context", "kind-" + a.cluster, "-n", "demo"]

    def pods():
        return json.loads(subprocess.check_output(base + ["get", "pods", "-l", "app=demo-service", "-o", "json"]))["items"]

    running = [pod for pod in pods() if not pod["metadata"].get("deletionTimestamp")]
    if len(running) != 1:
        p.error("expected exactly one demo-service pod")
    pod = running[0]
    uid, name = pod["metadata"]["uid"], pod["metadata"]["name"]
    st = Store(a.db)
    print(f"Injecting up to 24 MiB into {name} on kind-{a.cluster}; stops when a prediction opens.", flush=True)
    for _ in range(24):
        if not any(pod["metadata"]["uid"] == uid for pod in pods()):
            print("Pod replaced; stopping injection.", flush=True)
            return
        inc = st.open_for_pod(uid)
        if inc:
            print(f"Incident {inc['id']} ({inc['finding_kind']}) opened. Injection stopped; review it in the dashboard.", flush=True)
            return
        subprocess.run(base + ["exec", name, "--", "python", "-c",
            "import urllib.request as u; print(u.urlopen(u.Request('http://localhost:8080/leak?mb=1', method='POST')).read().decode())"], check=True)
        time.sleep(6)
    print("Injection budget reached. Review telemetry; no more memory will be allocated by this script.", flush=True)


if __name__ == "__main__":
    main()
