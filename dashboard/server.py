"""Tiny dashboard server: static files + Server-Sent Events over runs/events.jsonl.

    python dashboard/server.py            # http://localhost:8787
    python dashboard/server.py --port 9000 --events runs/events.jsonl

Stdlib only. GET /events streams every existing event (so a fresh browser tab
rebuilds the current state), then tails the file for new lines. Add
?from=end to skip history.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
DEFAULT_EVENTS = HERE.parent / "runs" / "events.jsonl"


def read_env() -> dict[str, str]:
    """Minimal .env reader (stdlib only); the ambient environment wins."""
    env: dict[str, str] = {}
    path = HERE.parent / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    env.update({k: v for k, v in os.environ.items() if k.startswith("ARIZE_")})
    return env


def ax_project_url() -> str | None:
    """Arize AX link to the factory's tracing project, if the IDs are configured."""
    e = read_env()
    org, space, proj = e.get("ARIZE_ORG_ID"), e.get("ARIZE_SPACE_ID"), e.get("ARIZE_PROJECT_ID")
    if not (org and space and proj):
        return None
    return (f"https://app.arize.com/organizations/{org}/spaces/{space}/projects/{proj}"
            "?selectedTab=llmTracing&envA=tracing&modelType=generative_llm")


class Handler(SimpleHTTPRequestHandler):
    events_path: Path = DEFAULT_EVENTS

    def log_message(self, fmt, *args):  # quieter console
        if not self.path.startswith("/events"):
            super().log_message(fmt, *args)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/events":
            return self._stream(parse_qs(url.query).get("from", ["start"])[0])
        if url.path == "/config":
            body = json.dumps({"ax_project_url": ax_project_url()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def _stream(self, start_from: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        path = self.events_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        try:
            with path.open("r", encoding="utf-8") as f:
                if start_from == "end":
                    f.seek(0, 2)
                else:
                    self.wfile.write(b"event: replay-start\ndata: {}\n\n")
                    self._drain(f)
                    self.wfile.write(b"event: replay-end\ndata: {}\n\n")
                    self.wfile.flush()
                last_beat = time.time()
                while True:
                    if self._drain(f):
                        last_beat = time.time()
                    elif time.time() - last_beat > 15:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_beat = time.time()
                    # File truncated (e.g. `simulate.py --reset`): start over.
                    if path.stat().st_size < f.tell():
                        f.seek(0)
                        self.wfile.write(b"event: reset\ndata: {}\n\n")
                    time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _drain(self, f) -> bool:
        wrote = False
        while True:
            pos = f.tell()
            line = f.readline()
            if not line:
                break
            if not line.endswith("\n"):  # partial write; retry next tick
                f.seek(pos)
                break
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                continue
            self.wfile.write(f"data: {line}\n\n".encode())
            wrote = True
        if wrote:
            self.wfile.flush()
        return wrote


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    args = ap.parse_args()
    Handler.events_path = args.events.resolve()
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(HERE)))
    server.daemon_threads = True
    print(f"Factory dashboard on http://localhost:{args.port}  (events: {Handler.events_path})")
    server.serve_forever()


if __name__ == "__main__":
    main()
