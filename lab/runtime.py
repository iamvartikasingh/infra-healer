"""Real HTTP experiments, bounded by time, concurrency, sample count, and target."""
from __future__ import annotations

import json
import secrets
import selectors
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone

from agent.state.machine import State
from agent.state.store import Store


class Worker:
    def __init__(self):
        self.token = secrets.token_hex(24)
        self.process = subprocess.Popen([sys.executable, '-m', 'lab.worker', '--token', self.token],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        # readline executes on the experiment thread; stop() terminates startup too.
        self.url = None

    def ready(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=5):
                raise TimeoutError("Checkout worker did not start within five seconds")
        line = self.process.stdout.readline()
        info = json.loads(line)
        self.url = 'http://127.0.0.1:' + str(info['port'])
        return info['pid']

    def fault(self, kind):
        req = urllib.request.Request(self.url + '/fault', json.dumps({'fault': kind}).encode(),
            {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.token})
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.load(response)

    def probe(self):
        start = time.monotonic()
        try:
            with urllib.request.urlopen(self.url + '/checkout', timeout=2) as response:
                code = response.status
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
        except (OSError, urllib.error.URLError):
            code = 0
        return {'ts': datetime.now(timezone.utc).isoformat(), 'status': code,
                'latency_ms': round((time.monotonic() - start) * 1000, 2),
                'pid': self.process.pid}

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.process.stdout:
            self.process.stdout.close()


class Experiment:
    def __init__(self, kind, store, release, interval=1.0, lifetime=120.0):
        self.kind, self.store, self.release = kind, store, release
        self.interval, self.lifetime = interval, lifetime
        self.id = secrets.token_hex(8)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.samples = deque(maxlen=150)
        self.events = deque(maxlen=30)
        self.phase, self.incident_id = 'STARTING', None
        self.started = time.monotonic()
        self.timings = {}
        self.thread = threading.Thread(target=self.run, daemon=True)

    def note(self, message):
        self.events.append({'ts': datetime.now(timezone.utc).isoformat(), 'message': message})

    def view(self):
        with self.lock:
            samples = list(self.samples)
            return {'id': self.id, 'kind': self.kind, 'phase': self.phase,
                    'incident_id': self.incident_id, 'samples': samples, 'events': list(self.events),
                    'timings': dict(self.timings), 'elapsed_seconds': round(time.monotonic()-self.started, 1),
                    'healthy_rate': round(100 * sum(s['status']==200 and s['latency_ms']<200 for s in samples)/len(samples),1) if samples else None,
                    'success_rate': round(100 * sum(s['status']==200 for s in samples)/len(samples),1) if samples else None,
                    'worker_pid': self.worker.process.pid if self.worker else None}

    def spawn(self):
        with self.lock:
            self.worker = Worker()
            worker = self.worker
        pid = worker.ready()
        with self.lock:
            self.note(f'Checkout HTTP worker started (process {pid})')
        return worker

    def run(self):
        iid = None
        try:
            worker = self.spawn()
            consecutive_bad, consecutive_ok, recovered = 0, 0, 0
            for index in range(150):
                if self.stop_event.is_set() or time.monotonic()-self.started >= self.lifetime:
                    break
                sample = worker.probe()
                with self.lock:
                    self.samples.append(sample)
                    if index < 5:
                        self.phase = 'BASELINE'
                    if index == 4:
                        if not all(s['status']==200 and s['latency_ms']<200 for s in self.samples):
                            raise RuntimeError('Baseline did not meet the 200ms / HTTP 200 hypothesis')
                        worker.fault(self.kind)
                        self.timings['fault_at_seconds'] = round(time.monotonic()-self.started,2)
                        self.phase = 'FAULT_ACTIVE'
                        self.note(f'Injected {self.kind} into this checkout worker only')
                    bad = sample['status'] != 200 or sample['latency_ms'] >= 200
                    consecutive_bad = consecutive_bad + 1 if bad else 0
                    if iid is None and index >= 5 and consecutive_bad >= 3:
                        self.timings['detection_seconds'] = round(time.monotonic()-self.started-self.timings['fault_at_seconds'],2)
                        iid = self.store.create_incident(self.id, 'live-lab', 'checkout-worker', 'app=checkout-worker',
                            'HTTP_ERRORS' if self.kind=='errors' else 'HIGH_LATENCY',
                            f'3 consecutive requests breached the HTTP 200 / <200ms hypothesis ({self.kind})')
                        self.incident_id = iid
                        self.store.transition(iid, State.DIAGNOSING, 'Examining actual HTTP probe results')
                        context = {'log_tail': '\n'.join(json.dumps(s) for s in list(self.samples)[-8:]),
                                   'events': [], 'previous_log_tail': ''}
                        diagnosis = {'rootCause': f'Injected {self.kind} is affecting the checkout process.', 'confidence': 1.0,
                                     'recommendedAction': 'RESTART_WORKER',
                                     'reasoning': 'Deterministic experiment rule: three observed request breaches after controlled injection. No LLM is used.'}
                        policy = {'outcome': 'REQUIRE_APPROVAL', 'rule': 'LAB_APPROVAL_REQUIRED',
                                  'reason': 'Only restarting this isolated checkout worker is permitted, after explicit approval.',
                                  'mode': 'HUMAN_APPROVAL', 'action': 'RESTART_WORKER'}
                        self.store.transition(iid, State.ACTION_PROPOSED, policy['reason'], approval='PENDING',
                            context_json=json.dumps(context), diagnosis_json=json.dumps(diagnosis), policy_json=json.dumps(policy))
                        self.phase = 'AWAITING_APPROVAL'
                        self.note('Request breaches detected; restart proposed for human approval')
                    if iid:
                        inc = self.store.get(iid)
                        if inc['approval']=='REJECTED':
                            self.store.transition(iid, State.ESCALATED, 'Human rejected restart; experiment stopped without recovery')
                            self.phase = 'REJECTED'
                            self.note('Restart rejected. Experiment stopped; recovery was not verified.')
                            return
                        if inc['state']=='ACTION_PROPOSED' and inc['approval']=='APPROVED':
                            self.timings['approval_at_seconds'] = round(time.monotonic()-self.started,2)
                            self.store.transition(iid, State.REMEDIATING, f'Restarting checkout process {worker.process.pid}')
                            if not self.store.claim_execution(iid, 'RESTART_WORKER', 'human'):
                                raise RuntimeError('Execution already claimed')
                            old_pid = worker.process.pid
                            worker.stop()
                            worker = self.spawn()
                            self.store.finish_execution(iid, 'RESTART_WORKER', 'EXECUTED', f'Process {old_pid} replaced by {worker.process.pid}')
                            self.store.transition(iid, State.VERIFYING, 'Require three HTTP 200 responses below 200ms from the replacement process')
                            self.phase = 'VERIFYING'
                            continue
                        if inc['state']=='VERIFYING':
                            consecutive_ok = consecutive_ok + 1 if not bad else 0
                            if consecutive_ok >= 3:
                                self.timings['recovery_seconds'] = round(time.monotonic()-self.started-self.timings['approval_at_seconds'],2)
                                self.store.transition(iid, State.RESOLVED, 'Replacement passed 3 consecutive real HTTP requests below 200ms')
                                self.phase = 'RECOVERED'
                                self.note('Recovery verified by three successful checkout requests')
                        if self.phase == 'RECOVERED':
                            recovered += 1
                            if recovered >= 5:
                                self.phase = 'COMPLETE'
                                return
                if self.stop_event.wait(self.interval):
                    break
            with self.lock:
                self.phase = 'STOPPED' if self.stop_event.is_set() else 'EXPIRED'
                self.note('Experiment ended; worker cleaned up')
        except Exception as e:
            with self.lock:
                self.phase = 'FAILED'
                self.note(f'Experiment failed: {type(e).__name__}: {e}')
        finally:
            with self.lock:
                if iid and self.store.get(iid)['state'] not in {'RESOLVED','ESCALATED'}:
                    self.store.transition(iid, State.ESCALATED, 'Experiment ended before verified recovery', approval='NONE')
                if self.worker:
                    self.worker.stop()
                    self.worker = None
            self.release()

    def stop(self):
        self.stop_event.set()
