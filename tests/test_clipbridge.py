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
        self.assertIn('if not "%CB_ELEVATED%"=="0"', txt)
        self.assertIn('set "CB_ELEVATED=UNKNOWN"', txt)
        self.assertIn('if "%CB_CHECK_CODE%"=="21"', txt)
        self.assertIn('if "%CHOICE%"=="7" goto FIREWALL', txt)
        self.assertIn("No server was started with elevated privileges", txt)
        self.assertNotIn('for %%R in ("ClipBridge_%CB_PORT%" "ClipBridge_5050"', txt)
        # Issue 1, 2, 3 safety assertions:
        self.assertIn('python "%PY_SCRIPT%" --status', txt)
        self.assertIn('python "%PY_SCRIPT%" --install-startup', txt)
        self.assertIn('python "%PY_SCRIPT%" --uninstall-startup', txt)
        self.assertIn('python "%PY_SCRIPT%" --uninstall-firewall', txt)
        self.assertIn('python "%PY_SCRIPT%" --check-firewall', txt)
        self.assertNotIn("ClipboardBridge_SilentStartup.vbs", txt)
        self.assertNotIn("ClipboardBridge_5050", txt)
        status_block = txt.split(":STATUS", 1)[1].split(":RESTART", 1)[0]
        self.assertNotIn("--register-only", status_block)

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

    def test_cli_status_and_info_are_strictly_readonly(self):
        buf = io.StringIO()
        with (
            patch.object(cb, "register_protocol_handler") as mock_reg,
            patch.object(cb, "unregister_protocol_handler") as mock_unreg,
            patch.object(cb, "install_startup_launcher") as mock_startup,
            patch.object(cb, "get_protocol_status", return_value={"registered": False, "command": None, "target_script": None, "owned_by_current": False}),
            patch.object(cb.HISTORY, "cleanup_orphans_on_startup") as mock_clean,
            patch.object(cb, "ClipBridgeHTTPServer") as mock_srv,
            patch("sys.stdout", buf),
        ):
            rc1 = cb.main(["--status"])
            rc2 = cb.main(["--info"])
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        mock_reg.assert_not_called()
        mock_unreg.assert_not_called()
        mock_startup.assert_not_called()
        mock_clean.assert_not_called()
        mock_srv.assert_not_called()

    def test_normal_startup_and_malformed_args_never_register_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hist = cb.TransferHistory(
                staged_dir=root / "staged",
                history_files_dir=root / "history",
                temp_dir=root,
                get_clipboard_files_fn=lambda: [],
            )

            class _FakeServer:
                def __init__(self, *_a, **_kw):
                    pass

                def serve_forever(self):
                    return

                def server_close(self):
                    return

            with (
                patch.object(cb, "HISTORY", hist),
                patch.object(cb, "register_protocol_handler") as mock_reg,
                patch.object(cb, "ClipBridgeHTTPServer", _FakeServer),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                # Normal server invocation must NOT auto-register protocol.
                self.assertEqual(cb.main([]), 0)
                mock_reg.assert_not_called()

                # Malformed arguments fail closed with exit code 2 and zero side effects.
                self.assertEqual(cb.main(["--unknown-flag"]), 2)
                self.assertEqual(cb.main(["--protocol"]), 2)
                self.assertEqual(cb.main(["--status", "extra"]), 2)
                mock_reg.assert_not_called()

                # Explicit registration is the only path that calls register_protocol_handler.
                self.assertEqual(cb.main(["--register-only"]), 0)
                mock_reg.assert_called_once()

    def test_protocol_ownership_parsing_and_uninstall_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_a = root / "Copy A & (v1)" / "clipboard_bridge.py"
            install_b = root / "Copy B" / "clipboard_bridge.py"
            install_a.parent.mkdir(parents=True)
            install_b.parent.mkdir(parents=True)
            install_a.write_text("# a", encoding="utf-8")
            install_b.write_text("# b", encoding="utf-8")

            cmd_a = f'"C:\\Python\\pythonw.exe" "{install_a}" --protocol "%1"'
            cmd_b = f'"C:\\Python\\pythonw.exe" "{install_b}" --protocol "%1"'
            self.assertTrue(cb.is_protocol_command_owned(cmd_a, install_a))
            self.assertFalse(cb.is_protocol_command_owned(cmd_b, install_a))
            self.assertFalse(cb.is_protocol_command_owned("malformed 'unclosed", install_a))
            self.assertFalse(cb.is_protocol_command_owned("", install_a))

            if sys.platform == "win32":
                import winreg

                # Foreign protocol owner must NOT be deleted.
                with (
                    patch.object(cb, "get_protocol_status", return_value={
                        "registered": True,
                        "command": cmd_b,
                        "target_script": str(install_b),
                        "owned_by_current": False,
                    }),
                    patch.object(winreg, "DeleteKey") as mock_del,
                ):
                    res_skip = cb.unregister_protocol_handler(install_a)
                self.assertEqual(res_skip["status"], "skipped")
                self.assertFalse(res_skip["removed"])
                mock_del.assert_not_called()

                # Owned protocol removes cleanly and repeats idempotently.
                with (
                    patch.object(cb, "get_protocol_status", return_value={
                        "registered": True,
                        "command": cmd_a,
                        "target_script": str(install_a),
                        "owned_by_current": True,
                    }),
                    patch.object(winreg, "DeleteKey") as mock_del,
                ):
                    res_ok = cb.unregister_protocol_handler(install_a)
                self.assertEqual(res_ok["status"], "removed")
                self.assertTrue(res_ok["removed"])
                self.assertEqual(mock_del.call_count, 4)

                with patch.object(cb, "get_protocol_status", return_value={
                    "registered": False,
                    "command": None,
                    "target_script": None,
                    "owned_by_current": False,
                }):
                    res_absent = cb.unregister_protocol_handler(install_a)
                self.assertEqual(res_absent["status"], "absent")
                self.assertFalse(res_absent["removed"])

    def test_startup_launcher_ownership_and_idempotent_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            startup = root / "Startup"
            startup.mkdir()
            install_a = root / "Install A & (Special)" / "clipboard_bridge.py"
            install_b = root / "Install B" / "clipboard_bridge.py"
            install_a.parent.mkdir()
            install_b.parent.mkdir()
            install_a.write_text("# a", encoding="utf-8")
            install_b.write_text("# b", encoding="utf-8")

            vbs_a = cb.install_startup_launcher(startup, install_a, Path("C:/Python/pythonw.exe"))
            vbs_b = cb.install_startup_launcher(startup, install_b, Path("C:/Python/pythonw.exe"))
            legacy_vbs = startup / "ClipboardBridge_SilentStartup.vbs"
            legacy_vbs.write_text('WshShell.Run "python ""D:\\ClipboardBridge\\clipboard_bridge.py""", 0, False', encoding="utf-8")
            shared_foreign = startup / "ClipBridge_SilentStartup.vbs"
            shared_foreign.write_text(f'WshShell.Run "python ""{install_b}""", 0, False', encoding="utf-8")

            self.assertTrue(cb.is_startup_launcher_owned(vbs_a, install_a))
            self.assertFalse(cb.is_startup_launcher_owned(vbs_b, install_a))
            self.assertFalse(cb.is_startup_launcher_owned(legacy_vbs, install_a))
            self.assertFalse(cb.is_startup_launcher_owned(shared_foreign, install_a))

            res1 = cb.uninstall_startup_launchers(startup, install_a)
            self.assertEqual(res1["removed"], [vbs_a.name])
            self.assertFalse(vbs_a.exists())
            self.assertTrue(vbs_b.exists())
            self.assertTrue(legacy_vbs.exists())
            self.assertTrue(shared_foreign.exists())

            # Idempotent second uninstall
            res2 = cb.uninstall_startup_launchers(startup, install_a)
            self.assertEqual(res2["removed"], [])
            self.assertTrue(vbs_b.exists())
            self.assertTrue(legacy_vbs.exists())

    def test_firewall_rule_ownership_matching_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_a = root / "App & (1)" / "clipboard_bridge.py"
            install_b = root / "App B" / "clipboard_bridge.py"
            install_a.parent.mkdir()
            install_b.parent.mkdir()
            install_a.write_text("# a", encoding="utf-8")
            install_b.write_text("# b", encoding="utf-8")

            id_a = cb.compute_install_id(install_a)
            id_b = cb.compute_install_id(install_b)
            desc_a = cb.format_firewall_description(id_a, install_a)
            desc_b = cb.format_firewall_description(id_b, install_b)

            self.assertTrue(cb.parse_firewall_ownership(desc_a, id_a, install_a))
            self.assertFalse(cb.parse_firewall_ownership(desc_b, id_a, install_a))
            self.assertFalse(cb.parse_firewall_ownership("", id_a, install_a))
            self.assertFalse(cb.parse_firewall_ownership("Manual rule", id_a, install_a))

            # Inspection unavailable -> fail closed
            with patch.object(cb, "inspect_firewall_rules_windows", return_value=None):
                res_abort = cb.uninstall_owned_firewall_rules(5050, id_a, install_a)
            self.assertEqual(res_abort["status"], "aborted")
            self.assertEqual(res_abort["removed"], [])

            # Mixed rules: delete ONLY owned rule
            mock_rules = [
                {"Name": "{GUID-A}", "DisplayName": f"ClipBridge_5050_{id_a}", "Description": desc_a},
                {"Name": "{GUID-UNTAGGED}", "DisplayName": "ClipBridge_5050", "Description": ""},
                {"Name": "{GUID-B}", "DisplayName": f"ClipBridge_5050_{id_b}", "Description": desc_b},
            ]
            with (
                patch.object(cb, "inspect_firewall_rules_windows", return_value=mock_rules),
                patch.object(cb, "_delete_firewall_rule_instance", return_value=(True, "OK")) as mock_del,
            ):
                res_mix = cb.uninstall_owned_firewall_rules(5050, id_a, install_a)
            self.assertEqual(res_mix["status"], "ok")
            self.assertEqual(res_mix["removed"], [f"ClipBridge_5050_{id_a}"])
            self.assertEqual(len(res_mix["skipped"]), 2)
            mock_del.assert_called_once_with("{GUID-A}")

    def test_cli_cleanup_all_fail_closed_and_ownership_scenarios(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_a = root / "InstA" / "clipboard_bridge.py"
            script_a.parent.mkdir()
            script_a.write_text("# a", encoding="utf-8")
            id_a = cb.compute_install_id(script_a)
            id_b = cb.compute_install_id(root / "InstB" / "clipboard_bridge.py")

            temp_dir = root / "temp_a"
            staged = temp_dir / "staged"
            hist_dir = temp_dir / "history_files"
            hist = cb.TransferHistory(
                maxlen=5,
                staged_dir=staged,
                history_files_dir=hist_dir,
                temp_dir=temp_dir,
                install_id=id_a,
                script_path=script_a,
                get_clipboard_files_fn=lambda: [],
            )
            hist.ensure_storage_ownership()

            # Stage an older inactive transfer and an active clipboard transfer in live server
            with patch.object(cb, "HISTORY", hist), patch.object(cb, "SECRET_KEY", "test-cleanup-secret"):
                srv = cb.ClipBridgeHTTPServer(("127.0.0.1", 0), cb.ClipboardRequestHandler)
                live_port = srv.server_address[1]
                th = threading.Thread(target=srv.serve_forever, daemon=True)
                th.start()
                try:
                    t1, s1 = hist.begin_upload(3)
                    f1 = s1 / "old.txt"
                    f1.write_bytes(b"old")
                    tid1, _, _, _ = hist.commit_upload(t1, s1, [f1], "Phone -> PC", lambda _: True)
                    hist.release_lease(tid1)

                    t2, s2 = hist.begin_upload(3)
                    f2 = s2 / "live.txt"
                    f2.write_bytes(b"new")
                    tid2, paths2, _, _ = hist.commit_upload(t2, s2, [f2], "Phone -> PC", lambda _: True)
                    hist.release_lease(tid2)

                    # 1. Running server + wrong key -> HTTP 401 -> ABORT safely
                    res_401 = cb.run_cli_cleanup(force=False, history_store=hist, port=live_port, secret_key="wrong")
                    self.assertEqual(res_401["status"], "aborted")
                    self.assertEqual(res_401["reason"], "unauthorized")
                    self.assertTrue((staged / tid1).exists())
                    self.assertTrue(paths2[0].exists())

                    # 2. Running server + valid key -> delegates cleanly, preserves active clipboard
                    res_ok = cb.run_cli_cleanup(force=False, history_store=hist, port=live_port, secret_key="test-cleanup-secret")
                    self.assertEqual(res_ok["status"], "ok")
                    self.assertFalse((staged / tid1).exists())
                    self.assertTrue(paths2[0].exists())

                    # 3. Timeout -> ABORT safely
                    with patch.object(cb, "urlopen", side_effect=TimeoutError("timed out")):
                        res_to = cb.run_cli_cleanup(force=True, history_store=hist, port=live_port, secret_key="test-cleanup-secret")
                    self.assertEqual(res_to["status"], "aborted")
                    self.assertEqual(res_to["reason"], "timeout")
                    self.assertTrue(paths2[0].exists())
                finally:
                    srv.shutdown()
                    srv.server_close()

            # 4. Another HTTP service occupies port (returns 404) -> ABORT safely
            from http.server import BaseHTTPRequestHandler

            class _OtherHandler(BaseHTTPRequestHandler):
                def log_message(self, *_a):
                    pass

                def do_POST(self):
                    self.send_response(404)
                    self.end_headers()

            other_srv = cb.ClipBridgeHTTPServer(("127.0.0.1", 0), _OtherHandler)
            other_port = other_srv.server_address[1]
            other_th = threading.Thread(target=other_srv.serve_forever, daemon=True)
            other_th.start()
            try:
                res_other = cb.run_cli_cleanup(force=True, history_store=hist, port=other_port, secret_key="k")
                self.assertEqual(res_other["status"], "aborted")
                self.assertEqual(res_other["reason"], "unexpected_http_service")
            finally:
                other_srv.shutdown()
                other_srv.server_close()

            # Allocate a closed port
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                closed_port = probe.getsockname()[1]

            # 5. Port occupied at OS level even when loopback refused -> ABORT safely
            with patch.object(cb, "_is_port_listening", return_value=True):
                res_occ = cb.run_cli_cleanup(force=True, history_store=hist, port=closed_port, secret_key="k")
            self.assertEqual(res_occ["status"], "aborted")
            self.assertEqual(res_occ["reason"], "port_occupied")

            # 6. Process ownership cannot be verified -> ABORT safely
            with (
                patch.object(cb, "_is_port_listening", return_value=False),
                patch.object(cb, "_verify_no_running_clipbridge_process", return_value=(False, "Opaque Python process")),
            ):
                res_proc = cb.run_cli_cleanup(force=True, history_store=hist, port=closed_port, secret_key="k")
            self.assertEqual(res_proc["status"], "aborted")
            self.assertEqual(res_proc["reason"], "process_unverifiable")

            # 7. Server lock held -> ABORT safely
            lock_fh = cb.acquire_server_lock(temp_dir, closed_port, id_a, script_a)
            try:
                with (
                    patch.object(cb, "_is_port_listening", return_value=False),
                    patch.object(cb, "_verify_no_running_clipbridge_process", return_value=(True, "verified_stopped")),
                ):
                    res_lock = cb.run_cli_cleanup(force=True, history_store=hist, port=closed_port, secret_key="k")
                self.assertEqual(res_lock["status"], "aborted")
                self.assertEqual(res_lock["reason"], "server_lock_held")
            finally:
                cb.release_server_lock(lock_fh)

            # 8. Shared/foreign files inside staged & history_files are preserved even under force=True!
            foreign_tx = staged / f"tx_{id_b}_foreign_1"
            foreign_tx.mkdir(parents=True)
            (foreign_tx / "foreign.txt").write_bytes(b"keep-foreign")
            legacy_item = hist_dir / "item_legacy"
            legacy_item.mkdir(parents=True)
            (legacy_item / "legacy.bin").write_bytes(b"keep-legacy")
            owned_orphan = staged / ".staging_owned_abandoned"
            owned_orphan.mkdir(parents=True)
            (owned_orphan / "part.bin").write_bytes(b"reclaim-me")
            owned_tx = staged / f"tx_{id_a}_old_session_9"
            owned_tx.mkdir(parents=True)
            (owned_tx / "old.txt").write_bytes(b"reclaim-tx")

            with (
                patch.object(cb, "_is_port_listening", return_value=False),
                patch.object(cb, "_verify_no_running_clipbridge_process", return_value=(True, "verified_stopped")),
            ):
                res_offline = cb.run_cli_cleanup(force=True, history_store=hist, port=closed_port, secret_key="k")
            self.assertEqual(res_offline["status"], "ok")
            self.assertFalse(owned_orphan.exists())
            self.assertFalse(owned_tx.exists())
            self.assertTrue((foreign_tx / "foreign.txt").exists())
            self.assertTrue((legacy_item / "legacy.bin").exists())

            # 9. Force cleanup without verified ownership -> REFUSE destructive cleanup
            hist._owner_file.unlink()
            with (
                patch.object(cb, "_is_port_listening", return_value=False),
                patch.object(cb, "_verify_no_running_clipbridge_process", return_value=(True, "verified_stopped")),
            ):
                res_unowned = cb.run_cli_cleanup(force=True, history_store=hist, port=closed_port, secret_key="k")
            self.assertEqual(res_unowned["status"], "aborted")
            self.assertEqual(res_unowned["reason"], "ownership_unverified")
            self.assertTrue((foreign_tx / "foreign.txt").exists())
            self.assertTrue((legacy_item / "legacy.bin").exists())


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
