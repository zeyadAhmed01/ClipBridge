#!/usr/bin/env python3
"""
ClipBridge — Lightweight Bidirectional Windows <-> Android Clipboard, Screenshot & File Bridge
Author  : Zeyad Ahmed
License : MIT
Version : 1.0.0

Zero third-party Python dependencies. Uses native Win32 ctypes for instant (<1ms) text
clipboard access and one-shot PowerShell (-EncodedCommand with timeouts) for images,
FileDropList, and Windows 10/11 Toast Notifications.
"""

import base64
from collections import deque
import ctypes
from ctypes import wintypes
from datetime import datetime
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

VERSION = "1.0.0"
APP_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = Path(__file__).resolve()
CONFIG_PATH = APP_DIR / "config.json"


def canonical_script_path(path: Path | None = None) -> str:
    target = (path or SCRIPT_PATH).resolve()
    return os.path.normcase(str(target))


def compute_install_id(path: Path | None = None) -> str:
    return hashlib.sha256(canonical_script_path(path).encode("utf-8")).hexdigest()[:12]


INSTALL_ID = compute_install_id(SCRIPT_PATH)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_TEXT_BYTES = 2 * 1024 * 1024  # 2 MB cap for text payloads
MAX_CONCURRENT_UPLOADS = 16
MAX_FILES_PER_UPLOAD = 100
REQUEST_TIMEOUT_SEC = 60


class IncompleteUploadError(ValueError):
    """Raised when an upload stream ends before the full payload or closing boundary."""


class UploadLimitError(ValueError):
    """Raised when an upload exceeds structural limits (e.g., too many parts)."""


class UploadBusyError(RuntimeError):
    """Raised when concurrent inflight uploads exceed capacity or storage budget."""


class StorageOwnershipError(PermissionError):
    """Raised when a temporary storage directory belongs to a different installation."""


def _safe_int(value, default: int, min_val: int, max_val: int | None = None) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        num = default
    if num < min_val:
        num = min_val
    if max_val is not None and num > max_val:
        num = max_val
    return num


# ==============================================================================
# AUTO-CONFIGURATION (config.json)
# ==============================================================================
def load_or_create_config(read_only: bool = False) -> dict:
    default_downloads = str(Path.home() / "Downloads")
    default_cfg = {
        "port": 5050,
        "secret_key": f"clip-{secrets.token_hex(16)}",
        "save_folder": default_downloads,
        "max_history_items": 25,
        "max_upload_mb": 512,
        "max_storage_mb": 1024,
        "retention_minutes": 60,
    }
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                loaded = {}
            merged = {**default_cfg, **loaded}
            key_val = str(merged.get("secret_key") or "").strip()
            if not key_val or key_val == "replace-with-your-secret-key":
                merged["secret_key"] = f"clip-{secrets.token_hex(16)}"
            if not read_only and merged != loaded:
                CONFIG_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
            return merged
        except Exception:
            pass

    if not read_only:
        try:
            CONFIG_PATH.write_text(json.dumps(default_cfg, indent=2), encoding="utf-8")
        except Exception:
            pass
    return default_cfg


def _has_persisted_config(config_path: Path | None = None) -> bool:
    target = config_path or CONFIG_PATH
    if not target.is_file():
        return False
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        key_val = str(data.get("secret_key") or "").strip()
        return bool(key_val and key_val != "replace-with-your-secret-key")
    except Exception:
        return False


