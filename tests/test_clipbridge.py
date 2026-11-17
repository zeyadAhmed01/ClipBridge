import base64
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import clipboard_bridge as cb  # noqa: E402


def _build_multipart_body(boundary: bytes, files: list[tuple[str, bytes, str]]) -> bytes:
    parts = []
    for filename, payload, content_type in files:
        parts.append(
            b"--"
            + boundary
            + b"\r\n"
            + f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'.encode("utf-8")
            + f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
            + payload
            + b"\r\n"
        )
    parts.append(b"--" + boundary + b"--\r\n")
    return b"".join(parts)


class _ChunkedSlowStream(io.RawIOBase):
    """Test stream returning small irregular chunks to exercise boundary splits."""

    def __init__(self, data: bytes, max_chunk: int = 13):
        self._data = data
        self._pos = 0
        self._max_chunk = max_chunk

    def readable(self):
        return True

    def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        if size is None or size < 0:
            n = self._max_chunk
        else:
            n = min(size, self._max_chunk)
        out = self._data[self._pos : self._pos + n]
        self._pos += len(out)
        return out


class TestClipBridgeUnit(unittest.TestCase):
    def test_sanitize_filename_prevents_traversal_and_reserved_names(self):
        self.assertEqual(cb.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(cb.sanitize_filename(r"C:\evil\..\CON.tar.gz"), "_CON.tar.gz")
        self.assertEqual(cb.sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(cb.sanitize_filename("nul"), "_nul")
        self.assertEqual(cb.sanitize_filename('bad<>:"|?*name.png'), "bad_______name.png")
        long_name = ("a" * 220) + ".png"
        sanitized_long = cb.sanitize_filename(long_name)
        self.assertLessEqual(len(sanitized_long), 180)
        self.assertTrue(sanitized_long.endswith(".png"))
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

        template = json.loads((REPO_ROOT / "http_shortcuts_android.json").read_text(encoding="utf-8"))
        self.assertEqual(len(template["categories"][0]["shortcuts"]), 3)
        self.assertEqual(
            [s["name"] for s in shortcuts],
            [s["name"] for s in template["categories"][0]["shortcuts"]],
        )
        self.assertIn("/api/push-text", shortcuts[0]["url"])
        self.assertIn("/api/push-files", shortcuts[1]["url"])
        self.assertEqual(shortcuts[1]["parameters"][0]["key"], "files")
        self.assertIn("/api/pull", shortcuts[2]["url"])

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

    def test_multipart_boundary_split_across_small_reads(self):
        boundary = b"SplitBoundaryXYZ"
        payload_a = b"A" * 1024 + b"\r\n--" + b"fake-inner\r\n" + b"B" * 512
        payload_b = b"\x89PNG" + b"\x00\xff" * 700
        body = _build_multipart_body(
            boundary,
            [("alpha.txt", payload_a, "text/plain"), ("beta.png", payload_b, "image/png")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            saved = cb.stream_multipart_to_staged_dir(
                _ChunkedSlowStream(body, max_chunk=7), len(body), boundary, Path(tmp)
            )
            self.assertEqual([p.name for p in saved], ["alpha.txt", "beta.png"])
            self.assertEqual(saved[0].read_bytes(), payload_a)
            self.assertEqual(saved[1].read_bytes(), payload_b)

    def test_multipart_interrupted_or_truncated_cleans_partial_files(self):
        boundary = b"TruncBoundary"
        full_body = _build_multipart_body(
            boundary,
            [("first.txt", b"complete-first", "text/plain"), ("second.bin", b"Z" * 5000, "application/octet-stream")],
        )
        truncated = full_body[:-30]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with self.assertRaises(cb.IncompleteUploadError):
                cb.stream_multipart_to_staged_dir(
                    io.BytesIO(truncated), len(full_body), boundary, tmp_path
                )
            self.assertEqual(list(tmp_path.iterdir()), [])

            # Complete Content-Length but missing closing "--"
            missing_close = full_body.replace(b"--TruncBoundary--\r\n", b"--TruncBoundary\r\n")
            with self.assertRaises(cb.IncompleteUploadError):
                cb.stream_multipart_to_staged_dir(
                    io.BytesIO(missing_close), len(missing_close), boundary, tmp_path
                )
            self.assertEqual(list(tmp_path.iterdir()), [])

    def test_multipart_exceeding_max_parts_raises_limit_error(self):
        boundary = b"ManyPartsBoundary"
        files = [(f"f_{i}.txt", b"x", "text/plain") for i in range(cb.MAX_FILES_PER_UPLOAD + 1)]
        body = _build_multipart_body(boundary, files)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with self.assertRaises(cb.UploadLimitError):
                cb.stream_multipart_to_staged_dir(
                    io.BytesIO(body), len(body), boundary, tmp_path
                )
            self.assertEqual(list(tmp_path.iterdir()), [])

    def test_transfer_history_add_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hist = cb.TransferHistory(
                maxlen=5,
                staged_dir=root / "staged",
                history_files_dir=root / "history",
                temp_dir=root,
            )
            hist.add_text("sample clipboard text", "Phone -> PC")
            items = hist.list_items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["type"], "text")
            hist.clear()
            self.assertEqual(len(hist.list_items()), 0)

    def test_history_maxlen_and_quota_evict_disk_files_deterministically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "sources"
            src_dir.mkdir()
            hist = cb.TransferHistory(
                maxlen=3,
                staged_dir=root / "staged",
                history_files_dir=root / "history",
                temp_dir=root,
                max_storage_bytes=1200,
                retention_seconds=3600,
            )
            for idx in range(5):
                src = src_dir / f"doc_{idx}.bin"
                src.write_bytes(bytes([65 + idx]) * 500)
                hist.add_files([src], "PC -> Phone (Files)")

            items = hist.list_items()
            # 500 bytes each with max_storage_bytes=1200 keeps at most 2 items on disk
            self.assertLessEqual(len(items), 2)
            status = hist.get_storage_status()
            self.assertLessEqual(status["used_bytes"], 1200)
            on_disk_dirs = list((root / "history").iterdir())
            self.assertLessEqual(len(on_disk_dirs), 2)
            # Original external source files were never touched.
            for idx in range(5):
                self.assertTrue((src_dir / f"doc_{idx}.bin").exists())

    def test_oversized_pc_to_phone_copy_does_not_exceed_quota_or_touch_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = root / "external_movie.bin"
            external.write_bytes(b"M" * 2048)
            hist = cb.TransferHistory(
                maxlen=5,
                staged_dir=root / "staged",
                history_files_dir=root / "history",
                temp_dir=root,
                max_storage_bytes=1024,
            )
            hist.add_files([external], "PC -> Phone (Files)")
            self.assertEqual(hist.get_storage_status()["used_bytes"], 0)
            self.assertEqual(list((root / "history").iterdir()), [])
            self.assertTrue(external.exists())

    def test_ttl_expiration_and_active_clipboard_and_lease_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seq = {"val": 100}
            hist = cb.TransferHistory(
                maxlen=10,
                staged_dir=root / "staged",
                history_files_dir=root / "history",
                temp_dir=root,
                max_storage_bytes=10_000,
                retention_seconds=30,
                get_clipboard_seq_fn=lambda: seq["val"],
            )

            token1, stg1 = hist.begin_upload(100)
            f1 = stg1 / "first.png"
            f1.write_bytes(b"PNG1" * 25)
            tid1, paths1, _, ok1 = hist.commit_upload(token1, stg1, [f1], "Phone -> PC", lambda _: True)
            self.assertTrue(ok1)
            hist.release_lease(tid1)

            # Even past TTL and during non-destructive clear, active clipboard transfer stays safe!
            future = time.time() + 120
            res = hist.cleanup(force=False, reclaim_inactive=True, now=future)
            self.assertEqual(res["preserved_active_count"], 1)
            self.assertTrue(paths1[0].exists())

            # Simulate user copying something else on Windows (clipboard seq changes) + active lease check.
            self.assertTrue(hist.acquire_lease(tid1))
            seq["val"] = 101
            # Still protected while an open download/toast lease is active!
            res_leased = hist.cleanup(force=True, reclaim_inactive=True, now=future)
            self.assertEqual(res_leased["preserved_active_count"], 1)
            self.assertTrue(paths1[0].exists())

            # Once lease is released and clipboard seq changed, expired transfer is cleaned!
            hist.release_lease(tid1)
            res_after = hist.cleanup(force=False, now=future)
            self.assertGreaterEqual(res_after["reclaimed_count"], 1)
            self.assertFalse(paths1[0].exists())

    def test_restart_recovery_cleans_orphans_and_adopts_live_clipboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "staged"
            history_dir = root / "history"
            staged.mkdir()
            history_dir.mkdir()
            outside_file = root / "user_personal_file.txt"
            outside_file.write_text("do not delete", encoding="utf-8")

            # Orphaned incomplete upload, old staged transfer, flat stale file, old history dirs
            orphan_staging = staged / ".staging_old_crash"
            orphan_staging.mkdir()
            (orphan_staging / "partial.bin").write_bytes(b"1234")

            old_tx = staged / "tx_old_session_1"
            old_tx.mkdir()
            (old_tx / "old.txt").write_bytes(b"old")

            flat_stale = staged / "legacy_flat.png"
            flat_stale.write_bytes(b"stale")

            live_tx = staged / "tx_prev_live_2"
            live_tx.mkdir()
            live_shot = live_tx / "live_shot.png"
            live_shot.write_bytes(b"PNGDATA")

            old_hist = history_dir / "item_1"
            old_hist.mkdir()
            (old_hist / "dup.pdf").write_bytes(b"pdf")

            hist = cb.TransferHistory(
                maxlen=10,
                staged_dir=staged,
                history_files_dir=history_dir,
                temp_dir=root,
                get_clipboard_seq_fn=lambda: 777,
                get_clipboard_files_fn=lambda: [live_shot],
            )
            report = hist.cleanup_orphans_on_startup(force=False)
            self.assertFalse(orphan_staging.exists())
            self.assertFalse(old_tx.exists())
            self.assertFalse(flat_stale.exists())
            self.assertFalse(old_hist.exists())
            self.assertTrue(live_shot.exists())
            self.assertEqual(report["preserved_active_count"], 1)
            self.assertTrue(outside_file.exists())

            # Latest transfer pointer is restored for clipbridge://save after adoption
            ptr = json.loads((root / "latest_transfer.json").read_text(encoding="utf-8"))
            self.assertEqual(ptr["transfer_id"], "tx_prev_live_2")

    def test_powershell_scripts_encode_filenames_without_command_injection(self):
        adversarial = Path("C:/Temp/$(calc.exe) ' & <b>.png")
        toast_ps = cb._build_ps_toast_script(
            f"<toast><text>{cb._xml_escape(adversarial.name)}</text></toast>"
        )
        self.assertNotIn("$(calc.exe)", toast_ps)
        self.assertIn("FromBase64String", toast_ps)

        clip_ps = cb._build_ps_set_clipboard_script([adversarial])
        self.assertNotIn("$(calc.exe)", clip_ps)
        self.assertIn("FromBase64String", clip_ps)

    def test_manage_bat_windows_safety_and_crlf(self):
        raw = (REPO_ROOT / "manage.bat").read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        txt = raw.decode("ascii")
        self.assertNotIn("EnableDelayedExpansion", txt)
        self.assertIn("profile=private remoteip=localsubnet", txt)
        self.assertIn("Wait-Process", txt)
        self.assertIn("netsh advfirewall firewall show rule", txt)
        self.assertIn("--cleanup", txt)
        self.assertIn(":CHECK_ELEVATED", txt)
        self.assertIn('if "%CB_ELEVATED%"=="1"', txt)
        self.assertIn('if "%CHOICE%"=="7" goto FIREWALL', txt)
        self.assertIn("No server was started with elevated privileges", txt)
        self.assertNotIn('for %%R in ("ClipBridge_%CB_PORT%" "ClipBridge_5050"', txt)

    def test_failed_clipboard_adapter_does_not_strand_storage_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = cb.TransferHistory(
                staged_dir=root / "staged",
                history_files_dir=root / "history",
                temp_dir=root,
                max_storage_bytes=1024 * 1024,
                get_clipboard_seq_fn=lambda: 1,
                get_clipboard_files_fn=lambda: [],
            )
            token, staging_dir = history.begin_upload(4)
            staged_file = staging_dir / "test.txt"
            staged_file.write_bytes(b"test")

            def failing_clipboard(_paths):
                raise RuntimeError("clipboard adapter unavailable")

            with self.assertRaisesRegex(RuntimeError, "adapter unavailable"):
                history.commit_upload(token, staging_dir, [staged_file], "Phone -> PC", failing_clipboard)
            self.assertFalse(history._inflight_uploads)
            self.assertTrue(all(rec["leases"] == 0 for rec in history._transfers.values()))

    def test_run_cli_cleanup_offline_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "staged"
            history_dir = root / "history"
            staged.mkdir()
            history_dir.mkdir()
            orphan = staged / ".staging_abandoned"
            orphan.mkdir()
            (orphan / "part.bin").write_bytes(b"12345")

            hist = cb.TransferHistory(
                maxlen=5,
                staged_dir=staged,
                history_files_dir=history_dir,
                temp_dir=root,
                get_clipboard_files_fn=lambda: [],
            )
            with patch.object(cb, "PORT", 1), patch.object(cb, "HISTORY", hist):
                res = cb.run_cli_cleanup(force=False)
            self.assertEqual(res["status"], "ok")
            self.assertFalse(orphan.exists())


class TestClipBridgeHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = cb.ClipBridgeHTTPServer(("127.0.0.1", 0), cb.ClipboardRequestHandler)
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
        ctx.exception.close()

    def test_blank_secret_key_never_authorizes(self):
        with patch.object(cb, "SECRET_KEY", ""):
            req = Request(
                f"http://127.0.0.1:{self.port}/api/history",
                headers={"X-Secret-Key": ""},
            )
            with self.assertRaises(HTTPError) as ctx:
                urlopen(req, timeout=3)
            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()

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
        ctx.exception.close()

    def test_chunked_encoding_without_content_length_returns_411(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.putrequest("POST", "/api/push-files")
            conn.putheader("X-Secret-Key", cb.SECRET_KEY)
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 411)
        finally:
            conn.close()

    def test_security_headers(self):
        with urlopen(f"http://127.0.0.1:{self.port}/ping", timeout=3) as resp:
            self.assertEqual(resp.headers.get("Referrer-Policy"), "no-referrer")
            self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")

    def test_no_permissive_cors_header(self):
        url = f"http://127.0.0.1:{self.port}/api/history?key={cb.SECRET_KEY}"
        with urlopen(url, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))


class TestClipBridgeConcurrencyAndStorageHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.staged_dir = root / "staged"
        self.history_dir = root / "history"
        self.save_dir = root / "Downloads"
        self.temp_dir = root
        self.clip_seq = {"val": 500}
        self.clipboard_calls: list[list[str]] = []
        self.clipboard_text = {"val": ""}

        self.hist = cb.TransferHistory(
            maxlen=25,
            staged_dir=self.staged_dir,
            history_files_dir=self.history_dir,
            temp_dir=self.temp_dir,
            max_storage_bytes=512 * 1024,
            retention_seconds=3600,
            get_clipboard_seq_fn=lambda: self.clip_seq["val"],
        )

        self.patchers = [
            patch.object(cb, "TEMP_DIR", self.temp_dir),
            patch.object(cb, "STAGED_DIR", self.staged_dir),
            patch.object(cb, "HISTORY_FILES_DIR", self.history_dir),
            patch.object(cb, "SAVE_FOLDER", self.save_dir),
            patch.object(cb, "LATEST_TRANSFER_FILE", self.temp_dir / "latest_transfer.json"),
            patch.object(cb, "HISTORY", self.hist),
            patch.object(cb.WIN_BRIDGE, "set_clipboard_files_and_optional_image", self._mock_set_files),
            patch.object(cb.WIN_BRIDGE, "set_clipboard_text", self._mock_set_text),
            patch.object(cb.WIN_BRIDGE, "get_clipboard_content", self._mock_get_clip),
            patch.object(cb.WIN_BRIDGE, "show_incoming_files_toast_async", self._mock_toast),
            patch.object(cb.WIN_BRIDGE, "show_simple_toast", lambda t, m: None),
        ]
        for p in self.patchers:
            p.start()

        self.server = cb.ClipBridgeHTTPServer(("127.0.0.1", 0), cb.ClipboardRequestHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def _mock_set_files(self, paths: list[Path]) -> bool:
        # Simulate PowerShell clipboard startup delay and verify files actually exist!
        time.sleep(0.02)
        for p in paths:
            if not p.is_file():
                return False
        self.clip_seq["val"] += 1
        self.clipboard_calls.append([p.name for p in paths])
        return True

    def _mock_set_text(self, text: str) -> bool:
        self.clip_seq["val"] += 1
        self.clipboard_text["val"] = text
        return True

    def _mock_get_clip(self) -> dict:
        if self.clipboard_text["val"]:
            return {"type": "text", "data": self.clipboard_text["val"]}
        return {"type": "empty"}

    def _mock_toast(self, file_paths, transfer_id=None, history_store=None):
        # Verify toast hero/attachment path still exists when async toast runs!
        leased = bool(history_store and transfer_id and history_store.acquire_lease(transfer_id))

        def _check():
            try:
                time.sleep(0.02)
                for p in file_paths:
                    assert p.is_file(), f"Toast file missing prematurely: {p}"
            finally:
                if leased and history_store and transfer_id:
                    history_store.release_lease(transfer_id)

        t = threading.Thread(target=_check, daemon=True)
        t.start()

    def _post_multipart(self, files: list[tuple[str, bytes, str]]) -> tuple[int, dict]:
        boundary = b"----ClipBridgeConcurrentBoundary"
        body = _build_multipart_body(boundary, files)
        req = Request(
            f"http://127.0.0.1:{self.port}/api/push-files",
            data=body,
            headers={
                "X-Secret-Key": cb.SECRET_KEY,
                "Content-Type": f"multipart/form-data; boundary={boundary.decode('ascii')}",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as err:
            data = json.loads(err.read().decode("utf-8"))
            err.close()
            return err.code, data

    def test_concurrent_uploads_never_delete_or_corrupt_each_other(self):
        num_workers = 8
        barrier = threading.Barrier(num_workers)
        results = {}
        expected_hashes = {}

        def _worker(idx: int):
            payload_a = f"worker-{idx}-text-".encode("utf-8") * 500
            payload_b = bytes([(idx * 31) % 256]) * (12 * 1024)
            expected_hashes[f"w{idx}_a.txt"] = hashlib.sha256(payload_a).hexdigest()
            expected_hashes[f"w{idx}_b.bin"] = hashlib.sha256(payload_b).hexdigest()
            barrier.wait(timeout=5)
            status, body = self._post_multipart(
                [
                    (f"w{idx}_a.txt", payload_a, "text/plain"),
                    (f"w{idx}_b.bin", payload_b, "application/octet-stream"),
                ]
            )
            results[idx] = (status, body)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(num_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(len(results), num_workers)
        for idx, (status, body) in results.items():
            self.assertEqual(status, 200, f"Worker {idx} failed: {body}")
            self.assertEqual(body["files"], [f"w{idx}_a.txt", f"w{idx}_b.bin"])

        # Wait briefly for async toast threads to release leases
        time.sleep(0.15)
        self.assertEqual(list(self.staged_dir.glob(".staging_*")), [])

        with urlopen(f"http://127.0.0.1:{self.port}/api/history?key={cb.SECRET_KEY}", timeout=5) as resp:
            hist_data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(len(hist_data["items"]), num_workers)

        seen_files = set()
        for item in hist_data["items"]:
            self.assertEqual(len(item["files"]), 2)
            for f in item["files"]:
                seen_files.add(f["name"])
                with urlopen(f"http://127.0.0.1:{self.port}{f['url']}", timeout=5) as dl:
                    content = dl.read()
                self.assertEqual(
                    hashlib.sha256(content).hexdigest(),
                    expected_hashes[f["name"]],
                    f"Corrupted content for {f['name']}",
                )
        self.assertEqual(seen_files, set(expected_hashes.keys()))

    def test_interrupted_and_malformed_uploads_preserve_prior_clipboard_and_clean_disk(self):
        # 1. Perform a valid upload first.
        status, body = self._post_multipart([("good.txt", b"VALID_PAYLOAD", "text/plain")])
        self.assertEqual(status, 200)
        self.assertEqual(body["files"], ["good.txt"])

        # 2. Abrupt socket disconnect mid-raw-upload.
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            headers = (
                f"POST /api/push-image HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"X-Secret-Key: {cb.SECRET_KEY}\r\n"
                f"Content-Type: image/png\r\n"
                f"X-File-Name: broken.png\r\n"
                f"Content-Length: 5000\r\n\r\n"
            ).encode("ascii")
            sock.sendall(headers + b"\x89PNG_PARTIAL")
        finally:
            sock.close()

        # 3. Complete Content-Length but malformed multipart (missing closing boundary).
        bad_mp = b"--badb\r\nContent-Disposition: form-data; name=\"files\"; filename=\"bad.txt\"\r\n\r\nabc\r\n"
        req = Request(
            f"http://127.0.0.1:{self.port}/api/push-files",
            data=bad_mp,
            headers={
                "X-Secret-Key": cb.SECRET_KEY,
                "Content-Type": "multipart/form-data; boundary=badb",
                "Content-Length": str(len(bad_mp)),
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # 4. Abrupt disconnect mid-push-text.
        sock2 = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            headers = (
                f"POST /api/push-text HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"X-Secret-Key: {cb.SECRET_KEY}\r\n"
                f"Content-Type: text/plain\r\n"
                f"Content-Length: 2000\r\n\r\n"
            ).encode("ascii")
            sock2.sendall(headers + b"truncated")
        finally:
            sock2.close()

        time.sleep(0.15)
        self.assertEqual(list(self.staged_dir.glob(".staging_*")), [])
        self.assertEqual(self.clipboard_text["val"], "")

        # Save-latest still saves good.txt uncorrupted!
        with urlopen(f"http://127.0.0.1:{self.port}/api/save-latest?key={cb.SECRET_KEY}", timeout=5) as resp:
            save_res = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(save_res["status"], "ok")
        saved_file = Path(save_res["saved"][0])
        self.assertEqual(saved_file.name, "good.txt")
        self.assertEqual(saved_file.read_bytes(), b"VALID_PAYLOAD")

    def test_concurrent_inflight_budget_returns_503_and_recovers_after_disconnect(self):
        # Shrink storage budget to 100 KB for this test.
        self.hist.max_storage_bytes = 100 * 1024
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            # Start an 80 KB upload and stall after headers + 1 KB.
            headers = (
                f"POST /api/push-image HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"X-Secret-Key: {cb.SECRET_KEY}\r\n"
                f"Content-Type: image/png\r\n"
                f"X-File-Name: slow.png\r\n"
                f"Content-Length: {80 * 1024}\r\n\r\n"
            ).encode("ascii")
            sock.sendall(headers + b"A" * 1024)
            time.sleep(0.1)

            # Second simultaneous 40 KB upload exceeds 100 KB inflight reservation -> 503.
            status, body = self._post_multipart([("overflow.bin", b"B" * (35 * 1024), "application/octet-stream")])
            self.assertEqual(status, 503)
            self.assertEqual(body["status"], "error")
        finally:
            sock.close()

        # After slow upload drops, reservation is released and normal upload succeeds.
        time.sleep(0.15)
        status2, body2 = self._post_multipart([("recovered.bin", b"C" * (40 * 1024), "application/octet-stream")])
        self.assertEqual(status2, 200)
        self.assertEqual(body2["files"], ["recovered.bin"])

    def test_quota_enforced_across_repeated_large_uploads(self):
        self.hist.max_storage_bytes = 220 * 1024
        for idx in range(5):
            status, _ = self._post_multipart(
                [(f"chunk_{idx}.bin", bytes([idx + 1]) * (90 * 1024), "application/octet-stream")]
            )
            self.assertEqual(status, 200)
            time.sleep(0.05)

        time.sleep(0.15)
        with urlopen(f"http://127.0.0.1:{self.port}/api/storage?key={cb.SECRET_KEY}", timeout=5) as resp:
            storage = json.loads(resp.read().decode("utf-8"))
        self.assertLessEqual(storage["used_bytes"], 220 * 1024)
        self.assertLessEqual(
            cb._dir_size_bytes(self.staged_dir) + cb._dir_size_bytes(self.history_dir),
            220 * 1024,
        )

    def test_non_destructive_cleanup_and_warning_before_force_clear(self):
        self.assertEqual(self._post_multipart([("older.txt", b"OLD_DATA", "text/plain")])[0], 200)
        self.assertEqual(self._post_multipart([("active.png", b"PNG_ACTIVE", "image/png")])[0], 200)
        time.sleep(0.1)

        with urlopen(f"http://127.0.0.1:{self.port}/api/storage?key={cb.SECRET_KEY}", timeout=5) as resp:
            st = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(len(st["protected_items"]), 1)
        self.assertEqual(st["protected_items"][0]["files"], ["active.png"])
        self.assertGreater(st["reclaimable_bytes"], 0)

        # Non-destructive cleanup removes older.txt but protects active.png!
        req_clean = Request(
            f"http://127.0.0.1:{self.port}/api/cleanup?key={cb.SECRET_KEY}",
            data=b"",
            method="POST",
        )
        with urlopen(req_clean, timeout=5) as resp:
            clean_res = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(clean_res["reclaimed_count"], 1)
        self.assertEqual(clean_res["preserved_active_count"], 1)
        self.assertIn("active", clean_res["warning"].lower())

        # Non-destructive history clear warns and keeps active.png safe!
        req_clear = Request(
            f"http://127.0.0.1:{self.port}/api/history/clear?key={cb.SECRET_KEY}",
            data=b"",
            method="POST",
        )
        with urlopen(req_clear, timeout=5) as resp:
            clear_res = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(clear_res["preserved_active_count"], 1)
        self.assertIn("active.png", clear_res["warning"])

        # Force clear removes active transfer as well once user confirms!
        req_force = Request(
            f"http://127.0.0.1:{self.port}/api/history/clear?key={cb.SECRET_KEY}&force=1",
            data=b"",
            method="POST",
        )
        with urlopen(req_force, timeout=5) as resp:
            force_res = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(force_res["preserved_active_count"], 0)

        with urlopen(f"http://127.0.0.1:{self.port}/api/storage?key={cb.SECRET_KEY}", timeout=5) as resp:
            st_after = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(st_after["used_bytes"], 0)

    def test_android_text_push_replaces_clipboard_and_pull_works(self):
        # First stage a file, then push text -> text push unpins previous staged file!
        self.assertEqual(self._post_multipart([("shot.png", b"IMG", "image/png")])[0], 200)
        time.sleep(0.1)

        req_txt = Request(
            f"http://127.0.0.1:{self.port}/api/push-text",
            data=b"hello from android",
            headers={
                "X-Secret-Key": cb.SECRET_KEY,
                "Content-Type": "text/plain; charset=utf-8",
            },
            method="POST",
        )
        with urlopen(req_txt, timeout=5) as resp:
            push_res = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(push_res["status"], "ok")

        with urlopen(f"http://127.0.0.1:{self.port}/api/storage?key={cb.SECRET_KEY}", timeout=5) as resp:
            st = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(st["protected_items"], [])

        req_pull = Request(
            f"http://127.0.0.1:{self.port}/api/pull",
            headers={"X-Secret-Key": cb.SECRET_KEY},
            method="GET",
        )
        with urlopen(req_pull, timeout=5) as resp:
            pull_res = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(pull_res["type"], "text")
        self.assertEqual(pull_res["content"], "hello from android")


if __name__ == "__main__":
    unittest.main()
