# Deploy the live application resilience lab on Render

The default Render entrypoint is now `dashboard.live:create_app()`. It runs actual HTTP experiments, rather than advancing scripted recovery states when the page is refreshed. The older simulation remains available through `dashboard.demo:create_app()`.

## What runs

Each experiment starts a disposable checkout HTTP process bound to loopback. A background thread sends actual checkout requests once a second and records response status, latency, and process identity. After five healthy baseline requests, it injects either HTTP 503 errors or a 350ms delay. Three consecutive breaches of the HTTP 200 / below 200ms hypothesis open an incident.

Diagnosis uses deterministic rules with knowledge of the controlled fault; no LLM is called. The only permitted action is restarting that visitor's checkout process after explicit human approval. The lab terminates the faulty process, starts a replacement, and requires three consecutive successful responses below 200ms before recording recovery. Rejection or timeout ends the experiment without claiming recovery.

This is a real **application resilience lab**. It does not execute Kubernetes remediation and does not target external websites. The Kubernetes agent remains a separate deployment, documented in [the real cluster walkthrough](real-demo.md).

A [validated Gunicorn run](evidence/live-checkout.json) contains 17 measured HTTP requests, actual HTTP 503 failures, two distinct worker process IDs, and verified recovery. See the [dashboard capture](evidence/live-checkout.png). These are recorded evidence of validation; the hosted lab starts a fresh experiment for each visitor.

## Deploy

1. Push these changes to GitHub, including `lab/`, `dashboard/live.py`, `dashboard/lab.js`, and `render.yaml`.
2. Sign in at https://dashboard.render.com/ and choose **New → Blueprint**.
3. Connect `iamvartikasingh/infra-healer` and select the branch containing these changes.
4. Review `infra-healer-demo`, confirm its plan is **Free**, and deploy.
5. Open the public service URL. Start an HTTP error experiment, wait for detection, approve the proposed worker restart, and watch successful requests return.

If the service already exists, deploy the new commit and verify its start command uses `dashboard.live`, rather than `dashboard.demo`.

| Setting | Value |
| --- | --- |
| Runtime | Python |
| Plan | Free |
| Build command | `pip install -r requirements-render.txt` |
| Start command | `gunicorn 'dashboard.live:create_app()' --bind 0.0.0.0:$PORT --workers 1 --threads 4` |
| Health check | `/health` |
| Environment | `PYTHON_VERSION=3.12.8`, generated `SECRET_KEY` |

Render provides `PORT` and `RENDER_EXTERNAL_HOSTNAME`. No Kubernetes credentials, payment credentials, or model API keys are required.

## How to demonstrate it

1. **Baseline:** point to five real HTTP 200 requests below 200ms.
2. **Injection:** show HTTP failures or increased latency after the recorded injection event.
3. **Detection:** inspect the actual request evidence and fixed threshold rule.
4. **Decision:** approve or reject the bounded restart action.
5. **Recovery:** show the changed worker PID and three measured healthy requests.

Detection and recovery timings come from the experiment's monotonic clock. Recovery time excludes human approval wait. The chart displays real observed request latency. The hypothesis success percentage counts requests that satisfy both status and latency requirements; it covers the retained experiment samples, not an external availability SLO. Manual checkout requests also contribute samples.

## Bounds and lifecycle

- One experiment per browser session; four concurrent experiments per server.
- Five baseline requests, at most 150 retained request samples, and a 120-second experiment budget.
- Only fixed loopback checkout targets; no arbitrary URL or command input.
- Errors and a 350ms delay are the only injectable faults; no memory exhaustion or CPU saturation.
- Workers terminate at the end of an experiment. A child-process watchdog exits after 130 seconds even if the parent disappears.
- At most 32 visitor sessions and 20 incidents per session. Inactive sessions with no running experiment expire after 15 minutes.
- Exactly one Gunicorn worker because session stores and experiment ownership are in memory.
- Server restart loses experiment history. No persistent storage or real payments are involved.

Render's free service can sleep when idle and may restart. A sleeping service has a cold-start delay. Review [Render's free service limits](https://render.com/docs/free). Successful local integration tests do not establish that the hosted service has deployed successfully; verify the actual public URL after publishing.

## Local developer verification

Local execution is only for development; visitors use the hosted app after deployment:

```bash
python -m flask --app 'dashboard.live:create_app' run --port 8002
python -m pytest tests/test_live_lab.py -q
```

The live lab tests spawn actual subprocesses and use loopback HTTP sockets. They need an environment that permits those operations.