def ensure_config_persisted(config_path: Path | None = None) -> dict:
    target = config_path or CONFIG_PATH
    if _has_persisted_config(target):
        return CONFIG
    payload = {
        "port": PORT,
        "secret_key": SECRET_KEY,
        "save_folder": str(SAVE_FOLDER),
        "max_history_items": MAX_HISTORY,
        "max_upload_mb": max(1, int(MAX_UPLOAD_BYTES // (1024 * 1024))),
        "max_storage_mb": max(1, int(MAX_STORAGE_BYTES // (1024 * 1024))),
        "retention_minutes": max(1, int(RETENTION_SECONDS // 60)),
    }
    try:
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass
    return payload


# Never write config.json as a module-import or read-only CLI side effect;
# ensure_config_persisted() writes config.json only during --register-only or server startup.
CONFIG = load_or_create_config(read_only=True)
PORT = _safe_int(CONFIG.get("port"), 5050, 1, 65535)
SECRET_KEY = str(CONFIG.get("secret_key") or "").strip() or f"clip-{secrets.token_hex(16)}"
SAVE_FOLDER = Path(CONFIG.get("save_folder") or str(Path.home() / "Downloads"))
MAX_HISTORY = _safe_int(CONFIG.get("max_history_items"), 25, 1)
MAX_UPLOAD_BYTES = _safe_int(CONFIG.get("max_upload_mb"), 512, 1) * 1024 * 1024
MAX_STORAGE_BYTES = _safe_int(CONFIG.get("max_storage_mb"), 1024, 1) * 1024 * 1024
RETENTION_SECONDS = _safe_int(CONFIG.get("retention_minutes"), 60, 1) * 60

SHARED_TEMP_ROOT = Path(tempfile.gettempdir()) / "ClipBridge"
TEMP_DIR = SHARED_TEMP_ROOT / "installs" / INSTALL_ID
STAGED_DIR = TEMP_DIR / "staged"
HISTORY_FILES_DIR = TEMP_DIR / "history_files"
PC_CLIPBOARD_IMAGE_PATH = TEMP_DIR / "pc_clipboard.png"
LATEST_TRANSFER_FILE = TEMP_DIR / "latest_transfer.json"
STORAGE_OWNER_FILE = TEMP_DIR / ".owner.json"
SERVER_LOCK_FILE = TEMP_DIR / ".server.lock"


def _is_strict_subpath(path: Path, root: Path) -> bool:
    try:
        p_res = path.resolve()
        r_res = root.resolve()
        return p_res != r_res and p_res.is_relative_to(r_res)
    except Exception:
        return False


def _safe_rmtree(path: Path, allowed_roots: tuple[Path, ...] | list[Path]) -> bool:
    if not any(_is_strict_subpath(path, root) for root in allowed_roots):
        return False
    if not path.exists() and not path.is_symlink():
        return True
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)
        return not path.exists()
    except Exception:
        return False


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        if path.is_file() and not path.is_symlink():
            return path.stat().st_size
        if not path.is_dir() or path.is_symlink():
            return 0
        for root, _, files in os.walk(path):
            for fname in files:
                fpath = Path(root) / fname
                try:
                    if not fpath.is_symlink():
                        total += fpath.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _atomic_write_json(target: Path, payload: dict):
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


# ==============================================================================
# BOUNDED TRANSFER STORAGE & SESSION HISTORY MANAGER
# ==============================================================================
class TransferHistory:
    def __init__(
        self,
        maxlen: int = MAX_HISTORY,
        staged_dir: Path | None = None,
        history_files_dir: Path | None = None,
        temp_dir: Path | None = None,
        max_storage_bytes: int | None = None,
        retention_seconds: float | None = None,
        get_clipboard_seq_fn=None,
        get_clipboard_files_fn=None,
        install_id: str | None = None,
        script_path: Path | None = None,
    ):
        self.lock = threading.RLock()
        self.commit_lock = threading.Lock()
        self.maxlen = max(1, int(maxlen))
        self.script_path = (script_path or SCRIPT_PATH).resolve()
        self.install_id = install_id or compute_install_id(self.script_path)
        self.staged_dir = staged_dir or STAGED_DIR
        self.history_files_dir = history_files_dir or HISTORY_FILES_DIR
        self.temp_dir = temp_dir or TEMP_DIR
        self.max_storage_bytes = max(1, int(max_storage_bytes if max_storage_bytes is not None else MAX_STORAGE_BYTES))
        self.retention_seconds = max(1.0, float(retention_seconds if retention_seconds is not None else RETENTION_SECONDS))
        self._get_clipboard_seq_fn = get_clipboard_seq_fn
        self._get_clipboard_files_fn = get_clipboard_files_fn
        self._latest_transfer_file = self.temp_dir / "latest_transfer.json"
        self._owner_file = self.temp_dir / ".owner.json"
        self._server_lock_file = self.temp_dir / ".server.lock"

        self.session_id = f"{self.install_id}_{int(time.time())}_{secrets.token_hex(4)}"
        self.items: deque[dict] = deque()
        self._counter = 0
        self._seq = 0
        self._transfers: dict[str, dict] = {}
        self._active_clipboard_tid: str | None = None
        self._inflight_uploads: dict[str, int] = {}

    def verify_storage_ownership(self) -> tuple[bool, str]:
        if not self.temp_dir.is_dir():
            return False, "temporary storage directory does not exist"
        if not self._owner_file.is_file():
            return False, "missing .owner.json manifest"
        try:
            data = json.loads(self._owner_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False, "invalid .owner.json manifest"
            owner_id = str(data.get("install_id", "")).strip()
            owner_path = os.path.normcase(str(data.get("script_path", "")).strip())
            expected_path = canonical_script_path(self.script_path)
            if owner_id != self.install_id or owner_path != expected_path:
                return False, f"storage belongs to another installation ({data.get('script_path', 'unknown')})"
            return True, "verified"
        except Exception as exc:
            return False, f"unable to read .owner.json ({exc})"

    def ensure_storage_ownership(self):
        if self._owner_file.exists():
            ok, reason = self.verify_storage_ownership()
            if not ok:
                raise StorageOwnershipError(f"Refusing to claim storage: {reason}")
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.staged_dir.mkdir(parents=True, exist_ok=True)
        self.history_files_dir.mkdir(parents=True, exist_ok=True)
        if not self._owner_file.exists():
            _atomic_write_json(
                self._owner_file,
                {
                    "install_id": self.install_id,
                    "script_path": canonical_script_path(self.script_path),
                    "version": VERSION,
                },
            )

    @staticmethod
    def _is_foreign_entry(child: Path, own_install_id: str, own_script_path: Path) -> bool:
        if child.is_dir() and (child / ".owner.json").is_file():
            try:
                data = json.loads((child / ".owner.json").read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return True
                cid = str(data.get("install_id", "")).strip()
                cpath = os.path.normcase(str(data.get("script_path", "")).strip())
                if cid != own_install_id or cpath != canonical_script_path(own_script_path):
                    return True
            except Exception:
                return True
        m = re.match(r"^(?:tx|s)_([0-9a-f]{12})_", child.name)
        if m and m.group(1) != own_install_id:
            return True
        return False

    def _current_clipboard_seq(self) -> int | None:
        if self._get_clipboard_seq_fn is not None:
            return self._get_clipboard_seq_fn()
        return win32_get_clipboard_sequence_number()

    def _current_clipboard_files(self) -> list[Path]:
        if self._get_clipboard_files_fn is not None:
            return self._get_clipboard_files_fn()
        return win32_get_clipboard_files()

    def _is_transfer_protected_locked(self, rec: dict) -> tuple[bool, str]:
        if rec["leases"] > 0:
            return True, "active_operation"
        if rec["transfer_id"] == self._active_clipboard_tid and rec.get("pinned_clipboard"):
            expected_seq = rec.get("clipboard_seq")
            if expected_seq is not None:
                cur_seq = self._current_clipboard_seq()
                if cur_seq is not None and cur_seq != expected_seq:
                    rec["pinned_clipboard"] = False
                    self._active_clipboard_tid = None
                    return False, ""
            return True, "active_clipboard"
        return False, ""

    def _remove_entry_by_transfer_id_locked(self, tid: str):
        self.items = deque(item for item in self.items if item.get("transfer_id") != tid)

    def _delete_transfer_locked(self, tid: str) -> int:
        rec = self._transfers.get(tid)
        if not rec:
            return 0
        freed = rec["bytes"]
        _safe_rmtree(rec["dir"], (self.staged_dir, self.history_files_dir))
        self._transfers.pop(tid, None)
        if self._active_clipboard_tid == tid:
            self._active_clipboard_tid = None
        self._remove_entry_by_transfer_id_locked(tid)
        self._sync_latest_pointer_if_removed_locked(tid)
        return freed

    def _sync_latest_pointer_if_removed_locked(self, removed_tid: str):
        if not self._latest_transfer_file.exists():
            return
        try:
            data = json.loads(self._latest_transfer_file.read_text(encoding="utf-8"))
            if data.get("transfer_id") == removed_tid:
                self._latest_transfer_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _write_latest_pointer_locked(self, tid: str, tx_dir: Path, files: list[Path], created_at: float):
        _atomic_write_json(
            self._latest_transfer_file,
            {
                "transfer_id": tid,
                "dir": str(tx_dir.resolve()),
                "files": [str(p.resolve()) for p in files],
                "created_at": created_at,
            },
        )

    def _cleanup_locked(self, force: bool = False, clear_all_inactive: bool = False, now: float | None = None) -> dict:
        now_ts = time.time() if now is None else float(now)
        reclaimed_bytes = 0
        reclaimed_count = 0

        # Refresh active clipboard pin before evaluating protection.
        if self._active_clipboard_tid and self._active_clipboard_tid in self._transfers:
            self._is_transfer_protected_locked(self._transfers[self._active_clipboard_tid])

        # 1. Evict history items beyond maxlen (tracking their transfer_id for disk deletion).
        while len(self.items) > self.maxlen:
            old_item = self.items.pop()
            old_tid = old_item.get("transfer_id")
            if old_tid and old_tid in self._transfers:
                self._transfers[old_tid]["evicted_from_history"] = True

        # 2. Remove any already-evicted transfers that are no longer protected.
        for tid in list(self._transfers.keys()):
            rec = self._transfers.get(tid)
            if not rec or not rec.get("evicted_from_history"):
                continue
            protected, _ = self._is_transfer_protected_locked(rec)
            if not protected or (force and rec["leases"] == 0):
                reclaimed_bytes += self._delete_transfer_locked(tid)
                reclaimed_count += 1

        # 3. Expire old text items and expired or inactive disk-backed transfers.
        kept_items: deque[dict] = deque()
        for item in self.items:
            age = now_ts - float(item.get("created_at", now_ts))
            if age >= self.retention_seconds and not item.get("transfer_id"):
                continue
            kept_items.append(item)
        self.items = kept_items

        ordered = sorted(
            self._transfers.values(),
            key=lambda r: (0 if r.get("evicted_from_history") else 1, r["created_at"], r["seq"]),
        )
        for rec in ordered:
            tid = rec["transfer_id"]
            if tid not in self._transfers:
                continue
            age = now_ts - rec["created_at"]
            should_remove = clear_all_inactive or (age >= self.retention_seconds)
            if not should_remove:
                continue
            protected, _ = self._is_transfer_protected_locked(rec)
            if not protected or (force and rec["leases"] == 0):
                reclaimed_bytes += self._delete_transfer_locked(tid)
                reclaimed_count += 1

        # 4. Enforce total on-disk quota across managed staged + history directories.
        total_bytes = sum(r["bytes"] for r in self._transfers.values())
        if total_bytes > self.max_storage_bytes:
            ordered = sorted(
                self._transfers.values(),
                key=lambda r: (0 if r.get("evicted_from_history") else 1, r["created_at"], r["seq"]),
            )
            for rec in ordered:
                if total_bytes <= self.max_storage_bytes:
                    break
                tid = rec["transfer_id"]
                if tid not in self._transfers:
                    continue
                protected, _ = self._is_transfer_protected_locked(rec)
                if protected and not (force and rec["leases"] == 0):
                    continue
                freed = self._delete_transfer_locked(tid)
                reclaimed_bytes += freed
                reclaimed_count += 1
                total_bytes -= freed

        preserved_items = []
        for rec in self._transfers.values():
            protected, reason = self._is_transfer_protected_locked(rec)
            if protected:
                preserved_items.append(
                    {
                        "transfer_id": rec["transfer_id"],
                        "reason": reason,
                        "files": [p.name for p in rec["files"]],
                        "bytes": rec["bytes"],
                    }
                )

        warning = ""
        if preserved_items and (clear_all_inactive or force):
            warning = (
                f"Preserved {len(preserved_items)} active transfer(s) still referenced by "
                "the Windows clipboard or active operations."
            )

        return {
            "reclaimed_bytes": reclaimed_bytes,
            "reclaimed_human": format_size(reclaimed_bytes),
            "reclaimed_count": reclaimed_count,
            "preserved_active_count": len(preserved_items),
            "preserved_items": preserved_items,
            "warning": warning,
        }

    def begin_upload(self, content_length: int) -> tuple[str, Path]:
        with self.lock:
            self.ensure_storage_ownership()
            if len(self._inflight_uploads) >= MAX_CONCURRENT_UPLOADS:
                raise UploadBusyError("Too many concurrent uploads in progress")
            inflight_total = sum(self._inflight_uploads.values())
            if inflight_total + content_length > self.max_storage_bytes:
                raise UploadBusyError("Concurrent upload reservation exceeds storage budget")
            token = f"{int(time.time() * 1000)}_{secrets.token_hex(6)}"
            staging_dir = self.staged_dir / f".staging_{token}"
            staging_dir.mkdir(parents=True, exist_ok=True)
            self._inflight_uploads[token] = content_length
            return token, staging_dir

    def abort_upload(self, token: str, staging_dir: Path | None):
        if staging_dir is not None:
            _safe_rmtree(staging_dir, (self.staged_dir,))
        with self.lock:
            self._inflight_uploads.pop(token, None)

    def acquire_lease(self, transfer_id: str | None) -> bool:
        if not transfer_id:
            return False
        with self.lock:
            rec = self._transfers.get(transfer_id)
            if not rec:
                return False
            rec["leases"] += 1
            return True

    def release_lease(self, transfer_id: str | None):
        if not transfer_id:
            return
        with self.lock:
            rec = self._transfers.get(transfer_id)
            if rec and rec["leases"] > 0:
                rec["leases"] -= 1
            self._cleanup_locked(force=False)

    def mark_clipboard_replaced(self):
        with self.lock:
            if self._active_clipboard_tid and self._active_clipboard_tid in self._transfers:
                self._transfers[self._active_clipboard_tid]["pinned_clipboard"] = False
            self._active_clipboard_tid = None
            self._cleanup_locked(force=False)

    def commit_upload(
        self,
        token: str,
        staging_dir: Path,
        staged_paths: list[Path],
        direction: str,
        set_clipboard_fn,
    ) -> tuple[str, list[Path], dict, bool]:
        """Atomically promote an isolated upload dir, set clipboard, and record bounded history."""
        with self.commit_lock:
            with self.lock:
                self._seq += 1
                self._counter += 1
                seq = self._seq
                entry_id = self._counter
                tx_id = f"tx_{self.session_id}_{seq}_{token}"
                tx_dir = self.staged_dir / tx_id
                self._inflight_uploads.pop(token, None)

            try:
                staging_dir.rename(tx_dir)
            except Exception:
                shutil.move(str(staging_dir), str(tx_dir))

            tx_paths = [tx_dir / p.name for p in staged_paths if (tx_dir / p.name).is_file()]
            now_ts = time.time()
            tx_bytes = _dir_size_bytes(tx_dir)

            with self.lock:
                self._transfers[tx_id] = {
                    "transfer_id": tx_id,
                    "dir": tx_dir,
                    "kind": "staged",
                    "bytes": tx_bytes,
                    "created_at": now_ts,
                    "seq": seq,
                    "history_entry_id": entry_id,
                    "leases": 1,  # Held across clipboard write + caller toast/response setup.
                    "pinned_clipboard": False,
                    "clipboard_seq": None,
                    "evicted_from_history": False,
                    "files": tx_paths,
                }

            ok = False
            try:
                ok = bool(set_clipboard_fn(tx_paths))
            except Exception:
                # Even unexpected clipboard adapter failures must not leave a permanent lease.
                # Keep the staged files for the normal retention window in case the OS
                # partially accepted the transfer before the adapter raised.
                with self.lock:
                    rec = self._transfers.get(tx_id)
                    if rec:
                        rec["leases"] = max(0, rec["leases"] - 1)
                raise
            finally:
                with self.lock:
                    rec = self._transfers.get(tx_id)
                    if rec:
                        if ok:
                            if self._active_clipboard_tid and self._active_clipboard_tid in self._transfers:
                                self._transfers[self._active_clipboard_tid]["pinned_clipboard"] = False
                            self._active_clipboard_tid = tx_id
                            rec["pinned_clipboard"] = True
                            rec["clipboard_seq"] = self._current_clipboard_seq()

                        self._write_latest_pointer_locked(tx_id, tx_dir, tx_paths, now_ts)
                        saved_records = []
                        for idx, p in enumerate(tx_paths):
                            size_b = p.stat().st_size if p.exists() else 0
                            saved_records.append(
                                {
                                    "index": idx,
                                    "name": p.name,
                                    "path": str(p),
                                    "size": size_b,
                                    "size_human": format_size(size_b),
                                    "is_image": p.suffix.lower() in IMAGE_EXTENSIONS,
                                    "url": f"/api/history-file/{entry_id}/{idx}?key={SECRET_KEY}",
                                }
                            )
                        entry = {
                            "id": entry_id,
                            "transfer_id": tx_id,
                            "type": "files",
                            "direction": direction,
                            "timestamp": datetime.now().strftime("%H:%M:%S"),
                            "created_at": now_ts,
                            "files": saved_records,
                        }
                        self.items.appendleft(entry)
                        self._cleanup_locked(force=False, now=now_ts)

            return tx_id, tx_paths, entry, ok

    def add_text(self, text: str, direction: str) -> dict:
        with self.lock:
            self._counter += 1
            now_ts = time.time()
            entry = {
                "id": self._counter,
                "type": "text",
                "direction": direction,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "created_at": now_ts,
                "preview": text[:160] + ("..." if len(text) > 160 else ""),
                "content": text,
            }
            self.items.appendleft(entry)
            self._cleanup_locked(force=False, now=now_ts)
            return entry

    def add_files(self, file_paths: list[Path], direction: str) -> dict:
        valid_sources = [src for src in file_paths if src.is_file()]
        total_src_bytes = 0
        for src in valid_sources:
            try:
                total_src_bytes += src.stat().st_size
            except OSError:
                pass

        with self.lock:
            self.ensure_storage_ownership()
            self._counter += 1
            self._seq += 1
            entry_id = self._counter
            seq = self._seq
            now_ts = time.time()

            saved_records = []
            tid: str | None = None
            if valid_sources and total_src_bytes <= self.max_storage_bytes:
                tid = f"hist_{self.session_id}_{entry_id}"
                entry_dir = self.history_files_dir / f"s_{self.session_id}_item_{entry_id}"
                entry_dir.mkdir(parents=True, exist_ok=True)
                copied_paths: list[Path] = []
                for idx, src in enumerate(valid_sources):
                    safe_name = sanitize_filename(src.name)
                    dst = entry_dir / safe_name
                    counter = 1
                    while dst.exists():
                        dst = entry_dir / f"{Path(safe_name).stem}_{counter}{Path(safe_name).suffix}"
                        counter += 1
                    try:
                        shutil.copy2(src, dst)
                        copied_paths.append(dst)
                        size_b = dst.stat().st_size if dst.exists() else 0
                        saved_records.append(
                            {
                                "index": idx,
                                "name": dst.name,
                                "path": str(dst),
                                "size": size_b,
                                "size_human": format_size(size_b),
                                "is_image": dst.suffix.lower() in IMAGE_EXTENSIONS,
                                "url": f"/api/history-file/{entry_id}/{idx}?key={SECRET_KEY}",
                            }
                        )
                    except Exception:
                        pass

                if copied_paths:
                    self._transfers[tid] = {
                        "transfer_id": tid,
                        "dir": entry_dir,
                        "kind": "history",
                        "bytes": _dir_size_bytes(entry_dir),
                        "created_at": now_ts,
                        "seq": seq,
                        "history_entry_id": entry_id,
                        "leases": 0,
                        "pinned_clipboard": False,
                        "clipboard_seq": None,
                        "evicted_from_history": False,
                        "files": copied_paths,
                    }
                else:
                    _safe_rmtree(entry_dir, (self.history_files_dir,))
                    tid = None

            entry = {
                "id": entry_id,
                "transfer_id": tid,
                "type": "files",
                "direction": direction,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "created_at": now_ts,
                "files": saved_records,
            }
            self.items.appendleft(entry)
            self._cleanup_locked(force=False, now=now_ts)
            return entry

    def add_image_bytes(self, filename: str, data: bytes, direction: str) -> dict:
        with self.lock:
            self.ensure_storage_ownership()
            self._counter += 1
            self._seq += 1
            entry_id = self._counter
            seq = self._seq
            now_ts = time.time()
            saved_records = []
            tid: str | None = None

            if data and len(data) <= self.max_storage_bytes:
                tid = f"hist_{self.session_id}_{entry_id}"
                entry_dir = self.history_files_dir / f"s_{self.session_id}_item_{entry_id}"
                entry_dir.mkdir(parents=True, exist_ok=True)
                dst = entry_dir / sanitize_filename(filename, ".png")
                dst.write_bytes(data)
                self._transfers[tid] = {
                    "transfer_id": tid,
                    "dir": entry_dir,
                    "kind": "history",
                    "bytes": len(data),
                    "created_at": now_ts,
                    "seq": seq,
                    "history_entry_id": entry_id,
                    "leases": 0,
                    "pinned_clipboard": False,
                    "clipboard_seq": None,
                    "evicted_from_history": False,
                    "files": [dst],
                }
                saved_records.append(
                    {
                        "index": 0,
                        "name": dst.name,
                        "path": str(dst),
                        "size": len(data),
                        "size_human": format_size(len(data)),
                        "is_image": dst.suffix.lower() in IMAGE_EXTENSIONS,
                        "url": f"/api/history-file/{entry_id}/0?key={SECRET_KEY}",
                    }
                )

            entry = {
                "id": entry_id,
                "transfer_id": tid,
                "type": "files",
                "direction": direction,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "created_at": now_ts,
                "files": saved_records,
            }
            self.items.appendleft(entry)
            self._cleanup_locked(force=False, now=now_ts)
            return entry

    def list_items(self) -> list[dict]:
        with self.lock:
            self._cleanup_locked(force=False)
            out = []
            for item in self.items:
                tid = item.get("transfer_id")
                protected = False
                if tid and tid in self._transfers:
                    protected, _ = self._is_transfer_protected_locked(self._transfers[tid])
                copy_item = {
                    k: v
                    for k, v in item.items()
                    if k not in {"transfer_id", "created_at"}
                }
                copy_item["protected"] = protected
                if copy_item["type"] == "files":
                    copy_item["files"] = [
                        {k: v for k, v in f.items() if k != "path"}
                        for f in item["files"]
                    ]
                out.append(copy_item)
            return out

    def get_file_path(self, entry_id: int, file_idx: int) -> Path | None:
        path, tid = self.acquire_file_for_read(entry_id, file_idx)
        if tid:
            self.release_lease(tid)
        return path

    def acquire_file_for_read(self, entry_id: int, file_idx: int) -> tuple[Path | None, str | None]:
        with self.lock:
            for item in self.items:
                if item["id"] == entry_id and item["type"] == "files":
                    for f in item["files"]:
                        if f["index"] == file_idx:
                            p = Path(f["path"])
                            if not p.is_file():
                                return None, None
                            if not (
                                _is_strict_subpath(p, self.staged_dir)
                                or _is_strict_subpath(p, self.history_files_dir)
                            ):
                                return None, None
                            tid = item.get("transfer_id")
                            if tid and tid in self._transfers:
                                self._transfers[tid]["leases"] += 1
                            return p, tid
            return None, None

    def acquire_latest_staged_files(self) -> tuple[str | None, list[Path]]:
        with self.lock:
            if self._latest_transfer_file.exists():
                try:
                    data = json.loads(self._latest_transfer_file.read_text(encoding="utf-8"))
                    tid = data.get("transfer_id")
                    if tid and tid in self._transfers:
                        rec = self._transfers[tid]
                        files = [p for p in rec["files"] if p.is_file()]
                        if files:
                            rec["leases"] += 1
                            return tid, files
                except Exception:
                    pass
            staged_recs = sorted(
                (r for r in self._transfers.values() if r["kind"] == "staged"),
                key=lambda r: (r["created_at"], r["seq"]),
                reverse=True,
            )
            for rec in staged_recs:
                files = [p for p in rec["files"] if p.is_file()]
                if files:
                    rec["leases"] += 1
                    return rec["transfer_id"], files
            return None, []

    def get_storage_status(self) -> dict:
        with self.lock:
            self._cleanup_locked(force=False)
            used_bytes = sum(r["bytes"] for r in self._transfers.values())
            protected_items = []
            reclaimable_bytes = 0
            for rec in self._transfers.values():
                protected, reason = self._is_transfer_protected_locked(rec)
                if protected:
                    protected_items.append(
                        {
                            "transfer_id": rec["transfer_id"],
                            "reason": reason,
                            "files": [p.name for p in rec["files"]],
                            "bytes": rec["bytes"],
                        }
                    )
                else:
                    reclaimable_bytes += rec["bytes"]
            return {
                "status": "ok",
                "used_bytes": used_bytes,
                "used_human": format_size(used_bytes),
                "quota_bytes": self.max_storage_bytes,
                "quota_human": format_size(self.max_storage_bytes),
                "retention_minutes": max(1, int(round(self.retention_seconds / 60))),
                "staged_transfers": sum(1 for r in self._transfers.values() if r["kind"] == "staged"),
                "history_items": len(self.items),
                "protected_items": protected_items,
                "reclaimable_bytes": reclaimable_bytes,
                "reclaimable_human": format_size(reclaimable_bytes),
            }

    def cleanup(self, force: bool = False, reclaim_inactive: bool = False, now: float | None = None) -> dict:
        with self.lock:
            return self._cleanup_locked(force=force, clear_all_inactive=reclaim_inactive, now=now)

    def clear(self, preserve_active: bool = False) -> dict:
        with self.lock:
            if not preserve_active:
                self.items.clear()
                reclaimed = 0
                count = 0
                for tid in list(self._transfers.keys()):
                    rec = self._transfers[tid]
                    if rec["leases"] > 0:
                        rec["evicted_from_history"] = True
                    else:
                        reclaimed += self._delete_transfer_locked(tid)
                        count += 1
                self._active_clipboard_tid = None
                try:
                    self._latest_transfer_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return {
                    "status": "ok",
                    "reclaimed_bytes": reclaimed,
                    "reclaimed_human": format_size(reclaimed),
                    "reclaimed_count": count,
                    "preserved_active_count": 0,
                    "preserved_items": [],
                    "warning": "",
                }

            # Non-destructive clear: drop text entries and inactive file entries,
            # keeping files still actively needed by Windows clipboard or open leases.
            new_items: deque[dict] = deque()
            preserved_tids: set[str] = set()
            for item in self.items:
                tid = item.get("transfer_id")
                if tid and tid in self._transfers:
                    protected, _ = self._is_transfer_protected_locked(self._transfers[tid])
                    if protected:
                        new_items.append(item)
                        preserved_tids.add(tid)
            self.items = new_items

            reclaimed = 0
            count = 0
            preserved_items = []
            for tid in list(self._transfers.keys()):
                rec = self._transfers[tid]
                protected, reason = self._is_transfer_protected_locked(rec)
                if protected or tid in preserved_tids:
                    preserved_items.append(
                        {
                            "transfer_id": tid,
                            "reason": reason,
                            "files": [p.name for p in rec["files"]],
                            "bytes": rec["bytes"],
                        }
                    )
                else:
                    reclaimed += self._delete_transfer_locked(tid)
                    count += 1

            warning = ""
            if preserved_items:
                names = ", ".join(
                    fname for item in preserved_items for fname in item["files"][:2]
                )
                warning = (
                    f"Kept {len(preserved_items)} active transfer(s) ({names}) still referenced "
                    "by the Windows clipboard or active operations."
                )
            return {
                "status": "ok",
                "reclaimed_bytes": reclaimed,
                "reclaimed_human": format_size(reclaimed),
                "reclaimed_count": count,
                "preserved_active_count": len(preserved_items),
                "preserved_items": preserved_items,
                "warning": warning,
            }

    def cleanup_orphans_on_startup(self, force: bool = False, require_verified_owner: bool = False) -> dict:
        """
        Reclaim orphaned staged/history files left on disk by earlier runs without
        touching foreign installations, anything outside TEMP_DIR, or live Windows CF_HDROP files.
        """
        with self.lock:
            if self._owner_file.exists():
                ok_owner, owner_reason = self.verify_storage_ownership()
                if not ok_owner:
                    return {
                        "status": "aborted",
                        "ok": False,
                        "reason": "ownership_mismatch",
                        "reclaimed_bytes": 0,
                        "reclaimed_human": "0 B",
                        "reclaimed_count": 0,
                        "preserved_active_count": 0,
                        "preserved_foreign_count": 0,
                        "preserved_files": [],
                        "warning": f"Refused cleanup: {owner_reason}.",
                    }
            elif require_verified_owner:
                return {
                    "status": "aborted",
                    "ok": False,
                    "reason": "ownership_unverified",
                    "reclaimed_bytes": 0,
                    "reclaimed_human": "0 B",
                    "reclaimed_count": 0,
                    "preserved_active_count": 0,
                    "preserved_foreign_count": 0,
                    "preserved_files": [],
                    "warning": "Refused cleanup: storage ownership cannot be verified (missing .owner.json).",
                }

        clipboard_files = set()
        if not force:
            for p in self._current_clipboard_files():
                try:
                    clipboard_files.add(p.resolve())
                except Exception:
                    pass

        reclaimed_bytes = 0
        reclaimed_count = 0
        preserved_foreign_count = 0
        adopted_files: list[str] = []
        owned_hist_prefix = f"s_{self.install_id}_"
        owned_tx_prefix = f"tx_{self.install_id}_"

        with self.lock:
            # Clean previous-session history_files directories owned by this installation.
            if self.history_files_dir.exists():
                session_prefix = f"s_{self.session_id}_"
                for child in list(self.history_files_dir.iterdir()):
                    if child.name.startswith(session_prefix):
                        continue
                    if self._is_foreign_entry(child, self.install_id, self.script_path):
                        preserved_foreign_count += 1
                        continue
                    if require_verified_owner and not child.name.startswith(owned_hist_prefix):
                        preserved_foreign_count += 1
                        continue
                    reclaimed_bytes += _dir_size_bytes(child)
                    if _safe_rmtree(child, (self.history_files_dir,)):
                        reclaimed_count += 1

            # Clean orphaned/in-flight staged entries; adopt live clipboard transfer if present.
            adopted_tid: str | None = None
            if self.staged_dir.exists():
                for child in list(self.staged_dir.iterdir()):
                    if child.name.startswith(f"tx_{self.session_id}_"):
                        continue
                    if self._is_foreign_entry(child, self.install_id, self.script_path):
                        preserved_foreign_count += 1
                        continue
                    if child.name.startswith(".staging_"):
                        reclaimed_bytes += _dir_size_bytes(child)
                        if _safe_rmtree(child, (self.staged_dir,)):
                            reclaimed_count += 1
                        continue
                    if require_verified_owner and not child.name.startswith(owned_tx_prefix):
                        preserved_foreign_count += 1
                        continue

                    child_files: list[Path] = []
                    if child.is_dir():
                        child_files = [p for p in sorted(child.iterdir()) if p.is_file() and not p.is_symlink()]
                    elif child.is_file() and not child.is_symlink():
                        child_files = [child]

                    referenced = False
                    if not force and clipboard_files and child_files:
                        for f in child_files:
                            try:
                                if f.resolve() in clipboard_files:
                                    referenced = True
                                    break
                            except Exception:
                                pass

                    if referenced and child.is_dir():
                        tid = child.name
                        if tid not in self._transfers:
                            self._seq += 1
                            now_ts = time.time()
                            self._transfers[tid] = {
                                "transfer_id": tid,
                                "dir": child,
                                "kind": "staged",
                                "bytes": _dir_size_bytes(child),
                                "created_at": now_ts,
                                "seq": self._seq,
                                "history_entry_id": None,
                                "leases": 0,
                                "pinned_clipboard": True,
                                "clipboard_seq": self._current_clipboard_seq(),
                                "evicted_from_history": False,
                                "files": child_files,
                            }
                            adopted_tid = tid
                            self._active_clipboard_tid = tid
                            self._write_latest_pointer_locked(tid, child, child_files, now_ts)
                            adopted_files.extend(p.name for p in child_files)
                    else:
                        reclaimed_bytes += _dir_size_bytes(child)
                        if _safe_rmtree(child, (self.staged_dir,)):
                            reclaimed_count += 1

            # Clean stale temporary pc_clipboard_*.tmp.png snapshots.
            if self.temp_dir.exists():
                for tmp_png in list(self.temp_dir.glob("pc_clipboard*.png")):
                    if not force and tmp_png.name == PC_CLIPBOARD_IMAGE_PATH.name:
                        continue
                    try:
                        reclaimed_bytes += tmp_png.stat().st_size
                        tmp_png.unlink(missing_ok=True)
                        reclaimed_count += 1
                    except OSError:
                        pass
                if force and PC_CLIPBOARD_IMAGE_PATH.exists():
                    try:
                        reclaimed_bytes += PC_CLIPBOARD_IMAGE_PATH.stat().st_size
                        PC_CLIPBOARD_IMAGE_PATH.unlink(missing_ok=True)
                        reclaimed_count += 1
                    except OSError:
                        pass

            if not adopted_tid and self._latest_transfer_file.exists():
                try:
                    data = json.loads(self._latest_transfer_file.read_text(encoding="utf-8"))
                    ptr_tid = data.get("transfer_id")
                    if ptr_tid not in self._transfers:
                        self._latest_transfer_file.unlink(missing_ok=True)
                except Exception:
                    try:
                        self._latest_transfer_file.unlink(missing_ok=True)
                    except OSError:
                        pass

        warnings = []
        if adopted_files:
            warnings.append(
                f"Preserved {len(adopted_files)} staged file(s) ({', '.join(adopted_files[:3])}) "
                "currently referenced by the Windows clipboard."
            )
        if preserved_foreign_count:
            warnings.append(
                f"Preserved {preserved_foreign_count} unrecognized or foreign storage item(s)."
            )
        return {
            "status": "ok",
            "ok": True,
            "reclaimed_bytes": reclaimed_bytes,
            "reclaimed_human": format_size(reclaimed_bytes),
            "reclaimed_count": reclaimed_count,
            "preserved_active_count": 1 if adopted_tid else 0,
            "preserved_foreign_count": preserved_foreign_count,
            "preserved_files": adopted_files,
            "warning": " ".join(warnings),
        }


HISTORY = TransferHistory(maxlen=MAX_HISTORY)


# ==============================================================================
# NATIVE WIN32 CLIPBOARD (ZERO SUBPROCESS FOR TEXT, <1ms EXECUTION)
# ==============================================================================
CF_UNICODETEXT = 13
CF_BITMAP = 2
CF_DIB = 8
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardSequenceNumber.argtypes = []
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT


def win32_get_clipboard_sequence_number() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        return int(user32.GetClipboardSequenceNumber())
    except Exception:
        return None


def win32_get_clipboard_files() -> list[Path]:
    if sys.platform != "win32":
        return []
    if not user32.IsClipboardFormatAvailable(CF_HDROP):
        return []
    if not _open_clipboard_with_retry():
        return []
    try:
        h_drop = user32.GetClipboardData(CF_HDROP)
        if not h_drop:
            return []
        count = shell32.DragQueryFileW(h_drop, 0xFFFFFFFF, None, 0)
        paths: list[Path] = []
        for idx in range(count):
            length = shell32.DragQueryFileW(h_drop, idx, None, 0)
            if length <= 0:
                continue
            buf = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(h_drop, idx, buf, length + 1)
            if buf.value:
                paths.append(Path(buf.value))
        return paths
    except Exception:
        return []
    finally:
        user32.CloseClipboard()


def _open_clipboard_with_retry(retries: int = 10, delay: float = 0.03) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def win32_set_clipboard_text(text: str) -> bool:
    if sys.platform != "win32":
        return False
    encoded = (text + "\0").encode("utf-16-le")
    if not _open_clipboard_with_retry():
        return False
    h_mem = None
    try:
        user32.EmptyClipboard()
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not h_mem:
            return False
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            kernel32.GlobalFree(h_mem)
            return False
        ctypes.memmove(ptr, encoded, len(encoded))
        kernel32.GlobalUnlock(h_mem)
        if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
            kernel32.GlobalFree(h_mem)
            return False
        h_mem = None
        return True
    finally:
        user32.CloseClipboard()
        if h_mem:
            kernel32.GlobalFree(h_mem)


def win32_get_clipboard_text() -> str:
    if sys.platform != "win32":
        return ""
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""
    if not _open_clipboard_with_retry():
        return ""
    try:
        h_data = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_data:
            return ""
        ptr = kernel32.GlobalLock(h_data)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(h_data)
    finally:
        user32.CloseClipboard()


# ==============================================================================
# ONE-SHOT POWERSHELL RUNNER (ENCODEDCOMMAND + STRICT TIMEOUT)
# ==============================================================================
def run_ps_oneshot(ps_script: str, timeout_sec: int = 10) -> str:
    if sys.platform != "win32":
        return "ERR:NotWindows"

    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0

    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-STA",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec,
            startupinfo=startupinfo,
            creationflags=0x08000000,
        )
        return (proc.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return "ERR:Timeout"
    except Exception as exc:
        return f"ERR:{exc}"


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("'", "&apos;")
        .replace('"', "&quot;")
    )


def _build_ps_toast_script(toast_xml: str) -> str:
    b64_xml = base64.b64encode(toast_xml.encode("utf-8")).decode("ascii")
    return f"""
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
$xmlString = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64_xml}'))
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($xmlString)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def _build_ps_set_clipboard_script(file_paths: list[Path]) -> str:
    single_image_path = None
    if len(file_paths) == 1 and file_paths[0].suffix.lower() in IMAGE_EXTENSIONS:
        single_image_path = str(file_paths[0].resolve())

    payload = {
        "paths": [str(p.resolve()) for p in file_paths],
        "image": single_image_path,
    }
    b64_json = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    if single_image_path:
        return f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$json = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64_json}'))
$cfg = $json | ConvertFrom-Json
$dataObj = New-Object System.Windows.Forms.DataObject
$sc = New-Object System.Collections.Specialized.StringCollection
foreach ($p in $cfg.paths) {{
    $null = $sc.Add([string]$p)
}}
$dataObj.SetFileDropList($sc)
$img = [System.Drawing.Image]::FromFile([string]$cfg.image)
try {{
    $dataObj.SetImage($img)
    [System.Windows.Forms.Clipboard]::SetDataObject($dataObj, $true)
    Write-Output "OK"
}} finally {{
    $img.Dispose()
}}
"""
    return f"""
Add-Type -AssemblyName System.Windows.Forms
$json = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64_json}'))
$cfg = $json | ConvertFrom-Json
$sc = New-Object System.Collections.Specialized.StringCollection
foreach ($p in $cfg.paths) {{
    $null = $sc.Add([string]$p)
}}
[System.Windows.Forms.Clipboard]::SetFileDropList($sc)
Write-Output "OK"
"""


class WindowsBridge:
    def __init__(self):
        self.lock = threading.Lock()

    def get_clipboard_sequence_number(self) -> int | None:
        return win32_get_clipboard_sequence_number()

    def set_clipboard_text(self, text: str) -> bool:
        with self.lock:
            return win32_set_clipboard_text(text)

    def set_clipboard_files_and_optional_image(self, file_paths: list[Path]) -> bool:
        if not file_paths:
            return False
        ps = _build_ps_set_clipboard_script(file_paths)
        with self.lock:
            res = run_ps_oneshot(ps, timeout_sec=10)
        return "OK" in res

    def get_clipboard_content(self) -> dict:
        if sys.platform != "win32":
            return {"type": "empty"}

        with self.lock:
            has_files = bool(user32.IsClipboardFormatAvailable(CF_HDROP))
            has_image = bool(
                user32.IsClipboardFormatAvailable(CF_BITMAP)
                or user32.IsClipboardFormatAvailable(CF_DIB)
            )
            has_text = bool(user32.IsClipboardFormatAvailable(CF_UNICODETEXT))

            if not has_files and not has_image:
                if has_text:
                    txt = win32_get_clipboard_text()
                    return {"type": "text", "data": txt} if txt else {"type": "empty"}
                return {"type": "empty"}

            TEMP_DIR.mkdir(parents=True, exist_ok=True)
            tmp_img_path = TEMP_DIR / f"pc_clipboard_{threading.get_ident()}_{secrets.token_hex(4)}.tmp.png"
            b64_img_path = base64.b64encode(str(tmp_img_path.resolve()).encode("utf-8")).decode("ascii")
            ps = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$targetImgPath = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64_img_path}'))
if ([System.Windows.Forms.Clipboard]::ContainsFileDropList()) {{
    $files = [System.Windows.Forms.Clipboard]::GetFileDropList()
    $valid = @()
    foreach ($f in $files) {{
        if (Test-Path -LiteralPath $f -PathType Leaf) {{
            $valid += $f
        }}
    }}
    if ($valid.Count -gt 0) {{
        $json = ConvertTo-Json -InputObject @($valid) -Compress
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
        $b64 = [System.Convert]::ToBase64String($bytes)
        Write-Output ("TYPE:FILES:" + $b64)
        exit 0
    }}
}}
if ([System.Windows.Forms.Clipboard]::ContainsImage()) {{
    $img = [System.Windows.Forms.Clipboard]::GetImage()
    if ($null -ne $img) {{
        $img.Save($targetImgPath, [System.Drawing.Imaging.ImageFormat]::Png)
        $img.Dispose()
        Write-Output "TYPE:IMAGE"
        exit 0
    }}
}}
Write-Output "TYPE:EMPTY"
"""
            try:
                res = run_ps_oneshot(ps, timeout_sec=10)
                if res.startswith("TYPE:FILES:"):
                    b64_part = res.split("TYPE:FILES:", 1)[1].strip()
                    raw_json = base64.b64decode(b64_part).decode("utf-8", errors="replace")
                    parsed = json.loads(raw_json)
                    if isinstance(parsed, str):
                        parsed = [parsed]
                    paths = [Path(p) for p in parsed if Path(p).is_file()]
                    if paths:
                        return {"type": "files", "paths": paths}
                if res.startswith("TYPE:IMAGE") and tmp_img_path.exists():
                    img_bytes = tmp_img_path.read_bytes()
                    try:
                        os.replace(tmp_img_path, PC_CLIPBOARD_IMAGE_PATH)
                    except OSError:
                        PC_CLIPBOARD_IMAGE_PATH.write_bytes(img_bytes)
                    return {"type": "image", "path": PC_CLIPBOARD_IMAGE_PATH, "bytes": img_bytes}
                if has_text:
                    txt = win32_get_clipboard_text()
                    if txt:
                        return {"type": "text", "data": txt}
                return {"type": "empty"}
            finally:
                try:
                    tmp_img_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def show_incoming_files_toast_async(
        self,
        file_paths: list[Path],
        transfer_id: str | None = None,
        history_store: TransferHistory | None = None,
    ):
        if not file_paths:
            return

        if len(file_paths) == 1:
            p = file_paths[0]
            is_img = p.suffix.lower() in IMAGE_EXTENSIONS
            title = "Screenshot in Windows Clipboard" if is_img else f"File in Clipboard: {p.name}"
            subtitle = "Press Ctrl+V to paste anywhere, or click Save to Downloads."
        else:
            names = ", ".join(p.name for p in file_paths[:3])
            if len(file_paths) > 3:
                names += f" (+{len(file_paths) - 3} more)"
            title = f"{len(file_paths)} Files in Windows Clipboard"
            subtitle = f"Ready for Ctrl+V ({names})"

        safe_title = _xml_escape(title)
        safe_sub = _xml_escape(subtitle)

        hero_xml = ""
        if len(file_paths) == 1 and file_paths[0].suffix.lower() in IMAGE_EXTENSIONS:
            escaped_img = _xml_escape(str(file_paths[0].resolve()))
            hero_xml = f'<image placement="hero" src="{escaped_img}"/>'

        toast_xml = f"""<toast activationType="protocol" launch="clipbridge://noop">
    <visual>
        <binding template="ToastGeneric">
            <text>{safe_title}</text>
            <text>{safe_sub}</text>
            {hero_xml}
        </binding>
    </visual>
    <actions>
        <action content="Save to Downloads" arguments="clipbridge://save" activationType="protocol"/>
        <action content="Open Downloads" arguments="clipbridge://open" activationType="protocol"/>
    </actions>
</toast>"""
        ps = _build_ps_toast_script(toast_xml)

        leased = bool(history_store and transfer_id and history_store.acquire_lease(transfer_id))

        def _worker():
            try:
                run_ps_oneshot(ps, 10)
            finally:
                if leased and history_store and transfer_id:
                    history_store.release_lease(transfer_id)

        threading.Thread(target=_worker, daemon=True).start()

    def show_simple_toast(self, title: str, message: str):
        safe_title = _xml_escape(title)
        safe_msg = _xml_escape(message)
        toast_xml = f"""<toast>
    <visual>
        <binding template="ToastGeneric">
            <text>{safe_title}</text>
            <text>{safe_msg}</text>
        </binding>
    </visual>
</toast>"""
        ps = _build_ps_toast_script(toast_xml)
        threading.Thread(target=run_ps_oneshot, args=(ps, 10), daemon=True).start()


WIN_BRIDGE = WindowsBridge()
PC_FILES_LOCK = threading.Lock()
CURRENT_PC_FILES: list[Path] = []


def sanitize_filename(name: str, default_ext: str = ".bin") -> str:
    raw = unquote(name.strip()).replace("\\", "/")
    base = raw.rsplit("/", 1)[-1].strip()
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)
    base = base.rstrip(" .")
    if not base or base in {".", ".."}:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return f"Shared_{timestamp}{default_ext}"
    stem_part = base.split(".", 1)[0].upper()
    if stem_part in WINDOWS_RESERVED_NAMES:
        base = f"_{base}"
    if len(base) > 180:
        p = Path(base)
        ext = p.suffix[:20]
        stem = p.stem[: 180 - len(ext)].rstrip(" .") or "file"
        base = f"{stem}{ext}"
    return base.rstrip(" .") or f"Shared_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}{default_ext}"


def _copy_files_to_downloads(staged_files: list[Path]) -> list[Path]:
    if not staged_files:
        return []
    SAVE_FOLDER.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    for src in staged_files:
        if not src.is_file():
            continue
        dest = SAVE_FOLDER / src.name
        counter = 1
        while dest.exists():
            dest = SAVE_FOLDER / f"{src.stem}_{counter}{src.suffix}"
            counter += 1
        shutil.copy2(src, dest)
        saved_paths.append(dest)
    return saved_paths


def save_staged_files_to_downloads(history_store: TransferHistory | None = None) -> list[Path]:
    if history_store is not None:
        tid, files = history_store.acquire_latest_staged_files()
        if not files:
            return []
        try:
            return _copy_files_to_downloads(files)
        finally:
            history_store.release_lease(tid)

    # Out-of-process path (e.g. clipbridge://save): read atomic pointer first,
    # never touching in-progress .staging_* directories.
    candidate_files: list[Path] = []
    if LATEST_TRANSFER_FILE.exists():
        try:
            data = json.loads(LATEST_TRANSFER_FILE.read_text(encoding="utf-8"))
            tx_dir = Path(str(data.get("dir", "")))
            if _is_strict_subpath(tx_dir, STAGED_DIR) and tx_dir.is_dir():
                for raw_f in data.get("files", []):
                    fp = Path(str(raw_f))
                    if _is_strict_subpath(fp, tx_dir) and fp.is_file():
                        candidate_files.append(fp)
                if not candidate_files:
                    candidate_files = sorted(p for p in tx_dir.iterdir() if p.is_file())
        except Exception:
            candidate_files = []

    if not candidate_files and STAGED_DIR.exists():
        dirs = sorted(
            (
                d
                for d in STAGED_DIR.iterdir()
                if d.is_dir() and not d.name.startswith(".staging_")
            ),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for d in dirs:
            files = sorted(p for p in d.iterdir() if p.is_file())
            if files:
                candidate_files = files
                break
        if not candidate_files:
            candidate_files = sorted(p for p in STAGED_DIR.iterdir() if p.is_file())

    return _copy_files_to_downloads(candidate_files)


def format_size(num_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ==============================================================================
# DYNAMIC HTTP SHORTCUTS JSON GENERATOR (FOR 1-CLICK "IMPORT FROM URL")
# ==============================================================================
def build_dynamic_shortcuts_json(pc_ip: str, port: int, secret_key: str) -> dict:
    return {
        "version": 80,
        "compatibilityVersion": 70,
        "categories": [
            {
                "id": "a1b2c3d4-0000-4000-8000-000000000001",
                "name": "ClipBridge",
                "layoutType": "linear_list",
                "shortcuts": [
                    {
                        "id": "a1b2c3d4-0000-4000-8000-000000000011",
                        "name": "Send Text to PC",
                        "description": "Copies your Android clipboard (or shared text) directly into Windows Clipboard",
                        "iconName": "flat_color_upload",
                        "method": "POST",
                        "url": "http://{{bf000000-0000-4000-8000-000000000001}}:" + str(port) + "/api/push-text",
                        "headers": [
                            {"key": "X-Secret-Key", "value": "{{bf000000-0000-4000-8000-000000000002}}"},
                            {"key": "Content-Type", "value": "text/plain; charset=utf-8"},
                        ],
                        "bodyContent": "{{bf000000-0000-4000-8000-000000000003}}",
                        "requestBodyType": "custom_text",
                        "codeOnPrepare": (
                            "const shared = getVariable('text_payload');\n"
                            "if (!shared || shared.trim() === '') {\n"
                            "  setVariable('text_payload', getClipboardContent() || '');\n"
                            "}"
                        ),
                        "quickSettingsTileShortcut": True,
                        "responseHandling": {
                            "uiType": "toast",
                            "successOutput": "message",
                            "successMessage": "Copied text to Windows Clipboard (Ctrl+V ready)!",
                        },
                    },
                    {
                        "id": "a1b2c3d4-0000-4000-8000-000000000012",
                        "name": "Send File or Screenshot to PC",
                        "description": "Share any screenshot or 1+ files (PDF, ZIP, video, doc) to put them in Windows Clipboard for Ctrl+V (+ Toast button to save to Downloads)",
                        "iconName": "flat_color_picture",
                        "method": "POST",
                        "url": "http://{{bf000000-0000-4000-8000-000000000001}}:" + str(port) + "/api/push-files",
                        "headers": [
                            {"key": "X-Secret-Key", "value": "{{bf000000-0000-4000-8000-000000000002}}"},
                        ],
                        "requestBodyType": "form_data",
                        "parameters": [
                            {"key": "files", "type": "files", "value": ""},
                        ],
                        "quickSettingsTileShortcut": True,
                        "responseHandling": {
                            "uiType": "toast",
                            "successOutput": "message",
                            "successMessage": "Ready for Ctrl+V on Windows!",
                        },
                    },
                    {
                        "id": "a1b2c3d4-0000-4000-8000-000000000013",
                        "name": "Get from PC",
                        "description": "Copies PC text/images to Android Clipboard, or downloads copied Windows Explorer files to your phone's Downloads folder",
                        "iconName": "flat_color_download",
                        "method": "GET",
                        "url": "http://{{bf000000-0000-4000-8000-000000000001}}:" + str(port) + "/api/pull",
                        "headers": [
                            {"key": "X-Secret-Key", "value": "{{bf000000-0000-4000-8000-000000000002}}"},
                        ],
                        "codeOnSuccess": (
                            "const data = JSON.parse(response.body);\n"
                            "if (data.type === 'text') {\n"
                            "  copyToClipboard(data.content);\n"
                            "  showToast('Copied PC text to phone clipboard!');\n"
                            "} else if (data.type === 'image') {\n"
                            f"  openUrl('http://' + getVariable('pc_ip') + ':{port}/?key=' + getVariable('secret_key') + '&action=pull');\n"
                            "} else if (data.type === 'files') {\n"
                            "  if (data.files.length === 1) {\n"
                            f"    openUrl('http://' + getVariable('pc_ip') + ':{port}' + data.files[0].url);\n"
                            "  } else {\n"
                            f"    openUrl('http://' + getVariable('pc_ip') + ':{port}/?key=' + getVariable('secret_key') + '&action=pull');\n"
                            "  }\n"
                            "} else {\n"
                            "  showToast('Windows clipboard is empty');\n"
                            "}"
                        ),
                        "quickSettingsTileShortcut": True,
                        "responseHandling": {
                            "uiType": "toast",
                            "successOutput": "none",
                        },
                    },
                ],
            }
        ],
        "variables": [
            {
                "id": "bf000000-0000-4000-8000-000000000001",
                "key": "pc_ip",
                "type": "constant",
                "value": pc_ip,
            },
            {
                "id": "bf000000-0000-4000-8000-000000000002",
                "key": "secret_key",
                "type": "constant",
                "value": secret_key,
            },
            {
                "id": "bf000000-0000-4000-8000-000000000003",
                "key": "text_payload",
                "type": "clipboard",
                "value": "",
                "shareText": True,
            },
        ],
    }


# ==============================================================================
# CHUNKED MULTIPART STREAMING PARSER (LOW RAM USAGE FOR LARGE VIDEOS / ZIPS)
# ==============================================================================
def stream_multipart_to_staged_dir(
    rfile: io.BufferedReader,
    content_length: int,
    boundary: bytes,
    target_dir: Path,
) -> list[Path]:
    """
    Streams multipart/form-data from rfile in 64KB chunks directly to disk files
    inside target_dir without loading the entire upload body into memory.
    Raises IncompleteUploadError if truncated/malformed or UploadLimitError if
    too many parts are submitted; cleans up created partial files on error.
    """
    if not boundary:
        raise IncompleteUploadError("Missing multipart boundary")

    saved_paths: list[Path] = []
    created_files: list[Path] = []
    delim = b"--" + boundary
    chunk_size = 65536
    remaining = content_length
    buf = b""

    state = "FIND_BOUNDARY"
    current_file = None
    current_path: Path | None = None
    seen_closing_boundary = False

    def _fill(min_bytes: int):
        nonlocal remaining, buf
        while remaining > 0 and len(buf) < min_bytes:
            to_read = min(chunk_size, remaining)
            chunk = rfile.read(to_read)
            if not chunk:
                raise IncompleteUploadError("Upload stream interrupted before Content-Length bytes were received")
            remaining -= len(chunk)
            buf += chunk

    try:
        while remaining > 0 or buf:
            if remaining > 0 and len(buf) < chunk_size * 2:
                _fill(min(chunk_size * 2, len(buf) + remaining))

            if state == "FIND_BOUNDARY":
                idx = buf.find(delim)
                if idx == -1:
                    if remaining == 0:
                        break
                    if len(buf) > len(delim):
                        buf = buf[-len(delim) :]
                    continue
                buf = buf[idx + len(delim) :]
                if len(buf) < 2 and remaining > 0:
                    _fill(2)
                if buf.startswith(b"--"):
                    seen_closing_boundary = True
                    break
                if buf.startswith(b"\r\n"):
                    buf = buf[2:]
                state = "READ_HEADERS"

            elif state == "READ_HEADERS":
                hdr_end = buf.find(b"\r\n\r\n")
                if hdr_end == -1:
                    if len(buf) > 32768:
                        raise IncompleteUploadError("Multipart part header exceeds 32 KB limit")
                    if remaining == 0:
                        raise IncompleteUploadError("Truncated multipart part headers")
                    continue

                header_bytes = buf[:hdr_end]
                buf = buf[hdr_end + 4 :]
                headers_text = header_bytes.decode("utf-8", errors="replace")

                if "Content-Disposition" not in headers_text:
                    state = "FIND_BOUNDARY"
                    continue

                if len(saved_paths) >= MAX_FILES_PER_UPLOAD:
                    raise UploadLimitError(f"Exceeded max files per upload ({MAX_FILES_PER_UPLOAD})")

                filename = None
                m_utf8 = re.search(r"filename\*=UTF-8''([^;\r\n]+)", headers_text, re.IGNORECASE)
                if m_utf8:
                    filename = unquote(m_utf8.group(1).strip())
                else:
                    m_std = re.search(r'filename="([^"\r\n]+)"', headers_text, re.IGNORECASE)
                    if m_std:
                        filename = m_std.group(1).strip()

                part_ctype = "application/octet-stream"
                m_ct = re.search(r"Content-Type:\s*([^\r\n;]+)", headers_text, re.IGNORECASE)
                if m_ct:
                    part_ctype = m_ct.group(1).strip()
                ext = mimetypes.guess_extension(part_ctype) or ".bin"
                if part_ctype == "image/jpeg":
                    ext = ".jpg"
                elif part_ctype == "image/png":
                    ext = ".png"

                if not filename:
                    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    prefix = "Screenshot" if ext in IMAGE_EXTENSIONS else "File"
                    filename = f"{prefix}_{timestamp}_{len(saved_paths) + 1}{ext}"
                else:
                    filename = sanitize_filename(filename, ext)

                target_path = target_dir / filename
                counter = 1
                while target_path.exists():
                    target_path = target_dir / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
                    counter += 1
                if not _is_strict_subpath(target_path, target_dir):
                    raise IncompleteUploadError("Resolved target path escaped staging directory")

                current_path = target_path
                current_file = target_path.open("wb")
                created_files.append(target_path)
                state = "STREAM_BODY"

            elif state == "STREAM_BODY":
                needle = b"\r\n" + delim
                idx = buf.find(needle)
                if idx != -1:
                    if current_file:
                        current_file.write(buf[:idx])
                        current_file.close()
                        current_file = None
                    if current_path and current_path.exists() and current_path.stat().st_size > 0:
                        saved_paths.append(current_path)
                    elif current_path and current_path.exists():
                        current_path.unlink(missing_ok=True)
                    current_path = None

                    buf = buf[idx + 2 + len(delim) :]
                    if len(buf) < 2 and remaining > 0:
                        _fill(2)
                    if buf.startswith(b"--"):
                        seen_closing_boundary = True
                        break
                    if buf.startswith(b"\r\n"):
                        buf = buf[2:]
                    state = "READ_HEADERS"
                else:
                    keep_tail = len(needle) + 8
                    if len(buf) > keep_tail:
                        writable = buf[:-keep_tail]
                        if current_file:
                            current_file.write(writable)
                        buf = buf[-keep_tail:]
                    elif remaining == 0:
                        raise IncompleteUploadError("Multipart payload ended before closing boundary")

        if not seen_closing_boundary:
            raise IncompleteUploadError("Multipart payload missing closing boundary")
    except Exception:
        if current_file:
            try:
                current_file.close()
            except Exception:
                pass
        for p in created_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        if current_file:
            try:
                current_file.close()
            except Exception:
                pass

    return saved_paths


# ==============================================================================
# WEB DASHBOARD & RECENT CLIPBOARD HISTORY UI
# ==============================================================================
MOBILE_WEB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ClipBridge — Windows & Android Hub</title>
<style>
  :root { color-scheme: dark; }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    background: #0f172a; color: #f8fafc;
    margin: 0; padding: 20px;
    display: flex; flex-direction: column; gap: 16px;
    max-width: 540px; margin-inline: auto;
  }
  .header {
    display: flex; justify-content: space-between; align-items: center;
    padding-bottom: 4px;
  }
  .header h1 { margin: 0; font-size: 1.25rem; color: #38bdf8; }
  .badge {
    font-size: 0.75rem; background: #1e293b; border: 1px solid #334155;
    padding: 4px 10px; border-radius: 999px; color: #94a3b8;
  }
  .card {
    background: #1e293b; border: 1px solid #334155;
    border-radius: 14px; padding: 16px;
  }
  h2 { margin: 0 0 8px 0; font-size: 1.05rem; }
  p { margin: 0 0 12px 0; font-size: 0.88rem; color: #94a3b8; line-height: 1.4; }
  button, .btn {
    display: block; box-sizing: border-box; text-align: center; text-decoration: none;
    width: 100%; padding: 13px; border-radius: 10px; border: none;
    font-size: 0.96rem; font-weight: 600; cursor: pointer;
    background: #3b82f6; color: white; margin-top: 8px;
  }
  button.secondary, .btn.secondary { background: #10b981; }
  button.ghost {
    background: transparent; border: 1px solid #475569; color: #cbd5e1;
    padding: 8px 12px; font-size: 0.82rem; width: auto; margin-top: 0;
  }
  textarea {
    width: 100%; box-sizing: border-box; height: 85px;
    border-radius: 8px; border: 1px solid #475569;
    background: #0f172a; color: #f8fafc; padding: 10px; font-size: 0.92rem;
  }
  #previewImg {
    max-width: 100%; border-radius: 8px; margin-top: 10px; display: none;
    border: 1px solid #475569;
  }
  #fileList, #historyList {
    display: flex; flex-direction: column; gap: 10px; margin-top: 10px;
  }
  .hist-item {
    background: #0f172a; border: 1px solid #334155; border-radius: 10px;
    padding: 12px; display: flex; flex-direction: column; gap: 6px;
  }
  .hist-meta {
    display: flex; justify-content: space-between; font-size: 0.78rem; color: #64748b;
  }
  .hist-text {
    font-family: ui-monospace, monospace; font-size: 0.85rem; color: #e2e8f0;
    white-space: pre-wrap; word-break: break-word;
  }
  .code-box {
    background: #0f172a; border: 1px solid #334155; border-radius: 8px;
    padding: 10px; font-family: ui-monospace, monospace; font-size: 0.82rem;
    word-break: break-all; color: #38bdf8; margin-bottom: 8px;
  }
  #status {
    padding: 12px; border-radius: 10px; background: #1e293b;
    border: 1px solid #38bdf8; color: #38bdf8; font-size: 0.92rem; display: none;
  }
</style>
</head>
<body>
  <div class="header">
    <h1>ClipBridge</h1>
    <span class="badge">v1.0.0 • Online</span>
  </div>

  <div id="status"></div>

  <div class="card">
    <h2>1. Pull from Windows PC</h2>
    <p>Fetches text, screenshots, or copied Explorer files from your Windows PC.</p>
    <button onclick="pullFromPC()">Get from PC Now</button>
    <textarea id="pcText" placeholder="PC text will appear here..." style="margin-top:12px;display:none;"></textarea>
    <img id="previewImg" alt="PC Clipboard Image" />
    <p id="imgHint" style="display:none;margin-top:8px;color:#cbd5e1;">
      If Chrome blocked automatic clipboard access, long-press the image above and tap <b>Copy image</b>.
    </p>
    <div id="fileList"></div>
  </div>

  <div class="card">
    <h2>2. Send to Windows PC</h2>
    <textarea id="phoneText" placeholder="Paste text here to send to Windows..."></textarea>
    <button onclick="sendTextToPC()">Send Text to PC</button>
    <input type="file" id="fileInput" multiple style="display:none" onchange="sendFilesToPC(this.files)">
    <button class="secondary" onclick="document.getElementById('fileInput').click()">Send File(s) or Screenshot to PC</button>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
      <h2>3. Recent Transfer History</h2>
      <div style="display:flex;gap:6px;">
        <button class="ghost" onclick="cleanInactiveStorage()">Clean Temp</button>
        <button class="ghost" onclick="clearHistory()">Clear</button>
      </div>
    </div>
    <div id="storageInfo" class="hist-meta" style="margin-top:8px;">Temp storage: checking...</div>
    <div id="historyList"><p style="margin:4px 0 0 0;">No transfers yet in this session.</p></div>
  </div>

  <div class="card">
    <h2>4. One-Click Android Import URL</h2>
    <p>In the Android <b>HTTP Shortcuts</b> app, tap <b>Settings → Import / Export → Import from URL</b> and paste this URL (your IP & key are pre-filled automatically):</p>
    <div class="code-box" id="importUrlBox"></div>
    <button class="ghost" style="width:100%" onclick="copyImportUrl()">Copy Import URL</button>
  </div>

<script>
const params = new URLSearchParams(window.location.search);
const KEY = params.get('key') || '';
const importUrl = `${window.location.origin}/setup/shortcuts.json?key=${encodeURIComponent(KEY)}`;
document.getElementById('importUrlBox').textContent = importUrl;

function showStatus(msg) {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.style.display = 'block';
}

function copyImportUrl() {
  navigator.clipboard.writeText(importUrl).then(() => showStatus('Copied Import URL!'));
}

function triggerFileDownload(url, filename) {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || '';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

async function pullFromPC() {
  showStatus('Checking Windows clipboard...');
  const res = await fetch(`/api/pull?key=${encodeURIComponent(KEY)}`);
  if (!res.ok) { showStatus('Unauthorized or error (' + res.status + ')'); return; }
  const data = await res.json();
  const txtEl = document.getElementById('pcText');
  const imgEl = document.getElementById('previewImg');
  const hintEl = document.getElementById('imgHint');
  const fileListEl = document.getElementById('fileList');
  fileListEl.innerHTML = '';

  if (data.type === 'text') {
    imgEl.style.display = 'none';
    hintEl.style.display = 'none';
    txtEl.style.display = 'block';
    txtEl.value = data.content;
    try {
      await navigator.clipboard.writeText(data.content);
      showStatus('Copied PC text to Android clipboard!');
    } catch (e) {
      txtEl.select();
      document.execCommand('copy');
      showStatus('Text loaded & copied!');
    }
  } else if (data.type === 'image') {
    txtEl.style.display = 'none';
    const imgUrl = `/api/pull-image?key=${encodeURIComponent(KEY)}&t=${Date.now()}`;
    imgEl.src = imgUrl;
    imgEl.style.display = 'block';
    hintEl.style.display = 'block';
    try {
      const imgRes = await fetch(imgUrl);
      const blob = await imgRes.blob();
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
      showStatus('Copied PC image directly to Android clipboard!');
    } catch (e) {
      showStatus('Image loaded below! Long-press the image and tap Copy image.');
    }
  } else if (data.type === 'files') {
    txtEl.style.display = 'none';
    imgEl.style.display = 'none';
    hintEl.style.display = 'none';
    showStatus(`Downloading ${data.files.length} file(s) from PC to your phone's Downloads folder...`);
    data.files.forEach((f, idx) => {
      const btn = document.createElement('a');
      btn.className = 'btn secondary';
      btn.href = f.url;
      btn.download = f.name;
      btn.textContent = `Download ${f.name} (${f.size_human})`;
      fileListEl.appendChild(btn);
      setTimeout(() => triggerFileDownload(f.url, f.name), idx * 450);
    });
  } else {
    showStatus('Windows clipboard is currently empty.');
  }
  loadHistory();
}

async function sendTextToPC() {
  const txt = document.getElementById('phoneText').value;
  if (!txt) return;
  const res = await fetch(`/api/push-text?key=${encodeURIComponent(KEY)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    body: txt
  });
  if (res.ok) {
    showStatus('Text copied to Windows clipboard (Press Ctrl+V on PC)!');
    document.getElementById('phoneText').value = '';
    loadHistory();
  }
}

async function sendFilesToPC(fileList) {
  if (!fileList || fileList.length === 0) return;
  showStatus(`Uploading ${fileList.length} file(s) to Windows clipboard...`);
  const form = new FormData();
  for (let i = 0; i < fileList.length; i++) {
    form.append('files', fileList[i], fileList[i].name);
  }
  const res = await fetch(`/api/push-files?key=${encodeURIComponent(KEY)}`, {
    method: 'POST',
    body: form
  });
  if (res.ok) {
    showStatus(`${fileList.length} file(s) in Windows clipboard! Press Ctrl+V on PC.`);
    loadHistory();
  } else {
    showStatus('Upload failed (' + res.status + ').');
  }
}

async function loadStorageInfo() {
  const res = await fetch(`/api/storage?key=${encodeURIComponent(KEY)}`);
  if (!res.ok) return;
  const st = await res.json();
  const el = document.getElementById('storageInfo');
  const activeCount = (st.protected_items || []).length;
  const activeTxt = activeCount > 0 ? ` • ${activeCount} active on clipboard` : '';
  el.textContent = `Temp: ${st.used_human} / ${st.quota_human} (TTL ${st.retention_minutes}m)${activeTxt}`;
}

async function loadHistory() {
  loadStorageInfo();
  const res = await fetch(`/api/history?key=${encodeURIComponent(KEY)}`);
  if (!res.ok) return;
  const data = await res.json();
  const listEl = document.getElementById('historyList');
  if (!data.items || data.items.length === 0) {
    listEl.innerHTML = '<p style="margin:4px 0 0 0;">No transfers yet in this session.</p>';
    return;
  }
  listEl.innerHTML = '';
  data.items.forEach(item => {
    const div = document.createElement('div');
    div.className = 'hist-item';
    const meta = document.createElement('div');
    meta.className = 'hist-meta';
    const dirSpan = document.createElement('span');
    dirSpan.textContent = item.direction + (item.protected ? ' • Active' : '');
    const timeSpan = document.createElement('span');
    timeSpan.textContent = item.timestamp;
    meta.appendChild(dirSpan);
    meta.appendChild(timeSpan);
    div.appendChild(meta);

    if (item.type === 'text') {
      const txt = document.createElement('div');
      txt.className = 'hist-text';
      txt.textContent = item.preview;
      div.appendChild(txt);
      const copyBtn = document.createElement('button');
      copyBtn.className = 'ghost';
      copyBtn.textContent = 'Copy Text';
      copyBtn.onclick = () => {
        navigator.clipboard.writeText(item.content).then(() => showStatus('Copied from history!'));
      };
      div.appendChild(copyBtn);
    } else if (item.type === 'files') {
      item.files.forEach(f => {
        const a = document.createElement('a');
        a.className = 'btn secondary';
        a.style.padding = '8px 12px';
        a.style.fontSize = '0.85rem';
        a.href = f.url;
        a.download = f.name;
        a.textContent = `Download ${f.name} (${f.size_human})`;
        div.appendChild(a);
      });
    }
    listEl.appendChild(div);
  });
}

async function cleanInactiveStorage() {
  const res = await fetch(`/api/cleanup?key=${encodeURIComponent(KEY)}`, { method: 'POST' });
  if (res.ok) {
    const data = await res.json();
    const parts = [`Cleaned ${data.reclaimed_human} (${data.reclaimed_count} item(s)).`];
    if (data.warning) parts.push(data.warning);
    showStatus(parts.join(' '));
  }
  loadHistory();
}

async function clearHistory() {
  const res = await fetch(`/api/history/clear?key=${encodeURIComponent(KEY)}`, { method: 'POST' });
  if (res.ok) {
    const data = await res.json();
    if (data.preserved_active_count > 0) {
      const ok = window.confirm(
        `Warning: ${data.warning}\n\nPress OK to force-remove those active clipboard files too (Ctrl+V will stop working for them), or Cancel to keep them safe.`
      );
      if (ok) {
        await fetch(`/api/history/clear?key=${encodeURIComponent(KEY)}&force=1`, { method: 'POST' });
        showStatus('Cleared all history and active temporary files.');
      } else {
        showStatus(data.warning);
      }
    } else {
      showStatus('Cleared session transfer history.');
    }
  }
  loadHistory();
}

window.addEventListener('DOMContentLoaded', () => {
  loadHistory();
  if (params.get('action') === 'pull') {
    pullFromPC();
  }
});
</script>
</body>
</html>
"""


# ==============================================================================
# HTTP REQUEST HANDLER
# ==============================================================================
class ClipBridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ClipboardRequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(REQUEST_TIMEOUT_SEC)
        except OSError:
            pass

    def end_headers(self):
        # Do not expose bearer-key URLs via referrer headers; disable MIME sniffing and framing.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def log_message(self, format, *args):
        if sys.stdout and not sys.executable.lower().endswith("pythonw.exe"):
            msg = re.sub(r"([?&]key=)[^&\s\"']+", r"\1***", format % args)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.client_address[0]} - {msg}", flush=True)

    def _is_authorized(self) -> bool:
        if not SECRET_KEY:
            return False
        header_key = self.headers.get("X-Secret-Key", "").strip()
        parsed = urlparse(self.path)
        query_key = parse_qs(parsed.query).get("key", [""])[0].strip()
        header_ok = bool(header_key) and secrets.compare_digest(header_key, SECRET_KEY)
        query_ok = bool(query_key) and secrets.compare_digest(query_key, SECRET_KEY)
        return header_ok or query_ok

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _parse_content_length(self, max_bytes: int) -> int | None:
        if "Transfer-Encoding" in self.headers and "Content-Length" not in self.headers:
            self._send_json(411, {"status": "error", "message": "Content-Length header is required"})
            return None
        raw_cl = self.headers.get("Content-Length", "0").strip()
        try:
            length = int(raw_cl)
        except ValueError:
            self._send_json(400, {"status": "error", "message": "Invalid Content-Length header"})
            return None
        if length < 0:
            self._send_json(400, {"status": "error", "message": "Negative Content-Length header"})
            return None
        if length > max_bytes:
            self._send_json(413, {"status": "error", "message": f"Payload exceeds limit of {format_size(max_bytes)}"})
            return None
        return length

    def _read_optional_force_flag(self, parsed) -> bool:
        query_force = parse_qs(parsed.query).get("force", [""])[0].strip().lower()
        if query_force in {"1", "true", "yes"}:
            return True
        raw_cl = self.headers.get("Content-Length", "0").strip()
        try:
            length = int(raw_cl)
        except ValueError:
            return False
        if 0 < length <= 4096:
            try:
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                data = json.loads(body)
                if isinstance(data, dict):
                    return bool(data.get("force", False))
            except Exception:
                pass
        return False

    def do_GET(self):
        global CURRENT_PC_FILES
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/ping":
            self._send_json(200, {"status": "ok", "service": "ClipBridge", "version": VERSION})
            return

        if not self._is_authorized():
            self._send_json(401, {"status": "error", "message": "Invalid or missing secret key"})
            return

        if route == "/":
            html = MOBILE_WEB_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
            return

        if route == "/setup/shortcuts.json":
            host_header = self.headers.get("Host", "")
            pc_ip = host_header.split(":")[0].strip() if host_header else get_local_ip()
            if pc_ip in {"127.0.0.1", "localhost", ""}:
                pc_ip = get_local_ip()
            shortcuts_payload = build_dynamic_shortcuts_json(pc_ip, PORT, SECRET_KEY)
            self._send_json(200, shortcuts_payload)
            return

        if route == "/api/history":
            self._send_json(200, {"status": "ok", "items": HISTORY.list_items()})
            return

        if route == "/api/storage":
            self._send_json(200, HISTORY.get_storage_status())
            return

        if route == "/api/pull":
            clip = WIN_BRIDGE.get_clipboard_content()
            if clip["type"] == "text":
                HISTORY.add_text(clip["data"], "PC -> Phone")
                self._send_json(200, {"type": "text", "content": clip["data"]})
            elif clip["type"] == "image":
                if clip.get("bytes"):
                    HISTORY.add_image_bytes("pc_clipboard.png", clip["bytes"], "PC -> Phone (Image)")
                else:
                    HISTORY.add_files([clip["path"]], "PC -> Phone (Image)")
                self._send_json(
                    200,
                    {
                        "type": "image",
                        "content": "",
                        "image_url": f"/api/pull-image?key={SECRET_KEY}",
                    },
                )
            elif clip["type"] == "files":
                with PC_FILES_LOCK:
                    CURRENT_PC_FILES = list(clip["paths"])
                    pc_files = list(CURRENT_PC_FILES)
                HISTORY.add_files(pc_files, "PC -> Phone (Files)")
                file_entries = []
                for idx, p in enumerate(pc_files):
                    size_bytes = p.stat().st_size if p.exists() else 0
                    file_entries.append(
                        {
                            "index": idx,
                            "name": p.name,
                            "size": size_bytes,
                            "size_human": format_size(size_bytes),
                            "url": f"/api/pull-file/{idx}?key={SECRET_KEY}",
                        }
                    )
                self._send_json(
                    200,
                    {
                        "type": "files",
                        "count": len(file_entries),
                        "files": file_entries,
                    },
                )
            else:
                self._send_json(200, {"type": "empty", "content": ""})
            return

        if route == "/api/pull-image":
            clip = WIN_BRIDGE.get_clipboard_content()
            data = clip.get("bytes")
            if data is None and clip["type"] == "image" and clip["path"].exists():
                data = clip["path"].read_bytes()
            if clip["type"] == "image" and data is not None:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", 'inline; filename="pc_clipboard.png"')
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_json(404, {"status": "error", "message": "No image in Windows clipboard"})
            return

        if route.startswith("/api/pull-file/"):
            idx_str = route.split("/api/pull-file/", 1)[1].split("/", 1)[0]
            if not idx_str.isdigit():
                self._send_json(400, {"status": "error", "message": "Invalid file index"})
                return
            idx = int(idx_str)

            with PC_FILES_LOCK:
                pc_files = list(CURRENT_PC_FILES)

            if not pc_files:
                clip = WIN_BRIDGE.get_clipboard_content()
                if clip["type"] == "files":
                    with PC_FILES_LOCK:
                        CURRENT_PC_FILES = list(clip["paths"])
                        pc_files = list(CURRENT_PC_FILES)

            if idx < 0 or idx >= len(pc_files) or not pc_files[idx].is_file():
                self._send_json(404, {"status": "error", "message": "File no longer in Windows clipboard"})
                return

            self._serve_file_attachment(pc_files[idx])
            return

        if route.startswith("/api/history-file/"):
            parts = route.split("/api/history-file/", 1)[1].split("/")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                target, tid = HISTORY.acquire_file_for_read(int(parts[0]), int(parts[1]))
                if target:
                    try:
                        self._serve_file_attachment(target)
                    finally:
                        HISTORY.release_lease(tid)
                    return
            self._send_json(404, {"status": "error", "message": "History file not found"})
            return

        if route == "/api/save-latest":
            saved_paths = save_staged_files_to_downloads(HISTORY)
            if saved_paths:
                names = ", ".join(p.name for p in saved_paths)
                WIN_BRIDGE.show_simple_toast("Saved to Downloads", f"Saved: {names}")
                self._send_json(200, {"status": "ok", "saved": [str(p) for p in saved_paths]})
            else:
                self._send_json(404, {"status": "error", "message": "No staged files found"})
            return

        self._send_json(404, {"status": "error", "message": "Not found"})

    def _serve_file_attachment(self, target_file: Path):
        mime_type, _ = mimetypes.guess_type(str(target_file))
        mime_type = mime_type or "application/octet-stream"
        try:
            file_size = target_file.stat().st_size
            file_handle = target_file.open("rb")
        except OSError:
            self._send_json(404, {"status": "error", "message": "File unavailable"})
            return

        safe_ascii_name = target_file.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        encoded_name = quote(target_file.name)
        try:
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{safe_ascii_name}"; filename*=UTF-8\'\'{encoded_name}',
            )
            self.end_headers()
            with file_handle:
                shutil.copyfileobj(file_handle, self.wfile, length=65536)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    def do_POST(self):
        if not self._is_authorized():
            self._send_json(401, {"status": "error", "message": "Invalid or missing secret key"})
            return

        parsed = urlparse(self.path)
        route = parsed.path
        content_type = self.headers.get("Content-Type", "")

        if route == "/api/cleanup":
            force = self._read_optional_force_flag(parsed)
            result = HISTORY.cleanup(force=force, reclaim_inactive=True)
            result["status"] = "ok"
            self._send_json(200, result)
            return

        if route == "/api/history/clear":
            force = self._read_optional_force_flag(parsed)
            result = HISTORY.clear(preserve_active=not force)
            self._send_json(200, result)
            return

        if route == "/api/push-text":
            content_length = self._parse_content_length(MAX_TEXT_BYTES)
            if content_length is None:
                return
            try:
                raw_body = self.rfile.read(content_length) if content_length > 0 else b""
            except (TimeoutError, OSError):
                self._send_json(400, {"status": "error", "message": "Interrupted text payload"})
                return
            if len(raw_body) < content_length:
                self._send_json(400, {"status": "error", "message": "Incomplete text payload"})
                return

            text = raw_body.decode("utf-8", errors="replace")
            if "application/json" in content_type:
                try:
                    payload = json.loads(text)
                    text = str(payload.get("text", text))
                except Exception:
                    pass
            ok = WIN_BRIDGE.set_clipboard_text(text)
            if ok:
                HISTORY.mark_clipboard_replaced()
                HISTORY.add_text(text, "Phone -> PC")
            self._send_json(
                200 if ok else 500,
                {"status": "ok" if ok else "error", "type": "text", "length": len(text)},
            )
            return

        if route in {"/api/push-files", "/api/push-image"}:
            effective_max = min(MAX_UPLOAD_BYTES, HISTORY.max_storage_bytes)
            content_length = self._parse_content_length(effective_max)
            if content_length is None:
                return
            if content_length == 0:
                self._send_json(400, {"status": "error", "message": "Empty file payload"})
                return

            try:
                token, staging_dir = HISTORY.begin_upload(content_length)
            except UploadBusyError as exc:
                body = json.dumps({"status": "error", "message": str(exc)}).encode("utf-8")
                try:
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Retry-After", "2")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            committed = False
            try:
                staged_paths: list[Path] = []
                if "multipart/form-data" in content_type and "boundary=" in content_type:
                    boundary_str = content_type.split("boundary=", 1)[1].split(";", 1)[0].strip().strip('"')
                    boundary = boundary_str.encode("ascii", errors="ignore")
                    staged_paths = stream_multipart_to_staged_dir(
                        self.rfile, content_length, boundary, staging_dir
                    )
                else:
                    clean_ctype = content_type.split(";", 1)[0].strip().lower()
                    ext = mimetypes.guess_extension(clean_ctype) or ".png"
                    if clean_ctype in {"image/jpeg", "image/jpg"}:
                        ext = ".jpg"
                    elif clean_ctype == "image/png":
                        ext = ".png"
                    header_name = self.headers.get("X-File-Name", "").strip()
                    if header_name:
                        filename = sanitize_filename(header_name, ext)
                    else:
                        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        prefix = "Screenshot" if ext in IMAGE_EXTENSIONS else "File"
                        filename = f"{prefix}_{timestamp}{ext}"
                    target = staging_dir / filename
                    if not _is_strict_subpath(target, staging_dir):
                        raise IncompleteUploadError("Invalid target filename")

                    remaining = content_length
                    with target.open("wb") as out_f:
                        while remaining > 0:
                            chunk = self.rfile.read(min(65536, remaining))
                            if not chunk:
                                raise IncompleteUploadError("Raw upload interrupted before Content-Length bytes")
                            out_f.write(chunk)
                            remaining -= len(chunk)
                    if target.exists() and target.stat().st_size > 0:
                        staged_paths.append(target)

                if not staged_paths:
                    HISTORY.abort_upload(token, staging_dir)
                    self._send_json(400, {"status": "error", "message": "No valid files parsed"})
                    return

                committed = True
                tx_id, tx_paths, _, ok = HISTORY.commit_upload(
                    token,
                    staging_dir,
                    staged_paths,
                    "Phone -> PC",
                    WIN_BRIDGE.set_clipboard_files_and_optional_image,
                )
                try:
                    try:
                        WIN_BRIDGE.show_incoming_files_toast_async(
                            tx_paths, transfer_id=tx_id, history_store=HISTORY
                        )
                    except TypeError:
                        WIN_BRIDGE.show_incoming_files_toast_async(tx_paths)
                    self._send_json(
                        200 if ok else 500,
                        {
                            "status": "ok" if ok else "error",
                            "count": len(tx_paths),
                            "files": [p.name for p in tx_paths],
                        },
                    )
                finally:
                    HISTORY.release_lease(tx_id)
                return

            except UploadLimitError as exc:
                if not committed:
                    HISTORY.abort_upload(token, staging_dir)
                self._send_json(413, {"status": "error", "message": str(exc)})
                return
            except IncompleteUploadError as exc:
                if not committed:
                    HISTORY.abort_upload(token, staging_dir)
                self._send_json(400, {"status": "error", "message": str(exc)})
                return
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                if not committed:
                    HISTORY.abort_upload(token, staging_dir)
                return
            except Exception as exc:
                if not committed:
                    HISTORY.abort_upload(token, staging_dir)
                self._send_json(500, {"status": "error", "message": f"Upload failed: {exc}"})
                return

        self._send_json(404, {"status": "error", "message": "Unknown endpoint"})


# ==============================================================================
# INSTALLATION OWNERSHIP, PROTOCOL HANDLER & CLI ENTRYPOINT
# ==============================================================================
_HELPER_CLI_FLAGS = (
    "--status",
    "--info",
    "--install-id",
    "--preflight-install",
    "--register-only",
    "--unregister",
    "--install-startup",
    "--uninstall-startup",
    "--uninstall-firewall",
    "--check-firewall",
    "--cleanup",
    "--cleanup-force",
    "--protocol",
)


def parse_protocol_command_script(command_value: str) -> Path | None:
    if not command_value or not isinstance(command_value, str):
        return None
    try:
        tokens = shlex.split(command_value.strip(), posix=False)
    except ValueError:
        return None
    cleaned = [t.strip().strip('"').strip("'") for t in tokens if t.strip()]
    for tok in cleaned:
        if tok.lower().endswith(".py"):
            try:
                return Path(tok).resolve()
            except Exception:
                return None
    return None


def is_protocol_command_owned(command_value: str, script_path: Path | None = None) -> bool:
    parsed = parse_protocol_command_script(command_value)
    if parsed is None:
        return False
    return os.path.normcase(str(parsed)) == canonical_script_path(script_path)


def _read_protocol_registry_command() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    key_path = r"Software\Classes\clipbridge\shell\open\command"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, "")
            return str(val) if val is not None else None
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _protocol_key_exists() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\clipbridge", 0, winreg.KEY_READ):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def get_protocol_status(script_path: Path | None = None) -> dict:
    registered = _protocol_key_exists()
    cmd = _read_protocol_registry_command() if registered else None
    parsed = parse_protocol_command_script(cmd) if cmd else None
    owned = bool(registered and cmd and is_protocol_command_owned(cmd, script_path))
    return {
        "registered": registered,
        "command": cmd,
        "target_script": str(parsed) if parsed else None,
        "owned_by_current": owned,
    }


def register_protocol_handler(script_path: Path | None = None) -> dict:
    if sys.platform != "win32":
        return {"status": "unsupported", "registered": False}
    import winreg

    target_script = (script_path or SCRIPT_PATH).resolve()
    python_exe = Path(sys.executable)
    pythonw_exe = python_exe.with_name("pythonw.exe")
    runner = str(pythonw_exe if pythonw_exe.exists() else python_exe)
    cmd_val = f'"{runner}" "{target_script}" --protocol "%1"'

    prev = get_protocol_status(target_script)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\clipbridge") as base_key:
        winreg.SetValueEx(base_key, "", 0, winreg.REG_SZ, "URL:ClipBridge Protocol")
        winreg.SetValueEx(base_key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\clipbridge\shell\open\command") as cmd_key:
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd_val)

    return {
        "status": "ok",
        "registered": True,
        "replaced_foreign": bool(prev["registered"] and not prev["owned_by_current"]),
        "previous_target": prev["target_script"],
        "command": cmd_val,
    }


def unregister_protocol_handler(script_path: Path | None = None) -> dict:
    if sys.platform != "win32":
        return {"status": "unsupported", "removed": False, "message": "Windows registry not applicable."}
    import winreg

    target_script = (script_path or SCRIPT_PATH).resolve()
    status = get_protocol_status(target_script)
    if not status["registered"]:
        return {"status": "absent", "removed": False, "message": "clipbridge:// protocol is not registered."}
    if not status["owned_by_current"]:
        owner = status["target_script"] or "unknown/unparseable command"
        return {
            "status": "skipped",
            "removed": False,
            "owner": owner,
            "message": f"Skipped clipbridge:// protocol: registered to another installation ({owner}).",
        }

    for subkey in (
        r"Software\Classes\clipbridge\shell\open\command",
        r"Software\Classes\clipbridge\shell\open",
        r"Software\Classes\clipbridge\shell",
        r"Software\Classes\clipbridge",
    ):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
        except FileNotFoundError:
            pass
    return {
        "status": "removed",
        "removed": True,
        "message": "Unregistered clipbridge:// protocol owned by this installation.",
    }


def get_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def build_startup_vbs_content(runner: Path | str, script_path: Path | None = None) -> str:
    target = (script_path or SCRIPT_PATH).resolve()
    iid = compute_install_id(target)
    canon = canonical_script_path(target)
    runner_esc = str(runner).replace('"', '""')
    script_esc = str(target).replace('"', '""')
    return (
        f"' ClipBridge-Install-Id: {iid}\n"
        f"' ClipBridge-Script: {canon}\n"
        'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.Run """{runner_esc}"" ""{script_esc}""", 0, False\n'
    )


def _extract_vbs_script_path(vbs_text: str) -> Path | None:
    if not vbs_text:
        return None
    for line in vbs_text.splitlines():
        m_hdr = re.match(r"^\s*'\s*ClipBridge-Script:\s*(.+?)\s*$", line, re.IGNORECASE)
        if m_hdr:
            try:
                return Path(m_hdr.group(1).strip()).resolve()
            except Exception:
                return None
    m_run = re.search(r'WshShell\.Run\s+.*?"([^"\r\n]+\.py)"', vbs_text, re.IGNORECASE)
    if m_run:
        raw_p = m_run.group(1).replace('""', '"').strip('"')
        try:
            return Path(raw_p).resolve()
        except Exception:
            return None
    return None


def is_startup_launcher_owned(vbs_path: Path, script_path: Path | None = None) -> bool:
    if not vbs_path.is_file():
        return False
    if vbs_path.name.lower() == "clipboardbridge_silentstartup.vbs":
        return False
    target = (script_path or SCRIPT_PATH).resolve()
    expected_canon = canonical_script_path(target)
    expected_id = compute_install_id(target)
    try:
        text = vbs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    m_id = re.search(r"^\s*'\s*ClipBridge-Install-Id:\s*([0-9a-f]{12})\s*$", text, re.IGNORECASE | re.MULTILINE)
    if m_id and m_id.group(1).lower() != expected_id:
        return False

    extracted = _extract_vbs_script_path(text)
    if extracted is None:
        return False
    return os.path.normcase(str(extracted)) == expected_canon


def install_startup_launcher(
    startup_dir: Path | None = None,
    script_path: Path | None = None,
    runner: Path | str | None = None,
) -> Path:
    target = (script_path or SCRIPT_PATH).resolve()
    iid = compute_install_id(target)
    sdir = startup_dir or get_startup_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    if runner is None:
        python_exe = Path(sys.executable)
        pythonw_exe = python_exe.with_name("pythonw.exe")
        runner = pythonw_exe if pythonw_exe.exists() else python_exe
    vbs_path = sdir / f"ClipBridge_{iid}.vbs"
    vbs_path.write_text(build_startup_vbs_content(runner, target), encoding="utf-8")
    return vbs_path


def uninstall_startup_launchers(
    startup_dir: Path | None = None,
    script_path: Path | None = None,
) -> dict:
    target = (script_path or SCRIPT_PATH).resolve()
    iid = compute_install_id(target)
    sdir = startup_dir or get_startup_dir()
    removed: list[str] = []
    skipped: list[str] = []

    if not sdir.exists():
        return {"status": "ok", "removed": removed, "skipped": skipped}

    candidates = [
        sdir / f"ClipBridge_{iid}.vbs",
        sdir / "ClipBridge_SilentStartup.vbs",
    ]
    for cand in candidates:
        if not cand.exists():
            continue
        if is_startup_launcher_owned(cand, target):
            try:
                cand.unlink()
                removed.append(cand.name)
            except OSError as exc:
                skipped.append(f"{cand.name} (delete failed: {exc})")
        else:
            skipped.append(f"{cand.name} (owned by another installation or ambiguous)")

    # Report any foreign ClipBridge_*.vbs launchers transparently without deleting them.
    for other in sorted(sdir.glob("ClipBridge*.vbs")):
        if other.name in removed or other in candidates:
            continue
        skipped.append(f"{other.name} (foreign installation)")

    return {"status": "ok", "removed": removed, "skipped": skipped}


def get_firewall_rule_name(port: int, install_id: str | None = None) -> str:
    iid = install_id or INSTALL_ID
    return f"ClipBridge_{port}_{iid}"


def format_firewall_description(install_id: str | None = None, script_path: Path | None = None) -> str:
    target = (script_path or SCRIPT_PATH).resolve()
    iid = install_id or compute_install_id(target)
    return f"ClipBridge [id={iid}] path={canonical_script_path(target)}"


def parse_firewall_ownership(
    description: str,
    install_id: str | None = None,
    script_path: Path | None = None,
) -> bool:
    if not description or not isinstance(description, str):
        return False
    target = (script_path or SCRIPT_PATH).resolve()
    expected_id = (install_id or compute_install_id(target)).lower()
    expected_path = canonical_script_path(target)
    m = re.match(r"^\s*ClipBridge\s+\[id=([0-9a-f]{12})\]\s+path=(.+?)\s*$", description, re.IGNORECASE)
    if not m:
        return False
    rule_id = m.group(1).lower()
    try:
        rule_path = os.path.normcase(str(Path(m.group(2).strip()).resolve()))
    except Exception:
        return False
    return rule_id == expected_id and rule_path == expected_path


def inspect_firewall_rules_windows(rule_names: list[str]) -> list[dict] | None:
    if sys.platform != "win32":
        return []
    payload = json.dumps({"names": rule_names})
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    ps = f"""
$cfg = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64}')) | ConvertFrom-Json
$out = @()
foreach ($n in $cfg.names) {{
    $rules = @(Get-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue)
    foreach ($r in $rules) {{
        $out += [PSCustomObject]@{{
            Name = [string]$r.Name
            DisplayName = [string]$r.DisplayName
            Description = [string]$r.Description
        }}
    }}
}}
if ($out.Count -eq 0) {{
    Write-Output "[]"
}} else {{
    $json = ConvertTo-Json -InputObject @($out) -Compress
    Write-Output $json
}}
"""
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            startupinfo=startupinfo,
            creationflags=0x08000000,
        )
        if proc.returncode != 0:
            return None
        parsed = json.loads((proc.stdout or "").strip() or "[]")
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return parsed
        return None
    except Exception:
        return None


def _delete_firewall_rule_instance(instance_name: str) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "NotWindows"
    b64 = base64.b64encode(instance_name.encode("utf-8")).decode("ascii")
    ps = f"""
$id = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64}'))
try {{
    Remove-NetFirewallRule -Name $id -ErrorAction Stop
    Write-Output "OK"
}} catch {{
    Write-Output ("ERR:" + $_.Exception.Message)
    exit 1
}}
"""
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            startupinfo=startupinfo,
            creationflags=0x08000000,
        )
        out = (proc.stdout or "").strip()
        return (proc.returncode == 0 and "OK" in out), out
    except Exception as exc:
        return False, str(exc)


