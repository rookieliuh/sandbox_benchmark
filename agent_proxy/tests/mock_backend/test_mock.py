import base64
import http.client
import json
import os
from pathlib import Path
import subprocess
import struct
import threading
import sys
import unittest
import urllib.error
import urllib.request
from server import make_server, frame


class MockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, body=None, envd=False, content_type="application/json"):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        headers = {"Content-Type": content_type, "X-API-Key": "mock-key"}
        if envd:
            headers["X-Access-Token"] = "mock-token-" + path.split("/")[2]
        req = urllib.request.Request(self.url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=3) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()

    def create(self, template="base"):
        code, data = self.request("POST", "/sandboxes", {"templateID": template, "timeout": 300,
                                                          "autoPause": False, "autoResume": {"policy": "off"}})
        self.assertEqual(code, 201)
        return json.loads(data)["sandboxID"]

    def test_health_failures_preserve_cleanup_target(self):
        unready = self.create("mock-unready")
        self.assertEqual(self.request("GET", f"/envd/{unready}/health", envd=True)[0], 503)
        dropped = self.create("mock-health-drop")
        with self.assertRaises(http.client.RemoteDisconnected):
            self.request("GET", f"/envd/{dropped}/health", envd=True)
        for sid in (unready, dropped):
            self.assertEqual(self.request("GET", f"/sandboxes/{sid}")[0], 200)
            self.assertEqual(self.request("DELETE", f"/sandboxes/{sid}")[0], 204)

    def test_envd_health_contract_and_200_compatibility(self):
        for template, expected in (("base", 204), ("mock-health-200", 200)):
            with self.subTest(template=template):
                sid = self.create(template)
                code, body = self.request("GET", f"/envd/{sid}/health", envd=True)
                self.assertEqual(code, expected)
                if code == 204:
                    self.assertEqual(body, b"")
                self.assertEqual(self.request("DELETE", f"/sandboxes/{sid}")[0], 204)

    def test_lifecycle_preserves_files(self):
        sid = self.create()
        envd = f"/envd/{sid}/files?path=/tmp/a"
        self.assertEqual(self.request("POST", envd, "你好".encode(), True, "application/octet-stream")[0], 200)
        self.assertEqual(self.request("POST", f"/sandboxes/{sid}/pause", {})[0], 204)
        self.assertEqual(self.request("POST", f"/sandboxes/{sid}/pause", {})[0], 409)
        self.assertEqual(self.request("GET", envd, envd=True)[0], 409)
        self.assertEqual(self.request("POST", f"/sandboxes/{sid}/connect", {"timeout": 300})[0], 201)
        self.assertEqual(self.request("GET", envd, envd=True)[1], "你好".encode())
        self.assertEqual(self.request("DELETE", f"/sandboxes/{sid}")[0], 204)
        self.assertEqual(self.request("GET", f"/sandboxes/{sid}")[0], 404)

    def stream(self, command):
        sid = self.create()
        request = frame({"process": {"cmd": "/bin/bash", "args": ["-l", "-c", command]}, "stdin": False})
        code, body = self.request("POST", f"/envd/{sid}/process.Process/Start", request, True, "application/connect+json")
        self.assertEqual(code, 200)
        frames = []
        while body:
            flags, size = body[0], struct.unpack(">I", body[1:5])[0]
            frames.append((flags, json.loads(body[5:5+size])))
            body = body[5+size:]
        return frames

    def test_command_stream_and_zero_omission(self):
        frames = self.stream("printf hello")
        self.assertEqual(base64.b64decode(frames[1][1]["event"]["data"]["stdout"]), b"hello")
        self.assertNotIn("exitCode", frames[-2][1]["event"]["end"])
        self.assertEqual(frames[-1], (2, {}))

    def test_nonzero_exit_is_an_event(self):
        frames = self.stream("exit 2")
        self.assertEqual(frames[-2][1]["event"]["end"]["exitCode"], 2)

    def test_trailer_error(self):
        self.assertEqual(self.stream("mock:bad-trailer")[-1][1]["error"]["code"], "unavailable")

    def test_rate_limit(self):
        code, _ = self.request("POST", "/sandboxes", {"templateID": "mock-rate-limit", "timeout": 10})
        self.assertEqual(code, 429)

    def test_authentication(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.opener.open(self.url + "/sandboxes/anything")
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()

    def test_signal_stops_mock_process(self):
        sid = self.create()
        request = frame({"process": {"cmd": "/bin/bash", "args": ["-l", "-c", "mock:sleep:3000"]}, "stdin": False})
        req = urllib.request.Request(self.url + f"/envd/{sid}/process.Process/Start", request,
                                     {"Content-Type": "application/connect+json", "X-Access-Token": "mock-token-" + sid})
        with self.opener.open(req, timeout=4) as response:
            header = response.read(5)
            event = json.loads(response.read(struct.unpack(">I", header[1:])[0]))
            pid = event["event"]["start"]["pid"]
            code, _ = self.request("POST", f"/envd/{sid}/process.Process/SendSignal",
                                   {"process": {"pid": pid}, "signal": "SIGNAL_SIGKILL"}, True)
            self.assertEqual(code, 200)
            remaining = response.read()
            size = struct.unpack(">I", remaining[1:5])[0]
            end = json.loads(remaining[5:5+size])["event"]["end"]
            self.assertFalse(end["exited"])
            self.assertEqual(end["exitCode"], -1)

    def test_write_drop_keeps_committed_content(self):
        sid = self.create()
        path = f"/envd/{sid}/files?path=/tmp/mock-write-drop"
        import http.client
        with self.assertRaises((http.client.RemoteDisconnected, urllib.error.URLError)):
            self.request("POST", path, b"committed", True, "application/octet-stream")
        self.assertEqual(self.request("GET", path, envd=True)[1], b"committed")

    def test_parent_directory_stat(self):
        sid = self.create()
        path = f"/envd/{sid}/filesystem.Filesystem/Stat"
        self.assertEqual(self.request("POST", path, {"path": "/tmp"}, True)[0], 200)
        code, body = self.request("POST", path, {"path": "/absent"}, True)
        self.assertEqual(code, 404)
        self.assertEqual(json.loads(body)["code"], "not_found")

    def test_pause_ack_precedes_paused_state(self):
        code, body = self.request("POST", "/sandboxes", {"templateID": "mock-slow-pause", "timeout": 300,
                                                       "autoPause": False, "autoResume": {"policy": "off"}})
        self.assertEqual(code, 201)
        sid = json.loads(body)["sandboxID"]
        self.assertEqual(self.request("POST", f"/sandboxes/{sid}/pause", {})[0], 204)
        self.assertEqual(json.loads(self.request("GET", f"/sandboxes/{sid}")[1])["state"], "pausing")
        self.assertEqual(json.loads(self.request("GET", f"/sandboxes/{sid}")[1])["state"], "paused")

    def test_delete_ack_can_leave_resource(self):
        code, body = self.request("POST", "/sandboxes", {"templateID": "mock-delete-stuck", "timeout": 300,
                                                       "autoPause": False, "autoResume": {"policy": "off"}})
        self.assertEqual(code, 201)
        sid = json.loads(body)["sandboxID"]
        self.assertEqual(self.request("DELETE", f"/sandboxes/{sid}")[0], 204)
        self.assertEqual(self.request("GET", f"/sandboxes/{sid}")[0], 200)

    def test_read_only_list_does_not_mutate_sandboxes(self):
        self.create()
        before = set(self.server.backend.sandboxes)
        code, body = self.request("GET", "/sandboxes?limit=1")
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)), 1)
        self.assertEqual(set(self.server.backend.sandboxes), before)

    def run_probe(self, key):
        script = Path(__file__).resolve().parents[2] / "scripts/check_e2b.py"
        env = dict(os.environ, E2B_API_KEY=key, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        return subprocess.run([sys.executable, str(script), "--api-url", self.url],
                              capture_output=True, text=True, timeout=15, env=env)

    def test_probe_script_is_read_only(self):
        before = set(self.server.backend.sandboxes)
        calls = len(self.server.backend.calls)
        result = self.run_probe("mock-key")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS:", result.stdout)
        self.assertNotIn("mock-key", result.stdout)
        self.assertEqual(set(self.server.backend.sandboxes), before)
        self.assertTrue(all(method == "GET" for method, _ in self.server.backend.calls[calls:]))

    def test_probe_script_rejects_invalid_key(self):
        result = self.run_probe("wrong-secret-key")
        self.assertEqual(result.returncode, 1)
        self.assertIn("HTTP 401", result.stdout)
        self.assertNotIn("wrong-secret-key", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
