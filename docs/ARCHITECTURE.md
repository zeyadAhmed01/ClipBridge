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
| `GET` | `/api/history-file/<id>/<idx>` | Yes | Downloads a file from session history |
| `POST` | `/api/push-text` | Yes | Writes UTF-8 text (up to 2 MB) to Windows clipboard |
| `POST` | `/api/push-files` | Yes | Streams multipart/raw files (up to `max_upload_mb`) to `%TEMP%\ClipBridge\staged` and sets Windows clipboard |
| `POST` | `/api/history/clear` | Yes | Clears in-memory & staged session history |

## Storage and concurrency caveats

`TransferHistory.add_files()` creates local file copies under `%TEMP%\ClipBridge\history_files`. The UI's `max_history_items` limit bounds **in-memory entries**, not disk usage. Old copies can outlive a session or be orphaned by deque eviction. Concurrent uploads also reuse the shared `staged` directory. These limitations must be reviewed before a production-grade general release; see [release readiness](./RELEASE_READINESS.md).
