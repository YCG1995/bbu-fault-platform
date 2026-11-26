"""H0 outbound URL safety tests.

These tests use loopback sockets, but every model call explicitly opts into the
private-endpoint policy so the default-blocking tests and compatibility tests
exercise different branches.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.ai import AIError, _get_json, list_models  # noqa: E402
from bbu.netguard import (  # noqa: E402
    OutboundContentTypeError,
    OutboundPolicy,
    OutboundPolicyError,
    OutboundResponseTooLarge,
    read_limited,
    validate_outbound_url,
)


class _RedirectHandler(BaseHTTPRequestHandler):
    seen: list[dict] = []
    echo_port = 0

    def do_GET(self):  # noqa: N802
        _RedirectHandler.seen.append({
            "host": self.headers.get("Host") or "",
            "path": self.path,
            "authorization": self.headers.get("Authorization") or "",
            "api_key": self.headers.get("x-api-key") or "",
        })
        if self.path.startswith("/redirect-private"):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data")
            self.end_headers()
            return
        if self.path.startswith("/redirect-file"):
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return
        if self.path.startswith("/redirect-loop"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/redirect-loop")
            self.end_headers()
            return
        if self.path.startswith("/redirect-same"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/echo")
            self.end_headers()
            return
        if self.path.startswith("/redirect-cross"):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{_RedirectHandler.echo_port}/echo")
            self.end_headers()
            return
        if self.path.startswith("/echo"):
            raw = json.dumps({
                "auth": self.headers.get("Authorization") or "",
                "api_key": self.headers.get("x-api-key") or "",
                "host": self.headers.get("Host") or "",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.startswith("/bad-json"):
            raw = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.startswith("/big"):
            raw = json.dumps({"data": [{"id": "m"}] + [{"id": "padding-" + "x" * 200}] * 6000}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.startswith("/slow"):
            time.sleep(6)
            raw = b'{"data":[{"id":"slow"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.startswith("/status401"):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"bad key"}')
            return
        if self.path.startswith("/status500"):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"upstream"}')
            return
        raw = b'{"data":[{"id":"test-model"},{"id":"test-model-2"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class NetGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        cls.port = cls.server.server_address[1]
        _RedirectHandler.echo_port = cls.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _model(self, path: str = "/v1/models", *, allow: bool = True) -> dict:
        return {
            "provider": "openai",
            "base_url": f"http://127.0.0.1:{self.port}{path}",
            "api_key": "super-secret-key",
            "extra": {},
            "allow_private": allow,
        }

    def test_private_is_blocked_by_default(self):
        with self.assertRaises(OutboundPolicyError):
            validate_outbound_url(f"http://127.0.0.1:{self.port}/v1/models")

    def test_private_api_allowed_when_explicit(self):
        self.assertTrue(validate_outbound_url(
            f"http://127.0.0.1:{self.port}/v1/models",
            OutboundPolicy(allow_private=True)))

    def test_metadata_and_link_local_never_allowed(self):
        for url in ("http://169.254.169.254/latest/meta-data",
                    "http://[fe80::1]/"):
            with self.assertRaises(OutboundPolicyError):
                validate_outbound_url(url, OutboundPolicy(allow_private=True))

    def test_scheme_userinfo_fragment_rejected(self):
        for url in ("file:///etc/passwd", "gopher://127.0.0.1/x"):
            with self.assertRaises(ValueError):
                validate_outbound_url(url, OutboundPolicy(allow_private=True))
        with self.assertRaises(OutboundPolicyError):
            validate_outbound_url("http://user:pass@127.0.0.1/x",
                                  OutboundPolicy(allow_private=True))
        with self.assertRaises(OutboundPolicyError):
            validate_outbound_url("http://127.0.0.1/x#frag",
                                  OutboundPolicy(allow_private=True))

    def test_redirect_to_private_is_blocked(self):
        with self.assertRaises(AIError):
            _get_json(f"http://127.0.0.1:{self.port}/redirect-private",
                      {"Authorization": "Bearer super-secret-key"}, 3,
                      allow_private=True)

    def test_redirect_to_file_is_blocked(self):
        with self.assertRaises(AIError):
            _get_json(f"http://127.0.0.1:{self.port}/redirect-file",
                      {"Authorization": "Bearer super-secret-key"}, 3,
                      allow_private=True)

    def test_cross_host_redirect_strips_auth(self):
        data = _get_json(f"http://127.0.0.1:{self.port}/redirect-cross",
                         {"Authorization": "Bearer super-secret-key",
                          "x-api-key": "another-secret"}, 3, allow_private=True)
        self.assertEqual(data["auth"], "")
        self.assertEqual(data["api_key"], "")

    def test_same_host_redirect_keeps_auth(self):
        data = _get_json(f"http://127.0.0.1:{self.port}/redirect-same",
                         {"Authorization": "Bearer super-secret-key"}, 3, allow_private=True)
        self.assertEqual(data["auth"], "Bearer super-secret-key")

    def test_redirect_limit(self):
        with self.assertRaises(AIError) as ctx:
            _get_json(f"http://127.0.0.1:{self.port}/redirect-loop",
                      {}, 3, allow_private=True)
        self.assertIn("403", str(ctx.exception))

    def test_bad_content_type(self):
        with self.assertRaises(AIError) as ctx:
            _get_json(f"http://127.0.0.1:{self.port}/bad-json", {}, 3, allow_private=True)
        self.assertIn("Content-Type", str(ctx.exception))

    def test_response_too_large(self):
        with self.assertRaises(AIError) as ctx:
            _get_json(f"http://127.0.0.1:{self.port}/big", {}, 3,
                      allow_private=True, max_bytes=1024)
        self.assertIn("上限", str(ctx.exception))

    def test_read_limited_does_not_truncate(self):
        class Resp:
            def __init__(self, data):
                self.data = data
                self.pos = 0

            def read(self, n):
                chunk = self.data[self.pos:self.pos + n]
                self.pos += len(chunk)
                return chunk

        with self.assertRaises(OutboundResponseTooLarge):
            read_limited(Resp(b"x" * 10), 5)

    def test_list_models_still_works(self):
        self.assertEqual(list_models(self._model()), ["test-model", "test-model-2"])

    def test_list_models_timeout(self):
        model = self._model("/slow")
        with self.assertRaises(AIError):
            list_models(model, timeout=5)

    def test_list_models_401_and_500(self):
        for path in ("/status401", "/status500"):
            with self.assertRaises(AIError):
                list_models(self._model(path), timeout=3)

    def test_api_key_not_in_error(self):
        model = self._model("/status401")
        try:
            list_models(model, timeout=3)
        except Exception as exc:
            self.assertNotIn("super-secret-key", str(exc))


if __name__ == "__main__":
    unittest.main()
