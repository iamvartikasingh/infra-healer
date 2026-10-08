"""Render entrypoint for the real, isolated application resilience lab."""
import atexit
import os
import secrets
import threading
import time
from collections import OrderedDict

from flask import abort, jsonify, request, session
from werkzeug.local import LocalProxy

from agent.state.store import Store
from dashboard.app import DEFAULT_HOSTS, create_app as dashboard_app
from lab.runtime import Experiment


class Visitor:
    def __init__(self):
        self.store = Store()
        self.experiment = None
        self.last_seen = time.monotonic()


def create_app():
    visitors = OrderedDict()
    lock = threading.RLock()
    slots = threading.BoundedSemaphore(4)

    def visitor():
        sid = session.get('lab_id')
        if not sid:
            sid = session['lab_id'] = secrets.token_hex(24)
        with lock:
            for key in list(visitors):
                v = visitors[key]
                if time.monotonic()-v.last_seen > 900 and not (v.experiment and v.experiment.thread.is_alive()):
                    visitors.pop(key).store.close()
            if sid not in visitors:
                if len(visitors) >= 32:
                    abort(503, 'Lab visitor capacity reached; try again later')
                visitors[sid] = Visitor()
            v = visitors[sid]
            v.last_seen = time.monotonic()
            return v

    hosts = DEFAULT_HOSTS | frozenset(filter(None, [os.getenv('RENDER_EXTERNAL_HOSTNAME')]))
    app = dashboard_app(LocalProxy(lambda: visitor().store), hosts)
    app.config.update(LAB=True, SECRET_KEY=os.getenv('SECRET_KEY') or secrets.token_hex(32),
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=bool(os.getenv('RENDER')), MAX_CONTENT_LENGTH=2048)

    @app.before_request
    def lab_guard():
        if request.path == '/stream':
            abort(404)

    @app.get('/api/lab')
    def status():
        v = visitor()
        return jsonify(experiment=v.experiment.view() if v.experiment else None,
                       hypothesis='HTTP 200 and checkout response below 200ms',
                       diagnosis='deterministic rules; no AI model', maximum_seconds=120)

    @app.post('/api/lab/start')
    def start():
        body = request.get_json(silent=True)
        kind = body.get('kind') if isinstance(body, dict) else None
        if kind not in {'errors', 'latency'}:
            return jsonify(error='Choose errors or latency'), 400
        with lock:
            v = visitor()
            if v.experiment and v.experiment.thread.is_alive():
                return jsonify(error='Your experiment is already running'), 409
            if len(v.store.list_incidents()) >= 20:
                return jsonify(error='Session experiment limit reached'), 429
            if not slots.acquire(blocking=False):
                return jsonify(error='Four experiments are running. Please try again shortly.'), 429
            experiment = Experiment(kind, v.store, slots.release)
            v.experiment = experiment
            try:
                experiment.thread.start()
            except Exception:
                slots.release()
                raise
        return jsonify(id=experiment.id), 201

    @app.post('/api/lab/checkout')
    def checkout():
        v = visitor()
        experiment = v.experiment
        if not experiment:
            return jsonify(error='Start an experiment first'), 409
        with experiment.lock:
            worker = experiment.worker
            if not worker or not worker.url or experiment.stop_event.is_set():
                return jsonify(error='Checkout worker is not running'), 409
            result = worker.probe()
            experiment.samples.append(result)
        return jsonify(result)

    @app.post('/api/lab/stop')
    def stop():
        v = visitor()
        if v.experiment:
            v.experiment.stop()
        return jsonify(ok=True)

    def shutdown():
        with lock:
            for v in visitors.values():
                if v.experiment:
                    v.experiment.stop()
        for v in list(visitors.values()):
            if v.experiment and v.experiment.thread.is_alive():
                v.experiment.thread.join(timeout=8)
    atexit.register(shutdown)
    return app
