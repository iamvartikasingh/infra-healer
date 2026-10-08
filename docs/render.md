# Publish the interactive demo on Render

The public app is a simulation: no cluster credentials, real Kubernetes changes, or model API keys. It uses the project's diagnosis schema, policy engine, state machine, and dashboard with scripted evidence and recovery. Each browser has a signed session and isolated in-memory SQLite store. Recovery advances on dashboard polls.

## Deploy

1. Push the deployment changes to your GitHub repository, including `render.yaml`.
2. Sign in at https://dashboard.render.com/ and select **New → Blueprint**.
3. Connect GitHub and select `iamvartikasingh/infra-healer` and the branch containing these changes.
4. Review the `infra-healer-demo` service. Confirm its plan is **Free**, then deploy.
5. After the build succeeds, open the service's public URL. Trigger a crash, approve the proposal, and watch it resolve. Open another browser or private window to verify that histories are separate.

The blueprint supplies the build/start commands, Python version, generated session secret, and `/health` endpoint. Render supplies `PORT` and `RENDER_EXTERNAL_HOSTNAME`; the app permits that exact hostname alongside localhost. No cluster or Anthropic environment variables are needed.

If creating a Web Service manually instead, use:

| Setting | Value |
| --- | --- |
| Runtime | Python |
| Plan | Free |
| Build command | `pip install -r requirements-render.txt` |
| Start command | `gunicorn 'dashboard.demo:create_app()' --bind 0.0.0.0:$PORT --workers 1 --threads 4` |
| Health check | `/health` |
| Environment | `PYTHON_VERSION=3.12.8`, and a randomly generated `SECRET_KEY` |

## Local preview

With the existing virtual environment activated:

```bash
python -m flask --app 'dashboard.demo:create_app' run --port 8000
```

Open http://127.0.0.1:8000. The regular `python -m dashboard.app` remains the real agent dashboard.

## Demo limits

Use exactly one Gunicorn worker because session stores are in process memory. The demo retains at most 64 browser sessions with 20 incidents each; idle sessions expire after an hour, and the least recently used session is evicted at capacity. Reset clears only the current browser's demo. Server restarts clear every simulation.

Render's free service sleeps after 15 minutes without inbound traffic, and waking can take about a minute. Open the link before an interview to allow startup. Free services do not provide persistent disks; this demo intentionally requires none. See [Render's free service limits](https://render.com/docs/free).

This deployment does not offer real cluster connections. Keep the real agent/dashboard installation separate; it needs authenticated access and the reliability work described in [architecture notes](architecture.md).
