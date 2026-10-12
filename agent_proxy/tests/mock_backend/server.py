"""Deterministic E2B/Connect protocol fixture; never runs commands on the host.

Bind to loopback. File contents and sandbox state live only in memory. This is a
protocol mock, not a sandbox emulator, and its timing is not a performance result.
"""
from __future__ import annotations

import argparse
import base64
import email.parser
import email.policy
import json
import socket
import struct
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit


def frame(data, flags=0):
    body = json.dumps(data, separators=(",", ":")).encode()
    return bytes([flags]) + struct.pack(">I", len(body)) + body


def decode_frame(data):
    if len(data) < 5 or data[0] != 0:
        raise ValueError("invalid frame")
    size = struct.unpack(">I", data[1:5])[0]
    if len(data) != size + 5:
        raise ValueError("invalid frame length")
    return json.loads(data[5:])


def timestamp(epoch=None):
    return datetime.fromtimestamp(epoch or time.time(), timezone.utc).isoformat().replace("+00:00", "Z")


class Backend:
    def __init__(self, key):
        self.key = key
        self.lock = threading.RLock()
        self.sandboxes = {}
        self.next_id = 1
        self.next_pid = 100
        self.calls = []

    def info(self, sb):
        return {"sandboxID": sb["id"], "templateID": sb["template"], "state": sb["state"],
                "metadata": sb["metadata"], "envdVersion": sb["version"],
                "envdAccessToken": "mock-token-" + sb["id"], "domain": "mock.invalid",
                "endAt": timestamp(sb["expires"])}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AgentProxyMock/1"

    @property
    def backend(self):
        return self.server.backend

    def log_message(self, *_):
        pass

    def respond(self, status, body=None, content_type="application/json", headers=None):
        if body is None:
            payload = b""
        elif isinstance(body, bytes):
            payload = body
        else:
            payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 16 * 1024 * 1024:
            raise ValueError("body size")
        return self.rfile.read(size)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()

    def handle_request(self):
        try:
            body = self.read_body()
            url = urlsplit(self.path)
            path = url.path
            query = parse_qs(url.query)
            with self.backend.lock:
                self.backend.calls.append((self.command, path))
            if path == "/health":
                return self.respond(200, {"ok": True})
            if path.startswith("/envd/"):
                return self.envd(path, query, body)
            if self.headers.get("X-API-Key") != self.backend.key:
                return self.respond(401, {"message": "unauthorized"})
            if path == "/__mock__/stats":
                with self.backend.lock:
                    return self.respond(200, {"calls": self.backend.calls,
                                              "sandboxes": list(self.backend.sandboxes)})
            with self.backend.lock:
                if path == "/sandboxes" and self.command == "POST":
                    data = json.loads(body)
                    if not data.get("templateID") or data.get("timeout", 0) <= 0:
                        return self.respond(400, {})
                    if data["templateID"] == "missing":
                        return self.respond(404, {})
                    if data["templateID"] == "mock-rate-limit":
                        return self.respond(429, {}, headers={"Retry-After": "1"})
                    if data.get("autoPause") is not False or data.get("autoResume") != {"policy": "off"}:
                        return self.respond(400, {"message": "explicit lifecycle required"})
                    sid = "mock-" + str(self.backend.next_id)
                    self.backend.next_id += 1
                    sb = {"id": sid, "template": data["templateID"], "state": "running",
                          "expires": time.time() + data["timeout"], "metadata": data.get("metadata", {}),
                          "version": "0.5.6" if data["templateID"] == "mock-legacy" else "0.5.7",
                          "files": {}, "processes": {}}
                    self.backend.sandboxes[sid] = sb
                    if data["templateID"] == "mock-create-drop":
                        return self.drop()
                    return self.respond(201, self.backend.info(sb))
                parts = path.strip("/").split("/")
                if len(parts) not in (2, 3) or parts[0] != "sandboxes":
                    return self.respond(404, {})
                sid = unquote(parts[1])
                sb = self.backend.sandboxes.get(sid)
                if not sb:
                    return self.respond(404, {})
                if len(parts) == 2 and self.command == "GET":
                    return self.respond(200, self.backend.info(sb))
                if len(parts) == 2 and self.command == "DELETE":
                    for process in sb["processes"].values():
                        process.set()
                    del self.backend.sandboxes[sid]
                    return self.respond(204)
                if len(parts) == 3 and parts[2] == "pause" and self.command == "POST":
                    if sb["state"] == "paused":
                        return self.respond(409, {})
                    sb["state"] = "paused"
                    return self.respond(204)
                if len(parts) == 3 and parts[2] == "connect" and self.command == "POST":
                    data = json.loads(body)
                    if data.get("timeout", 0) <= 0:
                        return self.respond(400, {})
                    status = 200 if sb["state"] == "running" else 201
                    sb["state"] = "running"
                    sb["expires"] = max(sb["expires"], time.time() + data["timeout"])
                    return self.respond(status, self.backend.info(sb))
                return self.respond(404, {})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except (ValueError, KeyError, TypeError, struct.error):
            self.respond(400, {"message": "invalid request"})

    def drop(self):
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def envd(self, path, query, body):
        _, _, sid, route = path.split("/", 3)
        with self.backend.lock:
            sb = self.backend.sandboxes.get(sid)
            if not sb:
                return self.respond(404, {})
            if self.headers.get("X-Access-Token") != "mock-token-" + sid:
                return self.respond(401, {})
            if sb["state"] != "running":
                return self.respond(409, {})
            if route == "health":
                return self.respond(503 if sb["template"] == "mock-unready" else 200, {})
            if route == "filesystem.Filesystem/Stat":
                data = json.loads(body)
                if data["path"] not in ("/", "/tmp", "/home", "/home/user"):
                    return self.respond(404, {"code": "not_found"})
                return self.respond(200, {"entry": {"path": data["path"], "type": "FILE_TYPE_DIRECTORY"}})
            if route == "files":
                file_path = query["path"][0]
                if self.command == "GET":
                    if file_path == "/tmp/mock-binary":
                        return self.respond(200, b"\xff\xfe", "application/octet-stream")
                    if file_path == "/tmp/mock-oversize":
                        return self.respond(200, b"x" * (8 * 1024 * 1024 + 1), "application/octet-stream")
                    if file_path not in sb["files"]:
                        return self.respond(404, {})
                    return self.respond(200, sb["files"][file_path], "application/octet-stream")
                if self.command == "POST":
                    if self.headers.get("Content-Type", "").startswith("multipart/form-data"):
                        content_type = self.headers["Content-Type"]
                        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
                            ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
                        parts = list(message.iter_parts())
                        if len(parts) != 1 or parts[0].get_param("name", header="content-disposition") != "file":
                            return self.respond(400, {})
                        body = parts[0].get_payload(decode=True)
                    elif self.headers.get("Content-Type") != "application/octet-stream" or sb["version"] == "0.5.6":
                        return self.respond(415, {})
                    sb["files"][file_path] = body
                    if file_path == "/tmp/mock-write-drop":
                        return self.drop()
                    return self.respond(200, [{"path": file_path, "name": file_path.rsplit("/", 1)[-1], "type": "file"}])
            if route == "process.Process/SendSignal":
                data = json.loads(body)
                event = sb["processes"].get(data["process"]["pid"])
                if not event:
                    return self.respond(404, {"code": "not_found"})
                event.set()
                return self.respond(200, {})
        if route == "process.Process/Start":
            return self.start_process(sb, body)
        return self.respond(404, {})

    def start_process(self, sb, body):
        if self.headers.get("Content-Type") != "application/connect+json":
            return self.respond(415, {})
        data = decode_frame(body)
        process = data["process"]
        if process["cmd"] != "/bin/bash" or process["args"][:2] != ["-l", "-c"] or data.get("stdin") is not False:
            return self.respond(400, {"code": "invalid_argument"})
        command = process["args"][2]
        with self.backend.lock:
            pid = self.backend.next_pid
            self.backend.next_pid += 1
            killed = threading.Event()
            sb["processes"][pid] = killed
        start = frame({"event": {"start": {"pid": pid}}})
        out, err, code = b"", b"", 0
        delay = 0
        if command == "printf hello":
            out = b"hello"
        elif command == "exit 2":
            code = 2
        elif command == "mock:stderr":
            err = b"diagnostic\n"
        elif command == "mock:large-output":
            out = b"x" * (1024 * 1024 + 100)
        elif command.startswith("mock:sleep:"):
            delay = int(command.rsplit(":", 1)[1]) / 1000
        elif command.startswith("mock:echo:"):
            out = command[len("mock:echo:"):].encode()
        elif command not in ("mock:bad-trailer", "mock:truncate", "mock:malformed"):
            code, err = 127, b"mock: command fixture not implemented\n"
        chunks = [start]
        if out:
            for pos in range(0, len(out), 16384):
                chunks.append(frame({"event": {"data": {"stdout": base64.b64encode(out[pos:pos+16384]).decode()}}}))
        if err:
            chunks.append(frame({"event": {"data": {"stderr": base64.b64encode(err).decode()}}}))
        end = {"exited": True}
        if code:
            end.update(exitCode=code, error="exit status " + str(code))
        chunks.append(frame({"event": {"end": end}}))  # zero exitCode deliberately omitted
        chunks.append(frame({}, 2))
        if command == "mock:bad-trailer":
            chunks = [start, frame({"error": {"code": "unavailable"}}, 2)]
        elif command == "mock:truncate":
            chunks = [start]
        elif command == "mock:malformed":
            chunks = [start, frame({"event": {"data": {"stdout": 123}}}), frame({}, 2)]
        self.send_response(200)
        self.send_header("Content-Type", "application/connect+json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            self.write_chunk(chunks[0][:2])  # Deliberately split Connect framing across HTTP chunks.
            self.write_chunk(chunks[0][2:])
            if delay:
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline and not killed.wait(0.02):
                    pass
                if killed.is_set():
                    chunks = [start, frame({"event": {"end": {"exited": False, "exitCode": -1}}}), frame({}, 2)]
            for chunk in chunks[1:]:
                self.write_chunk(chunk)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        finally:
            with self.backend.lock:
                sb["processes"].pop(pid, None)

    def write_chunk(self, data):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()


def make_server(port=0, key="mock-key"):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.backend = Backend(key)
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    server = make_server(args.port)
    print(f"Mock listening on 127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
