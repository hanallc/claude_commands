"""A browser harness for page probes that post their failures back to the test server."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import cast, override

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def firefox_probe_results(page: bytes) -> list[list[str]]:
    """Load the page in a headless Firefox frame and return the failure lists its probe posts."""
    results: list[list[str]] = []
    complete = threading.Event()

    class BrowserProbe(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if self.path == "/complete":
                _ = complete.wait(TEST_HANG_GUARD_SECS)
            elif self.path == "/":
                _ = self.wfile.write(b'<iframe src="/accounts"></iframe><img hidden src="/complete">')
            else:
                _ = self.wfile.write(page)

        def do_POST(self) -> None:
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            results.append(cast(list[str], json.loads(payload)))
            complete.set()
            self.send_response(204)
            self.end_headers()

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(("127.0.0.1", 0), BrowserProbe) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            profile = Path(temporary) / "profile"
            profile.mkdir()
            run = subprocess.run(["firefox", "--headless", "--no-remote", "--profile", str(profile),
                                  "--screenshot", str(Path(temporary) / "page.png"), f"http://127.0.0.1:{server.server_port}/"],
                                 capture_output=True, text=True, timeout=45)
            if run.returncode:
                raise AssertionError(run.stderr)
        finally:
            server.shutdown()
            thread.join(TEST_HANG_GUARD_SECS)
    return results
