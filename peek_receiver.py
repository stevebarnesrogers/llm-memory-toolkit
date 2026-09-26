#!/usr/bin/env python3
"""
Mobile Screen Context Awareness — Peek Receiver

Receives screenshot uploads from a mobile device and:
1. Forwards the screenshot to the AI's messaging channel (so it can see what the user is doing)
2. Injects a lightweight signal into the AI's session so it knows to respond to the screenshot

This enables the AI to proactively understand the user's current device context
and provide situationally-aware responses.

Setup:
  Mobile side: Configure MacroDroid (or similar automation app) to POST a screenshot
               to this server whenever a trigger notification is received.
  Server side: Set environment variables and run this script.

Environment variables:
  PEEK_SECRET        Shared secret for authenticating uploads (required)
  NOTIFY_BOT_TOKEN   Bot token for forwarding screenshots to a messaging channel (optional)
  NOTIFY_CHAT_ID     Target chat ID for the messaging channel (optional)
  PEEK_SAVE_DIR      Directory to save screenshots (default: /tmp/peek)
  PEEK_TMUX_SESSION  tmux session name to inject signals into (default: main)
  PEEK_PORT          Port to listen on (default: 8766)
"""
import http.server
import os
import cgi
import subprocess
import time
import urllib.request
from datetime import datetime

SECRET        = os.environ.get('PEEK_SECRET', '')
SAVE_DIR      = os.environ.get('PEEK_SAVE_DIR', '/tmp/peek')
CHAT_ID       = os.environ.get('NOTIFY_CHAT_ID', '')
BOT_TOKEN     = os.environ.get('NOTIFY_BOT_TOKEN', '')
TMUX_SESSION  = os.environ.get('PEEK_TMUX_SESSION', 'main')
PORT          = int(os.environ.get('PEEK_PORT', 8766))
MAX_SAVED     = 10
COOLDOWN_SECS = 30

_last_forward_time = 0


def send_photo_via_bot(token, chat_id, photo_path):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    boundary = "----PeekBoundary"
    with open(photo_path, "rb") as f:
        photo_data = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{chat_id}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="peek.jpg"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + photo_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status == 200


def inject_tmux_signal(session, filepath):
    signal = f"[peek_received:{filepath}]"
    subprocess.run(["tmux", "send-keys", "-t", session, signal], check=False)
    time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=False)


class Handler(http.server.BaseHTTPRequestHandler):

    def do_POST(self):
        if self.path != "/peek":
            self.send_response(404)
            self.end_headers()
            return

        auth = self.headers.get("X-Secret", "")
        if not SECRET or auth != SECRET:
            self.send_response(403)
            self.end_headers()
            return

        try:
            ctype, _ = cgi.parse_header(self.headers.get("Content-Type", ""))
            length = int(self.headers.get("Content-Length", 0))

            if ctype == "multipart/form-data":
                pdict = {}
                _, pdict_raw = cgi.parse_header(self.headers.get("Content-Type", ""))
                pdict_raw["boundary"] = pdict_raw["boundary"].encode()
                pdict_raw["CONTENT-LENGTH"] = length
                form = cgi.parse_multipart(self.rfile, pdict_raw)
                image_data = form.get("image", [None])[0]
                if image_data is None:
                    self.send_response(400)
                    self.end_headers()
                    return
                raw_bytes = bytes(image_data) if not isinstance(image_data, bytes) else image_data
            else:
                raw_bytes = self.rfile.read(length)
                if not raw_bytes:
                    self.send_response(400)
                    self.end_headers()
                    return

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"peek_{ts}.jpg"
            filepath = os.path.join(SAVE_DIR, filename)
            with open(filepath, "wb") as f:
                f.write(raw_bytes)

            shots = sorted(
                [x for x in os.listdir(SAVE_DIR) if x.startswith("peek_")],
                reverse=True,
            )
            for old in shots[MAX_SAVED:]:
                os.remove(os.path.join(SAVE_DIR, old))

            global _last_forward_time
            now = time.time()
            if now - _last_forward_time < COOLDOWN_SECS:
                print(f"[peek] cooldown active, saved {filepath} but not forwarding", flush=True)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return
            _last_forward_time = now

            tg_ok = False
            if BOT_TOKEN and CHAT_ID:
                try:
                    tg_ok = send_photo_via_bot(BOT_TOKEN, CHAT_ID, filepath)
                except Exception as e:
                    print(f"[peek] bot notification error: {e}", flush=True)

            inject_tmux_signal(TMUX_SESSION, filepath)

            print(f"[peek] saved {filepath}, tg_sent={tg_ok}, tmux_injected", flush=True)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        except Exception as e:
            print(f"[peek] error: {e}", flush=True)
            self.send_response(500)
            self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not SECRET:
        print("[peek] WARNING: PEEK_SECRET not set, all requests will be rejected", flush=True)
    os.makedirs(SAVE_DIR, exist_ok=True)
    print(f"[peek] receiver running on :{PORT}", flush=True)
    http.server.HTTPServer(("", PORT), Handler).serve_forever()
