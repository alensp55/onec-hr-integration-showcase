from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .domain import MockHrService, Response


SERVICE = MockHrService()


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "HrIntegrationMock/1.0"

    def do_GET(self) -> None:
        if self.path == "/demo/v1/health":
            self._write(Response(200, {"status": "ok"}))
            return
        self._write(self._dispatch("GET"))

    def do_POST(self) -> None:
        if self.path == "/demo/v1/sessions":
            self._write(SERVICE.authenticate(self._read_json()))
            return
        self._write(self._dispatch("POST"))

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _dispatch(self, method: str) -> Response:
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip() or None
        key = self.headers.get("X-Idempotency-Key")
        return SERVICE.request(method, self.path, token, self._read_json(), key)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _write(self, response: Response) -> None:
        payload = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local HR integration mock")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DemoHandler)
    print(f"Demo API is listening on loopback host {args.host}, port {args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
