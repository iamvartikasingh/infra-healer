"""Demo service for InfraHealer.

A deliberately breakable Flask app. /health and /api/payments are the "real"
surface; /crash and /leak exist only to produce CrashLoopBackOff and OOMKilled
on demand, and are disabled unless DEMO_ENDPOINTS_ENABLED=true.
"""
import logging
import os
import threading
import time
import uuid

from flask import Flask, jsonify, request

log = logging.getLogger("demo-service")

_leaked: list[bytes] = []  # intentionally never freed


def _hard_exit(code: int = 1, delay: float = 0.2) -> None:
    """Kill the whole process after `delay` so the HTTP response can flush."""
    threading.Timer(delay, lambda: os._exit(code)).start()


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DEMO_ENDPOINTS_ENABLED=os.getenv("DEMO_ENDPOINTS_ENABLED", "false").lower() == "true",
        # Marker lives on an emptyDir volume: it survives container restarts
        # (=> CrashLoopBackOff) but not pod deletion (=> RESTART_POD fixes it).
        CRASH_MARKER=os.getenv("CRASH_MARKER", "/state/crash.marker"),
        MAX_LEAK_MB=int(os.getenv("MAX_LEAK_MB", "512")),  # safety net for local runs
        EXIT_FN=_hard_exit,
    )
    if config:
        app.config.update(config)

    payments: dict[str, dict] = {}

    def demo_only(fn):
        def wrapper(*a, **kw):
            if not app.config["DEMO_ENDPOINTS_ENABLED"]:
                return jsonify(error="not found"), 404
            return fn(*a, **kw)
        wrapper.__name__ = fn.__name__
        return wrapper

    @app.get("/health")
    def health():
        return jsonify(status="ok", leaked_mb=sum(len(b) for b in _leaked) // 2**20)

    @app.get("/api/payments")
    def list_payments():
        return jsonify(list(payments.values()))

    @app.post("/api/payments")
    def create_payment():
        body = request.get_json(silent=True) or {}
        amount = body.get("amount")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
            return jsonify(error="amount must be a positive number"), 400
        pid = str(uuid.uuid4())
        payments[pid] = {"id": pid, "amount": amount,
                         "currency": body.get("currency", "USD"), "created_at": time.time()}
        return jsonify(payments[pid]), 201

    @app.post("/crash")
    @demo_only
    def crash():
        """?persist=true -> crash on every start until the pod is replaced
        (CrashLoopBackOff). Default -> crash once; kubelet restarts cleanly."""
        if request.args.get("persist", "false").lower() == "true":
            marker = app.config["CRASH_MARKER"]
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            open(marker, "w").close()
        log.error("crash requested; exiting")
        app.config["EXIT_FN"](1)
        return jsonify(status="crashing"), 202

    @app.post("/leak")
    @demo_only
    def leak():
        """Allocate and retain ?mb=N (default 10) MB until OOMKilled."""
        try:
            mb = int(request.args.get("mb", "10"))
        except ValueError:
            return jsonify(error="mb must be an integer"), 400
        total = sum(len(b) for b in _leaked) // 2**20
        if not 0 < mb <= 256 or total + mb > app.config["MAX_LEAK_MB"]:
            return jsonify(error="mb out of range", leaked_mb=total), 400
        _leaked.append(b"\x01" * (mb * 2**20))  # non-zero fill forces real pages
        log.warning("leaked %d MB (total %d MB)", mb, total + mb)
        return jsonify(leaked_mb=total + mb)

    return app


def main() -> None:
    from waitress import serve  # single process: if it dies, the container dies

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app()
    if app.config["DEMO_ENDPOINTS_ENABLED"] and os.path.exists(app.config["CRASH_MARKER"]):
        log.error("crash marker present at startup; exiting 1 (simulated crash loop)")
        raise SystemExit(1)
    serve(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
