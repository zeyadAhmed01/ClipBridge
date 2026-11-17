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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

VERSION = "1.0.0"
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_TEXT_BYTES = 2 * 1024 * 1024  # 2 MB cap for text payloads


# ==============================================================================
# AUTO-CONFIGURATION (config.json)
# ==============================================================================
def load_or_create_config() -> dict:
    default_downloads = str(Path.home() / "Downloads")
    default_cfg = {
        "port": 5050,
        "secret_key": f"clip-{secrets.token_hex(16)}",
        "save_folder": default_downloads,
        "max_history_items": 25,
        "max_upload_mb": 512,
    }
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged = {**default_cfg, **loaded}
            if merged != loaded:
                CONFIG_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
            return merged
        except Exception:
            pass

    try:
        CONFIG_PATH.write_text(json.dumps(default_cfg, indent=2), encoding="utf-8")
    except Exception:
        pass
    return default_cfg


CONFIG = load_or_create_config()
PORT = int(CONFIG.get("port", 5050))
SECRET_KEY = str(CONFIG.get("secret_key", "clip-default-key")).strip()
SAVE_FOLDER = Path(CONFIG.get("save_folder", str(Path.home() / "Downloads")))
MAX_HISTORY = int(CONFIG.get("max_history_items", 25))
MAX_UPLOAD_BYTES = max(1, int(CONFIG.get("max_upload_mb", 512))) * 1024 * 1024

TEMP_DIR = Path(tempfile.gettempdir()) / "ClipBridge"
STAGED_DIR = TEMP_DIR / "staged"
HISTORY_FILES_DIR = TEMP_DIR / "history_files"
PC_CLIPBOARD_IMAGE_PATH = TEMP_DIR / "pc_clipboard.png"