def uninstall_owned_firewall_rules(
    port: int,
    install_id: str | None = None,
    script_path: Path | None = None,
) -> dict:
    target = (script_path or SCRIPT_PATH).resolve()
    iid = install_id or compute_install_id(target)
    candidates = [get_firewall_rule_name(port, iid), f"ClipBridge_{port}"]
    rules = inspect_firewall_rules_windows(candidates)
    if rules is None:
        return {
            "status": "aborted",
            "removed": [],
            "skipped": ["Could not verify Windows Firewall rule ownership; failing closed."],
            "needs_elevation": False,
        }

    removed: list[str] = []
    skipped: list[str] = []
    needs_elevation = False
    for rule in rules:
        display_name = str(rule.get("DisplayName", ""))
        instance_name = str(rule.get("Name", ""))
        desc = str(rule.get("Description", ""))
        if not parse_firewall_ownership(desc, iid, target):
            skipped.append(f"{display_name} (untagged or owned by another installation)")
            continue
        ok, err = _delete_firewall_rule_instance(instance_name)
        if ok:
            removed.append(display_name)
        else:
            needs_elevation = True
            skipped.append(f"{display_name} (removal requires Administrator privileges: {err})")

    return {
        "status": "ok",
        "removed": removed,
        "skipped": skipped,
        "needs_elevation": needs_elevation,
    }


