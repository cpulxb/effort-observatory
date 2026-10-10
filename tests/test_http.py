import http.server
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from observatory import Observatory
from server import make_handler


class LocalServer:
    def __init__(self, handler):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        # Keep local integration traffic away from the user's HTTP proxy.
        self.proxy_environment = patch.dict(
            os.environ,
            {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"},
        )
        self.proxy_environment.start()
        self.addCleanup(self.proxy_environment.stop)

    def test_relay_preserves_body_and_pairs_response_without_real_api_calls(self):
        received = []

        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append(body)
                response = {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-integration",
                        "object": "response",
                        "model": "test-model",
                        "status": "completed",
                        "reasoning": {"effort": "high"},
                        "output": [],
                    },
                }
                payload = ("data: " + json.dumps(response) + "\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        request_body = json.dumps(
            {
                "model": "test-model",
                "input": [{"role": "user", "content": "integration message"}],
                "stream": True,
                "reasoning": {"effort": "xhigh"},
                "prompt_cache_key": "integration-thread",
                "client_metadata": {"turn_id": "integration-turn"},
            }
        ).encode()

        with tempfile.TemporaryDirectory() as directory:
            store = Observatory(directory)
            with LocalServer(Upstream) as upstream:
                route = {"/relay/v1/responses": upstream.url + "/v1/responses"}
                with patch("relay.UPSTREAM_ROUTES", route):
                    with LocalServer(make_handler(store)) as app:
                        request = urllib.request.Request(
                            app.url + "/relay/v1/responses",
                            data=request_body,
                            headers={
                                "Authorization": "Bearer local-test-secret",
                                "Content-Type": "application/json",
                            },
                        )
                        with urllib.request.urlopen(request, timeout=5) as response:
                            result = response.read()
            self.assertEqual(received, [request_body])
            self.assertIn(b"response.completed", result)
            records = [
                record
                for record in store.all_evidence()
                if record.get("thread_id") == "integration-thread"
            ]
            self.assertEqual(len(records), 2)
            self.assertEqual(len({record["capture_id"] for record in records}), 1)
            self.assertNotIn("local-test-secret", json.dumps(records))
            session = store.session("integration-thread")
            self.assertEqual(session["turns"][0]["verdict"], "mismatch")
            self.assertEqual(
                session["turns"][0]["messages"][0]["text"], "integration message"
            )

    def test_cross_origin_and_arbitrary_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Observatory(directory)
            with LocalServer(make_handler(store)) as app:
                request = urllib.request.Request(
                    app.url + "/api/status", headers={"Origin": "https://other.example"}
                )
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 403)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(app.url + "/../../.codex/auth.json")
                self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
