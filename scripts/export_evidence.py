"""Export a local incident for review. Inspect logs before publishing the artifact."""
import argparse
import json
from pathlib import Path

from agent.state.store import Store


def main():
    p = argparse.ArgumentParser()
    p.add_argument("id")
    p.add_argument("--db", default="real-demo.db")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    if not Path(a.db).is_file():
        p.error("database does not exist")
    st = Store(a.db)
    inc = st.get(a.id)
    samples = st.observations(inc["namespace"], inc["selector"], limit=1000)
    unique = {}
    for s in samples:
        if s["metrics_timestamp"] and s["memory_bytes"] is not None:
            unique[(s["uid"], s["restart_count"], s["metrics_timestamp"])] = s
    for key in ("context_json", "diagnosis_json", "policy_json"):
        inc[key.removesuffix("_json")] = json.loads(inc.pop(key) or "null")
    output = {"source": "Local agent database; inspect provider choice separately", "incident": inc,
              "timeline": st.events(a.id), "observations": list(unique.values())}
    Path(a.out).write_text(json.dumps(output, indent=2) + "\n")
    print(a.out)


if __name__ == "__main__":
    main()