def _lock_file_nonblocking(fh) -> bool:
    try:
        if sys.platform == "win32":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_file(fh):
    try:
        if sys.platform == "win32":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def acquire_server_lock(temp_dir: Path, port: int, install_id: str, script_path: Path):
    lock_path = temp_dir / ".server.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+", encoding="utf-8")
    if not _lock_file_nonblocking(fh):
        fh.close()
        raise RuntimeError(f"Another ClipBridge instance for this installation already holds {lock_path}")
    fh.seek(0)
    fh.truncate(0)
    fh.write(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": port,
                "install_id": install_id,
                "script_path": canonical_script_path(script_path),
            }
        )
    )
    fh.flush()
    return fh


def release_server_lock(fh):
    if fh is None:
        return
    try:
        _unlock_file(fh)
    finally:
        try:
            fh.close()
        except OSError:
            pass


def acquire_cleanup_lock(temp_dir: Path):
    if not temp_dir.is_dir():
        return None
    lock_path = temp_dir / ".server.lock"
    fh = lock_path.open("a+", encoding="utf-8")
    if not _lock_file_nonblocking(fh):
        fh.close()
        raise RuntimeError(f"Another ClipBridge instance for this installation already holds {lock_path}")
    return fh


def is_server_lock_held(temp_dir: Path) -> bool:
    lock_path = temp_dir / ".server.lock"
    if not lock_path.exists():
        return False
    try:
        with lock_path.open("r+", encoding="utf-8") as fh:
            if _lock_file_nonblocking(fh):
                _unlock_file(fh)
                return False
            return True
    except OSError:
        return True


