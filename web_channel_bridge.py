#!/usr/bin/env python3
"""
Web Channel Bridge - port 7788
Routes messages between web frontend (SSE) and Web Channel Plugin (polling).

Endpoints:
  POST /api/chat          { request_id, text } → SSE stream
  GET  /internal/next     → { request_id, text, source } | null
  POST /internal/reply    { request_id, messages: [{type, text},...] } → ack
"""
import json, threading, time, queue
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

pending: queue.Queue = queue.Queue()           # messages waiting for Web Channel
sse_sinks: dict = {}                          # request_id → callback(event_data)
sse_lock = threading.Lock()


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def send_json(self, code: int, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/internal/next':
            # Web Channel polls here for new messages
            try:
                msg = pending.get(timeout=25)   # long-poll up to 25s
                self.send_json(200, msg)
            except queue.Empty:
                self.send_json(200, None)
        elif path == '/health':
            self.send_json(200, {'ok': True})
        else:
            self.send_json(404, {'error': 'not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body) if body else {}
        except Exception:
            self.send_json(400, {'error': 'bad json'})
            return

        if path == '/api/chat':
            # Web frontend submits a message, receives SSE stream in response
            request_id = data.get('request_id') or f"req-{time.time_ns()}"
            text = data.get('text', '').strip()
            if not text:
                self.send_json(400, {'error': 'text required'})
                return

            # Set up SSE response
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            done_event = threading.Event()
            reply_parts = []

            def sse_write(obj):
                try:
                    line = f"event: message\ndata: {json.dumps(obj)}\n\n"
                    self.wfile.write(line.encode())
                    self.wfile.flush()
                except Exception:
                    done_event.set()

            def on_reply(messages):
                for m in messages:
                    if m.get('type') == 'text':
                        sse_write({'type': 'text', 'text': m['text']})
                sse_write({'type': 'done'})
                done_event.set()

            with sse_lock:
                sse_sinks[request_id] = on_reply

            # Send status and enqueue message for Web Channel
            sse_write({'type': 'status', 'status': 'thinking'})
            pending.put({'request_id': request_id, 'text': text, 'source': 'web'})

            # Wait for reply (up to 5 minutes)
            done_event.wait(timeout=300)
            with sse_lock:
                sse_sinks.pop(request_id, None)

        elif path == '/internal/reply':
            # Web Channel plugin delivers Claude's response here
            request_id = data.get('request_id')
            messages = data.get('messages', [])
            with sse_lock:
                sink = sse_sinks.get(request_id)
            if sink:
                threading.Thread(target=sink, args=(messages,), daemon=True).start()
                self.send_json(200, {'ok': True})
            else:
                self.send_json(404, {'error': f'no pending request {request_id}'})

        else:
            self.send_json(404, {'error': 'not found'})


if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', 7788), BridgeHandler)
    print('Web Channel Bridge listening on http://127.0.0.1:7788', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
