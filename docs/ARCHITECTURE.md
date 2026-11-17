# ClipBridge Architecture

## Overview

ClipBridge runs a multi-threaded HTTP server (`ThreadingHTTPServer`) on Windows listening on `0.0.0.0:5050` (configurable via `config.json`).

## Windows Clipboard & Toast Layer

* **Text (`<1 ms`)**: Handled directly inside Python via `ctypes` (`user32.dll` / `kernel32.dll` using `CF_UNICODETEXT` and `GlobalAlloc`).
* **Images & Explorer Files**: Handled via one-shot `powershell.exe -NoProfile -NonInteractive -STA -EncodedCommand ...` calls with a 10-second timeout.
  * When a single image is uploaded from Android, `DataObject` receives **both** `SetFileDropList` and `SetImage` so `Ctrl+V` works in both File Explorer and image/chat apps.
* **Toast Notifications**: Dispatched asynchronously on a daemon thread using WinRT `ToastNotificationManager`. Clicking **Save to Downloads** invokes `clipbridge://save` via `HKCU\Software\Classes\clipbridge`.

## HTTP Endpoints

| Method | Path | Auth Required | Purpose |
| :--- | :--- | :--- | :--- |
| `GET` | `/ping` | No | Health check (`{"status": "ok", ...}`) |
| `GET` | `/` | Yes | Web dashboard & session transfer history UI |
| `GET` | `/setup/shortcuts.json` | Yes | Dynamic Android HTTP Shortcuts JSON with PC IP & key pre-filled |
| `GET` | `/api/pull` | Yes | Reads Windows clipboard (text, image, or Explorer files) |
| `GET` | `/api/pull-image` | Yes | Returns PNG bytes of current Windows clipboard image |
| `GET` | `/api/pull-file/<idx>` | Yes | Streams a copied Windows Explorer file as an attachment |
| `GET` | `/api/history` | Yes | Returns recent session transfer history |
| `GET` | `/api/history-file/<id>/<idx>` | Yes | Downloads a file from session history (lease-protected) |
| `GET` | `/api/storage` | Yes | Returns temporary disk usage, quota, TTL, and active clipboard items |
| `GET` | `/api/save-latest` | Yes | Copies latest staged transfer to `save_folder` (`Downloads`) |
| `POST` | `/api/push-text` | Yes | Writes UTF-8 text (up to 2 MB) to Windows clipboard |
| `POST` | `/api/push-files` | Yes | Streams multipart/raw files into isolated staging dir, sets Windows clipboard, and dispatches Toast |
| `POST` | `/api/cleanup` | Yes | Reclaims inactive/expired temp files (`force=true` removes active clipboard files too) |
| `POST` | `/api/history/clear` | Yes | Non-destructive clear by default (preserves active clipboard files with warning unless `force=true`) |

## Storage Lifecycle & Concurrency Isolation

* **Isolated Per-Upload Staging**: Each `POST /api/push-files` or `/api/push-image` request reserves its payload size against `max_storage_mb` and streams chunks into a private `%TEMP%\ClipBridge\staged\.staging_<token>` directory. Truncated, malformed, or disconnected transfers immediately delete their `.staging_<token>` folder without affecting the Windows clipboard, `latest_transfer.json`, or concurrent uploads.
* **Zero-Duplication Staging + History**: Completed uploads atomically rename `.staging_<token>` to `tx_<session>_<seq>_<token>` and update `latest_transfer.json` atomically. `TransferHistory` links directly to that directory for `Phone -> PC` transfers (avoiding duplicate disk writes), while `PC -> Phone` history copies are placed under session-scoped `%TEMP%\ClipBridge\history_files\s_<session>_item_<id>` only when within `max_storage_mb`.
* **Active Clipboard & Operation Pinning**: Transfers hold reference leases during clipboard writes, background Toast threads, HTTP file downloads, and `Save to Downloads` copies. The latest clipboard transfer also records Windows `GetClipboardSequenceNumber()` and remains protected from automatic TTL/quota eviction until superseded or replaced on the clipboard.
* **Deterministic Bounded Retention & Startup Recovery**: `TransferHistory` enforces `max_history_items` (`25`), `retention_minutes` (`60`), and total on-disk `max_storage_mb` (`1024`) across both `staged` and `history_files` (with a 60s background timer). On server startup, `cleanup_orphans_on_startup()` removes orphaned `.staging_*` and previous-session directories while adopting any staged folder still referenced by Windows `CF_HDROP`, and never deletes anything outside `%TEMP%\ClipBridge`.