def _is_command_line_for_script(cmd_line: str, script_path: Path) -> bool:
    if not cmd_line or not isinstance(cmd_line, str):
        return False
    expected = canonical_script_path(script_path)
    try:
        tokens = shlex.split(cmd_line, posix=False)
    except ValueError:
        tokens = cmd_line.split()
    cleaned = [t.strip().strip('"').strip("'") for t in tokens if t.strip()]
    if any(tok in _HELPER_CLI_FLAGS for tok in cleaned):
        return False
    for tok in cleaned:
        if tok.lower().endswith(".py"):
            try:
                if os.path.normcase(str(Path(tok).resolve())) == expected:
                    return True
            except Exception:
                pass
    return False


def _is_port_listening(port: int) -> bool | None:
    # 1. OS listener check first on Windows (avoids waiting 2s on closed loopback ports if occupied)
    if sys.platform == "win32":
        ps = (
            f"$c = @(Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue); "
            "Write-Output $c.Count"
        )
        encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=8,
                startupinfo=startupinfo,
                creationflags=0x08000000,
            )
            if proc.returncode != 0:
                return None
            count_val = int((proc.stdout or "").strip())
            if count_val > 0:
                return True
        except Exception:
            return None

    # 2. Direct loopback connect probe (Windows SYN retry takes ~2.05s before WSAECONNREFUSED)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3.5):
            return True
    except ConnectionRefusedError:
        if sys.platform == "win32":
            return False
    except OSError as exc:
        if exc.errno in {errno.ECONNREFUSED, 10061}:
            if sys.platform == "win32":
                return False
        return None

    # POSIX bind probe
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
            return False
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, 98, 48}:
            return True
        return None