for _d in (TEMP_DIR, STAGED_DIR, HISTORY_FILES_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ==============================================================================
# IN-MEMORY RECENT HISTORY TRACKER
# ==============================================================================
class TransferHistory:
    def __init__(self, maxlen: int = 25):
        self.lock = threading.Lock()
        self.items = deque(maxlen=maxlen)
        self._counter = 0

    def add_text(self, text: str, direction: str) -> dict:
        with self.lock:
            self._counter += 1
            entry = {
                "id": self._counter,
                "type": "text",
                "direction": direction,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "preview": text[:160] + ("..." if len(text) > 160 else ""),
                "content": text,
            }
            self.items.appendleft(entry)
            return entry

    def add_files(self, file_paths: list[Path], direction: str) -> dict:
        with self.lock:
            self._counter += 1
            entry_id = self._counter
            saved_records = []
            entry_dir = HISTORY_FILES_DIR / f"item_{entry_id}"
            entry_dir.mkdir(parents=True, exist_ok=True)

            for idx, src in enumerate(file_paths):
                if not src.is_file():
                    continue
                dst = entry_dir / src.name
                if src.resolve() != dst.resolve():
                    try:
                        shutil.copy2(src, dst)
                    except Exception:
                        dst = src
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

            entry = {
                "id": entry_id,
                "type": "files",
                "direction": direction,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "files": saved_records,
            }
            self.items.appendleft(entry)
            return entry

    def list_items(self) -> list[dict]:
        with self.lock:
            out = []
            for item in self.items:
                copy_item = dict(item)
                if copy_item["type"] == "files":
                    copy_item["files"] = [
                        {k: v for k, v in f.items() if k != "path"}
                        for f in item["files"]
                    ]
                out.append(copy_item)
            return out

    def get_file_path(self, entry_id: int, file_idx: int) -> Path | None:
        with self.lock:
            for item in self.items:
                if item["id"] == entry_id and item["type"] == "files":
                    for f in item["files"]:
                        if f["index"] == file_idx:
                            p = Path(f["path"])
                            return p if p.is_file() else None
            return None

    def clear(self):
        with self.lock:
            self.items.clear()
            if HISTORY_FILES_DIR.exists():
                shutil.rmtree(HISTORY_FILES_DIR, ignore_errors=True)
                HISTORY_FILES_DIR.mkdir(parents=True, exist_ok=True)


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

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p


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


class WindowsBridge:
    def __init__(self):
        self.lock = threading.Lock()

    def set_clipboard_text(self, text: str) -> bool:
        with self.lock:
            return win32_set_clipboard_text(text)

    def set_clipboard_files_and_optional_image(self, file_paths: list[Path]) -> bool:
        if not file_paths:
            return False

        add_lines = []
        for p in file_paths:
            esc = str(p.resolve()).replace("'", "''")
            add_lines.append(f"$null = $sc.Add('{esc}')")
        add_script = "\n".join(add_lines)

        single_image_path = None
        if len(file_paths) == 1 and file_paths[0].suffix.lower() in IMAGE_EXTENSIONS:
            single_image_path = str(file_paths[0].resolve()).replace("'", "''")

        if single_image_path:
            ps = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$dataObj = New-Object System.Windows.Forms.DataObject
$sc = New-Object System.Collections.Specialized.StringCollection
{add_script}
$dataObj.SetFileDropList($sc)
$img = [System.Drawing.Image]::FromFile('{single_image_path}')
try {{
    $dataObj.SetImage($img)
    [System.Windows.Forms.Clipboard]::SetDataObject($dataObj, $true)
    Write-Output "OK"
}} finally {{
    $img.Dispose()
}}
"""
        else:
            ps = f"""
Add-Type -AssemblyName System.Windows.Forms
$sc = New-Object System.Collections.Specialized.StringCollection
{add_script}
[System.Windows.Forms.Clipboard]::SetFileDropList($sc)
Write-Output "OK"
"""
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

            escaped_img_path = str(PC_CLIPBOARD_IMAGE_PATH.resolve()).replace("'", "''")
            ps = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
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
        $img.Save('{escaped_img_path}', [System.Drawing.Imaging.ImageFormat]::Png)
        $img.Dispose()
        Write-Output "TYPE:IMAGE"
        exit 0
    }}
}}
Write-Output "TYPE:EMPTY"
"""
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
            if res.startswith("TYPE:IMAGE") and PC_CLIPBOARD_IMAGE_PATH.exists():
                return {"type": "image", "path": PC_CLIPBOARD_IMAGE_PATH}
            if has_text:
                txt = win32_get_clipboard_text()
                if txt:
                    return {"type": "text", "data": txt}
            return {"type": "empty"}

    def show_incoming_files_toast_async(self, file_paths: list[Path]):
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

        ps = f"""
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
$xmlString = @"
<toast activationType="protocol" launch="clipbridge://noop">
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
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($xmlString)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""
        threading.Thread(target=run_ps_oneshot, args=(ps, 10), daemon=True).start()

    def show_simple_toast(self, title: str, message: str):
        safe_title = _xml_escape(title)
        safe_msg = _xml_escape(message)
        ps = f"""
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
$xmlString = @"
<toast>
    <visual>
        <binding template="ToastGeneric">
            <text>{safe_title}</text>
            <text>{safe_msg}</text>
        </binding>
    </visual>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($xmlString)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""
        threading.Thread(target=run_ps_oneshot, args=(ps, 10), daemon=True).start()


WIN_BRIDGE = WindowsBridge()
CURRENT_PC_FILES: list[Path] = []


def sanitize_filename(name: str, default_ext: str = ".bin") -> str:
    name = os.path.basename(unquote(name.strip()))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.rstrip(" .")
    if not name or name in {".", ".."}:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return f"Shared_{timestamp}{default_ext}"
    stem = Path(name).stem.upper()
    if stem in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name[:180]


def save_staged_files_to_downloads() -> list[Path]:
    if not STAGED_DIR.exists():
        return []
    staged_files = sorted([p for p in STAGED_DIR.iterdir() if p.is_file()])
    if not staged_files:
        return []

    SAVE_FOLDER.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    for src in staged_files:
        dest = SAVE_FOLDER / src.name
        counter = 1
        while dest.exists():
            dest = SAVE_FOLDER / f"{src.stem}_{counter}{src.suffix}"
            counter += 1
        shutil.copy2(src, dest)
        saved_paths.append(dest)
    return saved_paths


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
    """
    saved_paths: list[Path] = []
    delim = b"--" + boundary
    chunk_size = 65536
    remaining = content_length
    buf = b""

    state = "FIND_BOUNDARY"
    current_file = None
    current_path: Path | None = None

    while remaining > 0 or buf:
        if remaining > 0 and len(buf) < chunk_size * 2:
            to_read = min(chunk_size, remaining)
            chunk = rfile.read(to_read)
            if not chunk:
                remaining = 0
            else:
                remaining -= len(chunk)
                buf += chunk

        if state == "FIND_BOUNDARY":
            idx = buf.find(delim)
            if idx == -1:
                if remaining == 0:
                    break
                buf = buf[-len(delim) :]
                continue
            buf = buf[idx + len(delim) :]
            if buf.startswith(b"--"):
                break
            if buf.startswith(b"\r\n"):
                buf = buf[2:]
            state = "READ_HEADERS"

        elif state == "READ_HEADERS":
            hdr_end = buf.find(b"\r\n\r\n")
            if hdr_end == -1:
                if remaining == 0 or len(buf) > 32768:
                    break
                continue

            header_bytes = buf[:hdr_end]
            buf = buf[hdr_end + 4 :]
            headers_text = header_bytes.decode("utf-8", errors="replace")

            if "Content-Disposition" not in headers_text:
                state = "FIND_BOUNDARY"
                continue

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

            current_path = target_path
            current_file = target_path.open("wb")
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
                if buf.startswith(b"--"):
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
                    if current_file:
                        current_file.write(buf)
                        current_file.close()
                        current_file = None
                    if current_path and current_path.exists() and current_path.stat().st_size > 0:
                        saved_paths.append(current_path)
                    buf = b""
                    break

    if current_file:
        current_file.close()

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
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <h2>3. Recent Transfer History</h2>
      <button class="ghost" onclick="clearHistory()">Clear</button>
    </div>
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
  }
}

async function loadHistory() {
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
    dirSpan.textContent = item.direction;
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

async function clearHistory() {
  await fetch(`/api/history/clear?key=${encodeURIComponent(KEY)}`, { method: 'POST' });
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
class ClipboardRequestHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        # Do not expose bearer-key URLs via referrer headers; disable MIME sniffing.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, format, *args):
        if sys.stdout and not sys.executable.lower().endswith("pythonw.exe"):
            msg = re.sub(r"(key=)[^&\s]+", r"\1***", format % args)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.client_address[0]} - {msg}", flush=True)

    def _is_authorized(self) -> bool:
        header_key = self.headers.get("X-Secret-Key", "").strip()
        parsed = urlparse(self.path)
        query_key = parse_qs(parsed.query).get("key", [""])[0].strip()
        return secrets.compare_digest(header_key, SECRET_KEY) or secrets.compare_digest(query_key, SECRET_KEY)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _parse_content_length(self, max_bytes: int) -> int | None:
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

        if route == "/api/pull":
            clip = WIN_BRIDGE.get_clipboard_content()
            if clip["type"] == "text":
                HISTORY.add_text(clip["data"], "PC -> Phone")
                self._send_json(200, {"type": "text", "content": clip["data"]})
            elif clip["type"] == "image":
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
                CURRENT_PC_FILES = clip["paths"]
                HISTORY.add_files(CURRENT_PC_FILES, "PC -> Phone (Files)")
                file_entries = []
                for idx, p in enumerate(CURRENT_PC_FILES):
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
            if clip["type"] == "image" and clip["path"].exists():
                data = clip["path"].read_bytes()
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

            if not CURRENT_PC_FILES:
                clip = WIN_BRIDGE.get_clipboard_content()
                if clip["type"] == "files":
                    CURRENT_PC_FILES = clip["paths"]

            if idx < 0 or idx >= len(CURRENT_PC_FILES) or not CURRENT_PC_FILES[idx].is_file():
                self._send_json(404, {"status": "error", "message": "File no longer in Windows clipboard"})
                return

            self._serve_file_attachment(CURRENT_PC_FILES[idx])
            return

        if route.startswith("/api/history-file/"):
            parts = route.split("/api/history-file/", 1)[1].split("/")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                target = HISTORY.get_file_path(int(parts[0]), int(parts[1]))
                if target and target.is_file():
                    self._serve_file_attachment(target)
                    return
            self._send_json(404, {"status": "error", "message": "History file not found"})
            return

        if route == "/api/save-latest":
            saved_paths = save_staged_files_to_downloads()
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
        file_size = target_file.stat().st_size
        encoded_name = quote(target_file.name)

        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"{target_file.name}\"; filename*=UTF-8''{encoded_name}",
        )
        self.end_headers()
        with target_file.open("rb") as f:
            shutil.copyfileobj(f, self.wfile, length=65536)

    def do_POST(self):
        if not self._is_authorized():
            self._send_json(401, {"status": "error", "message": "Invalid or missing secret key"})
            return

        parsed = urlparse(self.path)
        route = parsed.path
        content_type = self.headers.get("Content-Type", "")

        if route == "/api/history/clear":
            HISTORY.clear()
            self._send_json(200, {"status": "ok"})
            return

        if route == "/api/push-text":
            content_length = self._parse_content_length(MAX_TEXT_BYTES)
            if content_length is None:
                return
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""
            text = raw_body.decode("utf-8", errors="replace")
            if "application/json" in content_type:
                try:
                    payload = json.loads(text)
                    text = str(payload.get("text", text))
                except Exception:
                    pass
            ok = WIN_BRIDGE.set_clipboard_text(text)
            if ok:
                HISTORY.add_text(text, "Phone -> PC")
            self._send_json(
                200 if ok else 500,
                {"status": "ok" if ok else "error", "type": "text", "length": len(text)},
            )
            return

        if route in {"/api/push-files", "/api/push-image"}:
            content_length = self._parse_content_length(MAX_UPLOAD_BYTES)
            if content_length is None:
                return
            if content_length == 0:
                self._send_json(400, {"status": "error", "message": "Empty file payload"})
                return

            STAGED_DIR.mkdir(parents=True, exist_ok=True)
            for old_file in STAGED_DIR.iterdir():
                if old_file.is_file():
                    try:
                        old_file.unlink()
                    except Exception:
                        pass

            staged_paths: list[Path] = []
            if "multipart/form-data" in content_type and "boundary=" in content_type:
                boundary_str = content_type.split("boundary=", 1)[1].split(";", 1)[0].strip().strip('"')
                boundary = boundary_str.encode("ascii", errors="ignore")
                staged_paths = stream_multipart_to_staged_dir(
                    self.rfile, content_length, boundary, STAGED_DIR
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
                target = STAGED_DIR / filename
                remaining = content_length
                with target.open("wb") as out_f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        out_f.write(chunk)
                        remaining -= len(chunk)
                if target.exists() and target.stat().st_size > 0:
                    staged_paths.append(target)

            if not staged_paths:
                self._send_json(400, {"status": "error", "message": "No valid files parsed"})
                return

            ok = WIN_BRIDGE.set_clipboard_files_and_optional_image(staged_paths)
            HISTORY.add_files(staged_paths, "Phone -> PC")
            WIN_BRIDGE.show_incoming_files_toast_async(staged_paths)

            self._send_json(
                200 if ok else 500,
                {
                    "status": "ok" if ok else "error",
                    "count": len(staged_paths),
                    "files": [p.name for p in staged_paths],
                },
            )
            return

        self._send_json(404, {"status": "error", "message": "Unknown endpoint"})


# ==============================================================================
# PROTOCOL HANDLER & CLI ENTRYPOINT
# ==============================================================================
def register_protocol_handler():
    if sys.platform != "win32":
        return
    python_exe = Path(sys.executable)
    pythonw_exe = python_exe.with_name("pythonw.exe")
    runner = str(pythonw_exe if pythonw_exe.exists() else python_exe)
    script_path = str(Path(__file__).resolve())

    ps_reg = f"""
$baseKey = 'HKCU:\\Software\\Classes\\clipbridge'
New-Item -Path $baseKey -Force | Out-Null
Set-ItemProperty -Path $baseKey -Name '(Default)' -Value 'URL:ClipBridge Protocol'
Set-ItemProperty -Path $baseKey -Name 'URL Protocol' -Value ''
$cmdKey = "$baseKey\\shell\\open\\command"
New-Item -Path $cmdKey -Force | Out-Null
$cmdVal = '"{runner}" "{script_path}" --protocol "%1"'
Set-ItemProperty -Path $cmdKey -Name '(Default)' -Value $cmdVal
"""
    run_ps_oneshot(ps_reg, timeout_sec=10)


def unregister_protocol_handler():
    if sys.platform != "win32":
        return
    ps_unreg = """
if (Test-Path 'HKCU:\\Software\\Classes\\clipbridge') {
    Remove-Item -Path 'HKCU:\\Software\\Classes\\clipbridge' -Recurse -Force
}
"""
    run_ps_oneshot(ps_unreg, timeout_sec=10)


def handle_protocol_invocation(uri: str):
    uri_lower = uri.lower().strip().rstrip("/")
    if uri_lower.endswith("://save"):
        saved = save_staged_files_to_downloads()
        if saved and sys.platform == "win32":
            if len(saved) == 1:
                WIN_BRIDGE.show_simple_toast("Saved to Downloads", f"Saved as {saved[0].name}")
            else:
                WIN_BRIDGE.show_simple_toast("Saved to Downloads", f"Saved {len(saved)} files to {SAVE_FOLDER}")
    elif uri_lower.endswith("://open"):
        SAVE_FOLDER.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(SAVE_FOLDER))


def print_status_banner():
    local_ip = get_local_ip()
    web_url = f"http://{local_ip}:{PORT}/?key={SECRET_KEY}"
    import_url = f"http://{local_ip}:{PORT}/setup/shortcuts.json?key={SECRET_KEY}"
    print("======================================================================")
    print(f"  ClipBridge v{VERSION} — By Zeyad Ahmed")
    print("======================================================================")
    print(f"  Config File        : {CONFIG_PATH}")
    print(f"  Save Folder        : {SAVE_FOLDER}")
    print(f"  Local Server URL   : http://{local_ip}:{PORT}")
    print(f"  Secret Key         : {SECRET_KEY}")
    print("----------------------------------------------------------------------")
    print("  1-Click Android 'Import from URL' (Paste in HTTP Shortcuts):")
    print(f"  {import_url}")
    print("----------------------------------------------------------------------")
    print("  Web Dashboard & History UI:")
    print(f"  {web_url}")
    print("======================================================================")


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--protocol":
        handle_protocol_invocation(sys.argv[2])
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--unregister":
        unregister_protocol_handler()
        print("Unregistered clipbridge:// protocol.")
        return

    register_protocol_handler()

    if len(sys.argv) >= 2 and sys.argv[1] == "--register-only":
        print_status_banner()
        return

    print_status_banner()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), ClipboardRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
