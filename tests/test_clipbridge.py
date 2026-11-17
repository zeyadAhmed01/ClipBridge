from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import clipboard_bridge as cb  # noqa: E402


class TestClipBridgeUnit(unittest.TestCase):
    def test_sanitize_filename_prevents_traversal_and_reserved_names(self):
        self.assertEqual(cb.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(cb.sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(cb.sanitize_filename("nul"), "_nul")
        self.assertEqual(cb.sanitize_filename('bad<>:"|?*name.png'), "bad_______name.png")
        fallback = cb.sanitize_filename("   ", ".png")
        self.assertTrue(fallback.startswith("Shared_") and fallback.endswith(".png"))

    def test_dynamic_shortcuts_json_generation(self):
        payload = cb.build_dynamic_shortcuts_json("192.168.1.50", 5050, "test-secret")
        self.assertEqual(payload["version"], 80)
        shortcuts = payload["categories"][0]["shortcuts"]
        self.assertEqual(len(shortcuts), 3)
        vars_by_key = {v["key"]: v["value"] for v in payload["variables"]}
        self.assertEqual(vars_by_key["pc_ip"], "192.168.1.50")
        self.assertEqual(vars_by_key["secret_key"], "test-secret")

    def test_chunked_multipart_streaming_multiple_files(self):
        boundary = b"----ClipBridgeTestBoundary"
        body = (
            b"------ClipBridgeTestBoundary\r\n"
            b'Content-Disposition: form-data; name="files"; filename="notes.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
            b"Hello ClipBridge\r\n"
            b"------ClipBridgeTestBoundary\r\n"
            b'Content-Disposition: form-data; name="files"; filename="shot.png"\r\n'
            b"Content-Type: image/png\r\n\r\n"
            b"\x89PNG\r\n\x1a\nMOCK_IMAGE_DATA\r\n"
            b"------ClipBridgeTestBoundary--\r\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            saved = cb.stream_multipart_to_staged_dir(
                io.BytesIO(body), len(body), boundary, Path(tmp)
            )
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0].name, "notes.txt")
            self.assertEqual(saved[0].read_bytes(), b"Hello ClipBridge")
            self.assertEqual(saved[1].name, "shot.png")
            self.assertEqual(saved[1].read_bytes(), b"\x89PNG\r\n\x1a\nMOCK_IMAGE_DATA")

    def test_transfer_history_add_and_clear(self):
        hist = cb.TransferHistory(maxlen=5)
        hist.add_text("sample clipboard text", "Phone -> PC")
        items = hist.list_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "text")
        hist.clear()
        self.assertEqual(len(hist.list_items()), 0)


class TestClipBridgeHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cb.ClipboardRequestHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_ping_open_without_auth(self):
        with urlopen(f"http://127.0.0.1:{self.port}/ping", timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["service"], "ClipBridge")

    def test_unauthorized_request_rejected(self):
        with self.assertRaises(HTTPError) as ctx:
            urlopen(f"http://127.0.0.1:{self.port}/api/pull", timeout=3)
        self.assertEqual(ctx.exception.code, 401)

    def test_dynamic_shortcuts_endpoint_requires_key(self):
        url = f"http://127.0.0.1:{self.port}/setup/shortcuts.json?key={cb.SECRET_KEY}"
        with urlopen(url, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("categories", data)

    def test_oversized_text_returns_413(self):
        req = Request(
            f"http://127.0.0.1:{self.port}/api/push-text",
            data=b"a" * 10,
            headers={
                "X-Secret-Key": cb.SECRET_KEY,
                "Content-Length": str(cb.MAX_TEXT_BYTES + 1),
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 413)

    def test_security_headers(self):
        with urlopen(f"http://127.0.0.1:{self.port}/ping", timeout=3) as resp:
            self.assertEqual(resp.headers.get("Referrer-Policy"), "no-referrer")
            self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")

    def test_no_permissive_cors_header(self):
        url = f"http://127.0.0.1:{self.port}/api/history?key={cb.SECRET_KEY}"
        with urlopen(url, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))


if __name__ == "__main__":
    unittest.main()