def _verify_no_running_clipbridge_process(script_path: Path) -> tuple[bool, str]:
    target = script_path.resolve()
    if sys.platform == "win32":
        ps = (
            "$procs = @(Get-CimInstance Win32_Process | Where-Object { "
            "$_.Name -match '^(?:python(?:w|3(?:\\.\\d+)?)?|pyw?)\\.exe$' "
            "} | Select-Object ProcessId, CommandLine); "
            "if ($procs.Count -eq 0) { Write-Output '[]' } "
            "else { ConvertTo-Json -InputObject @($procs) -Compress }"
        )
        encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                startupinfo=startupinfo,
                creationflags=0x08000000,
            )
            if proc.returncode != 0:
                return False, "Process inspection failed; refusing offline cleanup."
            raw = (proc.stdout or "").strip() or "[]"
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = [parsed]
            if not isinstance(parsed, list):
                return False, "Unexpected process inspection output; refusing offline cleanup."
        except Exception as exc:
            return False, f"Process inspection unavailable ({exc}); refusing offline cleanup."

        my_pid = os.getpid()
        for entry in parsed:
            pid = entry.get("ProcessId")
            if pid == my_pid:
                continue
            cmd = entry.get("CommandLine")
            if cmd is None or not str(cmd).strip():
                return (
                    False,
                    f"Opaque Python process (PID {pid}) prevents verifying server state; refusing offline cleanup.",
                )
            if _is_command_line_for_script(str(cmd), target):
                return False, f"ClipBridge server for this installation is running (PID {pid})."
        return True, "verified_stopped"

    # POSIX / Linux inspection via /proc when available
    proc_root = Path("/proc")
    if proc_root.is_dir():
        my_pid = str(os.getpid())
        for pid_dir in proc_root.iterdir():
            if not pid_dir.name.isdigit() or pid_dir.name == my_pid:
                continue
            try:
                raw_cmd = (pid_dir / "cmdline").read_bytes()
            except OSError:
                continue
            if not raw_cmd:
                continue
            parts = [p.decode("utf-8", errors="replace") for p in raw_cmd.split(b"\x00") if p]
            if parts and "python" in Path(parts[0]).name.lower():
                joined = " ".join(f'"{p}"' if " " in p else p for p in parts)
                if _is_command_line_for_script(joined, target):
                    return False, f"ClipBridge server for this installation is running (PID {pid_dir.name})."
    return True, "verified_stopped"


