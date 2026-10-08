"""Disposable checkout HTTP process. Bound to loopback; fault control requires a token."""
import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--token', required=True)
    a = p.parse_args()
    state = {'fault': 'none'}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != '/fault' or self.headers.get('Authorization') != 'Bearer ' + a.token:
                self.send_json(403, {'error': 'refused'})
                return
            if int(self.headers.get('Content-Length', 0)) > 100:
                self.send_json(413, {'error': 'too large'})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                fault = body.get('fault')
            except (ValueError, AttributeError):
                fault = None
            if fault not in {'errors', 'latency'}:
                self.send_json(400, {'error': 'invalid fault'})
                return
            with lock:
                state['fault'] = fault
            self.send_json(200, {'fault': fault})

        def do_GET(self):
            if self.path != '/checkout':
                self.send_json(404, {'error': 'not found'})
                return
            with lock:
                fault = state['fault']
            if fault == 'latency':
                time.sleep(0.35)
            if fault == 'errors':
                self.send_json(503, {'error': 'injected checkout dependency failure', 'pid': os.getpid()})
            else:
                self.send_json(200, {'status': 'checkout completed', 'pid': os.getpid()})

    watchdog = threading.Timer(130, lambda: os._exit(0))
    watchdog.daemon = True
    watchdog.start()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    print(json.dumps({'port': server.server_port, 'pid': os.getpid()}), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
