# Demo guide

## Before the interview

Complete the README setup and run `make test`. Use the fake provider for a repeatable demonstration without credentials or network calls to a model. Explain that its diagnosis is scripted; the orchestration, policy, Kubernetes action, and verification still run.

`make demo-up` creates a kind cluster named `infra-healer`, builds and loads the workload image, and deploys it in namespace `demo`. The manifest fixes that namespace; keep the default for setup. Agent and dashboard commands should run from the repository root in terminals with the virtual environment activated.

The Makefile uses your current kubectl context. Check it before injecting faults:

```bash
kubectl config current-context
kubectl -n demo get pods
```

The expected context is `kind-infra-healer`.

## Main walkthrough: human-approved recovery

1. Start the agent in terminal 1:

   ```bash
   make agent-run MODE=HUMAN_APPROVAL PROVIDER=fake
   ```

2. Start the dashboard in terminal 2:

   ```bash
   make dashboard
   ```

   Open http://127.0.0.1:8000. Agent and dashboard share `infra-healer.db` by default.

3. Inject a crash in terminal 3:

   ```bash
   make crash
   make watch
   ```

4. Wait for `CRASH_LOOP` to appear. Explain the evidence, proposed `RESTART_POD`, confidence, and `APPROVAL_REQUIRED` policy decision. The container may restart several times before Kubernetes reports CrashLoopBackOff.

5. Approve the action. Show the timeline reaching `REMEDIATING`, `VERIFYING`, and `RESOLVED`. Recovery is not instantaneous: verification includes a stabilization wait and repeated checks.

6. Explain why this particular restart works: the marker survives container restarts but is removed when the pod and its `emptyDir` are replaced. A restart is not a general fix for bad images or configuration.

To demonstrate rejection, inject another crash and reject its proposal. The incident escalates and the agent does not perform the proposed action. Recover the demo manually afterward:

```bash
kubectl -n demo rollout restart deployment/demo-service
kubectl -n demo rollout status deployment/demo-service --timeout=90s
```

## Optional: autonomous recovery

Stop the running agent with Ctrl-C before starting another mode:

```bash
make agent-run MODE=AUTONOMOUS PROVIDER=fake
```

Inject another crash. The fake crash diagnosis has confidence 0.85, exceeding the default 0.8 threshold. The default autonomy check allows three claimed attempts per hour; later attempts require approval. This check is not a strict global cap across concurrent workers.

## Optional: prediction before OOM

Install metrics-server in the local kind cluster:

```bash
make metrics
kubectl -n demo top pods
```

This target downloads the metrics-server release manifest and enables insecure kubelet TLS for the local demo. Wait until `top pods` returns readings, then start a fresh workload and allow metrics to settle:

```bash
kubectl -n demo rollout restart deployment/demo-service
kubectl -n demo rollout status deployment/demo-service --timeout=90s
make leak-slow
```

Watch for `PREDICTED_OOM`. The rule needs five distinct metrics samples across at least 40 seconds, and timing depends on metrics-server sampling and available memory. Predictions remain subject to human approval by default, including in autonomous mode. Stop the leak loop with Ctrl-C before approving a replacement so it does not keep leaking into the new pod.

Treat this as an optional demonstration: OOM can occur before sufficient evidence accumulates. The deterministic predictive tests are the reliable way to show threshold behavior, noise rejection, and sample deduplication.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Dashboard fails to import Flask | Activate `.venv` and install root `requirements.txt`. |
| No incidents appear | Check agent logs, current context, namespace, and whether dashboard and agent use the same DB path. |
| Approval has no effect | Ensure the agent is still running; the dashboard only records the decision. |
| No memory predictions | Check `kubectl -n demo top pods`; allow enough distinct samples to accumulate. |
| A new fault is ignored | Check for an existing open incident on that pod. Resolve or reject pending work; interrupted incidents currently need manual reconciliation. |
| `make crash` loses its connection | The endpoint kills the process; check pod status and agent logs to confirm fault injection. |

Stop the agent and dashboard with Ctrl-C. `make clean` deletes the local kind cluster; the SQLite incident history remains on disk.

## Interview talking points

- **Problem:** reduce the gap between detecting an infrastructure symptom and executing an explainable recovery action.
- **Key decision:** use the model to recommend while deterministic code owns authorization and execution.
- **Reliability:** show the failed-verification path, duplicate-action claims, and universal escalation transition.
- **Prediction:** explain R², evidence requirements, and why trend confidence is not a probability of failure.
- **Tradeoff:** SQLite and polling simplify a local prototype; production needs atomic budgets, reconciliation, controller identity, and integration validation.
- **Evidence:** walk through `tests/test_predictive_rules.py` and the policy invariant sweep, then distinguish mocked tests from live integration coverage.

Describe measured behavior and implemented guarantees. Avoid claiming that every restart fixes a failure or that a passing readiness probe proves full application recovery.