def handle_protocol_invocation(uri: str):
    uri_lower = uri.lower().strip().rstrip("/")
    if uri_lower.endswith("://save"):
        saved = save_staged_files_to_downloads(None)
        if sys.platform == "win32":
            if saved:
                if len(saved) == 1:
                    WIN_BRIDGE.show_simple_toast("Saved to Downloads", f"Saved as {saved[0].name}")
                else:
                    WIN_BRIDGE.show_simple_toast("Saved to Downloads", f"Saved {len(saved)} files to {SAVE_FOLDER}")
            else:
                WIN_BRIDGE.show_simple_toast("Nothing to Save", "No staged ClipBridge files are currently available.")
    elif uri_lower.endswith("://open"):
        SAVE_FOLDER.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(SAVE_FOLDER))


def _is_connection_refused(exc: BaseException) -> bool:
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, URLError):
        return _is_connection_refused(exc.reason)
    if isinstance(exc, OSError) and exc.errno in {errno.ECONNREFUSED, 10061}:
        return True
    return False


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, URLError):
        return _is_timeout_error(exc.reason)
    return False


def _aborted_cleanup(reason: str, warning: str) -> dict:
    return {
        "status": "aborted",
        "ok": False,
        "reason": reason,
        "reclaimed_bytes": 0,
        "reclaimed_human": "0 B",
        "reclaimed_count": 0,
        "preserved_active_count": 0,
        "preserved_foreign_count": 0,
        "warning": warning,
    }


def run_cli_cleanup(
    force: bool = False,
    history_store: TransferHistory | None = None,
    port: int | None = None,
    secret_key: str | None = None,
) -> dict:
    """
    Fail-closed cleanup:
    1. Delegates to a running authenticated ClipBridge server on PORT.
    2. Aborts immediately on HTTP 401/403, unexpected HTTP responses, timeouts,
       occupied ports, running/opaque processes, active server locks, or unverified storage ownership.
    3. Executes offline cleanup ONLY after proving the server is stopped and storage belongs to this installation.
    """
    store = history_store or HISTORY
    target_port = PORT if port is None else int(port)
    key = SECRET_KEY if secret_key is None else str(secret_key)

    url = f"http://127.0.0.1:{target_port}/api/cleanup"
    body = json.dumps({"force": bool(force)}).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "X-Secret-Key": key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict) or data.get("status") != "ok":
                return _aborted_cleanup("unexpected_response", "Unexpected response from server; cleanup aborted.")
            data.setdefault("ok", True)
            return data
    except HTTPError as err:
        code = err.code
        err.close()
        if code in {401, 403}:
            return _aborted_cleanup("unauthorized", f"Server rejected authentication (HTTP {code}); offline cleanup aborted.")
        return _aborted_cleanup("unexpected_http_service", f"Port {target_port} responded with HTTP {code}; offline cleanup aborted.")
    except (http.client.HTTPException, json.JSONDecodeError) as exc:
        return _aborted_cleanup("unexpected_service", f"Non-ClipBridge or invalid service on port {target_port} ({exc}); cleanup aborted.")
    except Exception as exc:
        if _is_timeout_error(exc):
            return _aborted_cleanup("timeout", f"Cleanup request timed out on port {target_port}; offline cleanup aborted.")
        if not _is_connection_refused(exc):
            return _aborted_cleanup("network_error", f"Could not verify port {target_port} ({exc}); offline cleanup aborted.")

    # Only reached on confirmed ConnectionRefusedError: acquire the installation lock across the
    # entire offline cleanup critical section and recheck all invariants under the held lock.
    lock_file_path = store.temp_dir / ".server.lock"
    lock_existed_before = lock_file_path.exists()
    try:
        lock_fh = acquire_cleanup_lock(store.temp_dir)
    except Exception:
        return _aborted_cleanup("server_lock_held", "Active ClipBridge server lock detected; offline cleanup aborted.")

    cleanup_owner_verified = False
    try:
        port_listening = _is_port_listening(target_port)
        if port_listening is True:
            return _aborted_cleanup("port_occupied", f"Port {target_port} is occupied by another listener; offline cleanup aborted.")
        if port_listening is None:
            return _aborted_cleanup("port_unverifiable", f"Could not verify whether port {target_port} is free; offline cleanup aborted.")

        proc_ok, proc_msg = _verify_no_running_clipbridge_process(store.script_path)
        if not proc_ok:
            return _aborted_cleanup("process_unverifiable", proc_msg)

        owner_ok, owner_msg = store.verify_storage_ownership()
        if not owner_ok:
            return _aborted_cleanup(
                "ownership_unverified",
                f"Storage ownership could not be verified ({owner_msg}); offline cleanup refused.",
            )
        cleanup_owner_verified = True

        return store.cleanup_orphans_on_startup(force=force, require_verified_owner=True)
    finally:
        release_server_lock(lock_fh)
        if lock_fh is not None and not lock_existed_before and not cleanup_owner_verified:
            try:
                lock_file_path.unlink(missing_ok=True)
            except OSError:
                pass


def run_install_preflight(
    port: int | None = None,
    script_path: Path | None = None,
    startup_dir: Path | None = None,
) -> dict:
    """
    Non-destructive preflight before installation (`manage.bat` option [1]):
    Detects whether another application or foreign/legacy ClipBridge installation
    occupies the target TCP port, owns `clipbridge://`, or has a legacy/foreign
    startup launcher before mutating registry, startup, or config.json.
    """
    target_script = (script_path or SCRIPT_PATH).resolve()
    target_port = PORT if port is None else int(port)
    sdir = startup_dir or get_startup_dir()
    conflicts: list[dict] = []

    port_state = _is_port_listening(target_port)
    if port_state is True:
        conflicts.append(
            {
                "kind": "port_occupied",
                "message": f"TCP port {target_port} is already in use by another running process or legacy ClipBridge instance.",
            }
        )
    elif port_state is None:
        conflicts.append(
            {
                "kind": "port_unverifiable",
                "message": f"Could not verify whether TCP port {target_port} is free (failing closed).",
            }
        )

    proto = get_protocol_status(target_script)
    if proto.get("registered") and not proto.get("owned_by_current"):
        owner_desc = proto.get("target_script") or proto.get("command") or "unknown installation"
        conflicts.append(
            {
                "kind": "foreign_protocol",
                "owner": owner_desc,
                "message": f"Protocol handler clipbridge:// is currently registered to another installation ({owner_desc}).",
            }
        )

    if sdir.is_dir():
        legacy_vbs = sdir / "ClipboardBridge_SilentStartup.vbs"
        if legacy_vbs.exists():
            conflicts.append(
                {
                    "kind": "legacy_startup",
                    "launcher": legacy_vbs.name,
                    "message": f"Legacy startup launcher '{legacy_vbs.name}' exists in Windows Startup ({sdir}).",
                }
            )
        for cand in sorted(sdir.glob("ClipBridge*.vbs")):
            if not is_startup_launcher_owned(cand, target_script):
                conflicts.append(
                    {
                        "kind": "foreign_startup",
                        "launcher": cand.name,
                        "message": f"Startup launcher '{cand.name}' belongs to another ClipBridge installation.",
                    }
                )

    return {
        "ok": len(conflicts) == 0,
        "status": "ok" if len(conflicts) == 0 else "conflict",
        "port": target_port,
        "script_path": str(target_script),
        "conflicts": conflicts,
    }


def print_status_banner(
    include_protocol: bool = False,
    read_only: bool = False,
    config_path: Path | None = None,
):
    cfg_path = config_path or CONFIG_PATH
    configured = (not read_only) or _has_persisted_config(cfg_path)
    local_ip = get_local_ip()
    if configured:
        cfg_display = str(cfg_path)
        key_display = SECRET_KEY
        web_url = f"http://{local_ip}:{PORT}/?key={SECRET_KEY}"
        import_url = f"http://{local_ip}:{PORT}/setup/shortcuts.json?key={SECRET_KEY}"
    else:
        cfg_display = f"{cfg_path} (not created yet)"
        key_display = "[Not configured yet — run manage.bat option [1] to generate config.json]"
        web_url = f"http://{local_ip}:{PORT}/?key=<generated-on-install>"
        import_url = f"http://{local_ip}:{PORT}/setup/shortcuts.json?key=<generated-on-install>"
    print("======================================================================")
    print(f"  ClipBridge v{VERSION} — By Zeyad Ahmed")
    print("======================================================================")
    print(f"  Install ID         : {INSTALL_ID}")
    print(f"  Config File        : {cfg_display}")
    print(f"  Save Folder        : {SAVE_FOLDER}")
    print(f"  Temp Storage Dir   : {TEMP_DIR}")
    print(f"  Temp Storage Cap   : {format_size(MAX_STORAGE_BYTES)} (TTL {max(1, int(RETENTION_SECONDS // 60))} min)")
    if include_protocol:
        proto = get_protocol_status(SCRIPT_PATH)
        if not proto["registered"]:
            proto_desc = "Not registered"
        elif proto["owned_by_current"]:
            proto_desc = "Registered (this installation)"
        else:
            proto_desc = f"Registered to another installation ({proto['target_script'] or 'unknown'})"
        print(f"  Protocol Handler   : {proto_desc}")
    print(f"  Local Server URL   : http://{local_ip}:{PORT}")
    print(f"  Secret Key         : {key_display}")
    print("----------------------------------------------------------------------")
    print("  1-Click Android 'Import from URL' (Paste in HTTP Shortcuts):")
    print(f"  {import_url}")
    print("----------------------------------------------------------------------")
    print("  Web Dashboard & History UI:")
    print(f"  {web_url}")
    print("======================================================================")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)

    if len(args) == 2 and args[0] == "--protocol":
        handle_protocol_invocation(args[1])
        return 0

    if len(args) == 1 and args[0] in {"--status", "--info"}:
        print_status_banner(include_protocol=True, read_only=True)
        return 0

    if len(args) == 1 and args[0] == "--install-id":
        print(INSTALL_ID)
        return 0

    if len(args) == 1 and args[0] == "--preflight-install":
        res = run_install_preflight(PORT, SCRIPT_PATH)
        if not res["ok"]:
            print("[ABORTED] Installation preflight detected an existing installation or port conflict:")
            for item in res["conflicts"]:
                print(f"  - {item['message']}")
            print("  Migration guidance: Stop and uninstall the older/foreign ClipBridge installation")
            print("  (or free the configured port and remove foreign clipbridge:// / Startup entries)")
            print("  before running option [1]. Existing installations were left untouched.")
            return 1
        print(f"[OK] Installation preflight passed (port {res['port']} free, no foreign integration conflicts).")
        return 0

    if len(args) == 1 and args[0] == "--register-only":
        ensure_config_persisted()
        reg_res = register_protocol_handler(SCRIPT_PATH)
        if reg_res.get("replaced_foreign"):
            print(f"[Protocol] Updated clipbridge:// from previous target ({reg_res.get('previous_target')}).")
        print_status_banner(include_protocol=True, read_only=False)
        return 0

    if len(args) == 1 and args[0] == "--unregister":
        res = unregister_protocol_handler(SCRIPT_PATH)
        prefix = "[REMOVED]" if res.get("removed") else ("[SKIPPED]" if res.get("status") == "skipped" else "[INFO]")
        print(f"{prefix} {res.get('message', '')}")
        return 0

    if len(args) == 1 and args[0] == "--install-startup":
        vbs = install_startup_launcher()
        print(f"[INSTALLED] Startup launcher: {vbs}")
        return 0

    if len(args) == 1 and args[0] == "--uninstall-startup":
        res = uninstall_startup_launchers()
        for name in res["removed"]:
            print(f"[REMOVED] Startup launcher: {name}")
        for msg in res["skipped"]:
            print(f"[SKIPPED] Startup launcher: {msg}")
        return 0

    if len(args) == 1 and args[0] == "--uninstall-firewall":
        res = uninstall_owned_firewall_rules(PORT, INSTALL_ID, SCRIPT_PATH)
        for name in res["removed"]:
            print(f"[REMOVED] Firewall rule: {name}")
        for msg in res["skipped"]:
            print(f"[SKIPPED] Firewall rule: {msg}")
        if res.get("needs_elevation"):
            print("[WARNING] Administrator rights are required to delete this installation's firewall rule.")
            return 2
        return 0 if res.get("status") == "ok" else 1

    if len(args) == 1 and args[0] == "--check-firewall":
        rule_name = get_firewall_rule_name(PORT, INSTALL_ID)
        rules = inspect_firewall_rules_windows([rule_name])
        if rules is None:
            return 2
        if not rules:
            return 1
        owned = all(parse_firewall_ownership(str(r.get("Description", "")), INSTALL_ID, SCRIPT_PATH) for r in rules)
        return 0 if owned else 2

    if len(args) == 1 and args[0] in {"--cleanup", "--cleanup-force"}:
        force = args[0] == "--cleanup-force"
        result = run_cli_cleanup(force=force)
        if result.get("status") == "aborted":
            print(f"[ABORTED] {result.get('warning', 'Cleanup aborted safely.')}")
            return 1
        print(f"Reclaimed : {result.get('reclaimed_human', '0 B')} ({result.get('reclaimed_count', 0)} item(s))")
        if result.get("warning"):
            print(f"Warning   : {result['warning']}")
        return 0

    if args:
        print(f"[ERROR] Invalid or unrecognized arguments: {' '.join(args)}", file=sys.stderr)
        return 2

    # Normal server mode: never auto-register clipbridge:// protocol!
    ensure_config_persisted()
    HISTORY.ensure_storage_ownership()
    lock_fh = acquire_server_lock(HISTORY.temp_dir, PORT, INSTALL_ID, SCRIPT_PATH)
    try:
        startup_res = HISTORY.cleanup_orphans_on_startup(force=False)
        print_status_banner(include_protocol=True)
        if startup_res.get("reclaimed_count"):
            print(
                f"[Storage] Reclaimed {startup_res['reclaimed_human']} from previous session "
                f"({startup_res['reclaimed_count']} orphaned item(s))."
            )
        if startup_res.get("warning"):
            print(f"[Storage] {startup_res['warning']}")

        stop_event = threading.Event()

        def _maintenance_loop():
            while not stop_event.wait(60):
                try:
                    HISTORY.cleanup(force=False)
                except Exception:
                    pass

        threading.Thread(target=_maintenance_loop, daemon=True).start()
        server = ClipBridgeHTTPServer(("0.0.0.0", PORT), ClipboardRequestHandler)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
            server.server_close()
    finally:
        release_server_lock(lock_fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
