# ClipBridge

**Your clipboard, connected.**
An easy, lightweight way to move text, screenshots, and files between **Windows** and **Android** over local Wi-Fi—with **no custom Android app required**.

[![CI](https://github.com/zeyadAhmed01/ClipBridge/actions/workflows/ci.yml/badge.svg)](https://github.com/zeyadAhmed01/ClipBridge/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform: Windows & Android](https://img.shields.io/badge/Platform-Windows%20%7C%20Android-0078D6)](#requirements-and-compatibility)
[![Dependencies: Zero](https://img.shields.io/badge/Dependencies-Zero%20pip%20packages-10b981)](#requirements-and-compatibility)
[![License: MIT](https://img.shields.io/badge/License-MIT-f59e0b)](./LICENSE)

[Quick Start](#quick-start) · [How It Works](#how-it-works) · [Security & Privacy](#security-and-privacy) · [Troubleshooting](./docs/TROUBLESHOOTING.md) · [Contributing](./CONTRIBUTING.md)

---

## Why ClipBridge?

Moving a link, verification code, screenshot, or PDF between your Android phone and Windows PC usually means emailing yourself, opening a chat app, or running heavy background sync suites.

ClipBridge takes a minimal approach:
* **Zero-dependency Windows server**: Runs on standard Python 3.10+ using Windows' built-in Win32 clipboard APIs (`ctypes`) and native PowerShell Toast notifications—no `pip install`, no Electron wrapper, and no background cloud account.
* **No custom Android APK**: Works directly from your Android browser (via the built-in mobile web dashboard) or from Android's **Quick Settings notification shade** and **Share menu** using the open-source [HTTP Shortcuts](https://http-shortcuts.rmy.ch/) app.
* **True `Ctrl+V` on Windows**: Incoming text, screenshots, and files are written straight to the Windows system clipboard (`CF_UNICODETEXT`, `Bitmap`, and `FileDropList`), so you can immediately press `Ctrl+V` in File Explorer, editors, browsers, or chat apps.

---

## Features

### Supported Today
* **Bidirectional Text Transfer**: Uses native Win32 `OpenClipboard` / `SetClipboardData` calls in Python with no subprocess overhead for text.
* **Dual-Format Screenshot Paste (Android → Windows)**: Sharing a screenshot from Android places **both** an image `Bitmap` and a `FileDropList` on the Windows clipboard—paste it as an image in chat/design apps or as a `.png` file in File Explorer.
* **Actionable Windows Toast Notifications**: Incoming screenshots and files pop up a native Windows 10/11 Toast notification with **Save to Downloads** and **Open Downloads** buttons (`clipbridge://`).
* **Chunked Multi-File Transfers**: Stream single or multiple files (PDFs, ZIPs, videos, APKs) in 64 KB chunks into isolated per-upload staging directories without loading entire files into RAM or colliding across concurrent requests.
* **Bounded Local Retention & Clipboard Protection**: Configurable disk quota (`max_storage_mb`, default `1024 MB`), TTL (`retention_minutes`, default `60 min`), and history cap (`max_history_items`, default `25`) automatically reclaim old temporary files while protecting files still referenced by your active Windows clipboard or open downloads.
* **PC → Android Pull**: Fetch copied Windows text or snipped screenshots (`Win+Shift+S`) to your Android clipboard, or download copied Windows Explorer files (`Ctrl+C`) straight to your phone's `Downloads` folder.
* **1-Click "Import from URL" Onboarding**: ClipBridge serves `/setup/shortcuts.json` with your PC's local IP and generated secret key pre-filled.
* **Session Transfer History & Non-Destructive Cleanup**: Built-in dark-mode web dashboard (`http://<pc-ip>:5050/?key=<secret_key>`) with live temporary storage usage, non-destructive **Clean Temp**, and active-clipboard warnings before force-clearing.

---

## Quick Start

> **Important:** "No dedicated Android app" means you do not need to install a custom ClipBridge APK on your phone. On your **Windows PC**, you do need **Python 3.10+** installed and `clipboard_bridge.py` running.

### 1. Start ClipBridge on Windows

1. Clone or download this repository into a permanent folder (for example, `D:\ClipBridge\`):
   ```powershell
   git clone https://github.com/zeyadAhmed01/ClipBridge.git
   cd ClipBridge
   ```
2. Run `manage.bat` on Windows. Use an elevated Command Prompt only if you need the manager to configure Windows Firewall.
3. Press **`1`** (**Install & Start Silently**). This will:
   * Auto-create `config.json` with a random `secret_key`, your Windows `Downloads` path, and bounded temp storage defaults (`max_storage_mb`: `1024`, `retention_minutes`: `60`).
   * Register the `clipbridge://` protocol for the Toast **Save to Downloads** button.
   * Attempt to allow port `5050` for **Private** network profiles and local-subnet clients only (requires administrator rights).
   * Add a silent startup launcher to your Windows Startup folder and start the server.
   * You can also use option **`6`** in `manage.bat` (or `python clipboard_bridge.py --cleanup`) anytime to clean inactive temporary files without disturbing active clipboard transfers.
4. Note the **Web Dashboard URL** and **Import from URL** printed in the terminal.

### 2. Connect from Android (Choose Either Method)

* **Option A — Zero-Install Browser Mode**: Open Chrome on your Android phone and visit `http://<your-pc-ip>:5050/?key=<your-secret-key>` to send/pull text, screenshots, and files.
* **Option B — Quick Settings Tile & Share Menu (Recommended)**:
  1. Install the free, open-source **[HTTP Shortcuts](https://play.google.com/store/apps/details?id=ch.rmy.android.http_shortcuts)** app from Google Play.
  2. Open **HTTP Shortcuts** → **Settings (gear)** → **Import / Export** → **Import from URL**, and paste `http://<your-pc-ip>:5050/setup/shortcuts.json?key=<your-secret-key>`.
  3. Drag the **HTTP Shortcuts** tile into your Android Quick Settings shade.
  4. For images retrieved from Windows, use the image preview and your browser's **Copy image** or save action. Automatic image clipboard writing may be blocked over plain HTTP; do not weaken Chrome's secure-context protections.

---

## How It Works

```mermaid
flowchart LR
    A[Android Phone<br/>Quick Tile / Share / Browser] <-->|"Local Wi-Fi HTTP (Port 5050)<br/>X-Secret-Key / ?key= Auth"| B[Windows PC<br/>clipboard_bridge.py]
    B <-->|"<1ms Win32 ctypes<br/>CF_UNICODETEXT"| C[Windows Clipboard]
    B -->|"PowerShell -EncodedCommand<br/>FileDropList + Bitmap"| C
    B -->|"WinRT Toast<br/>clipbridge://save"| D[Windows Downloads Folder]
```

1. **Text transfers** are handled in-process via `user32.dll` and `kernel32.dll` (`ctypes`).
2. **Images and Explorer files** use short-lived, timeout-bounded `powershell.exe -EncodedCommand` calls with `.NET` `System.Windows.Forms.Clipboard`.
3. **Large uploads** are streamed in 64 KB chunks into isolated per-transfer directories under `%TEMP%\ClipBridge\staged`, kept protected while Windows clipboard/Toast operations use them, and only copied to `Downloads` if you click **Save to Downloads** on the Windows Toast notification. Incomplete or aborted uploads are cleaned up automatically without replacing active clipboard data.

See [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) for the full API and component breakdown.

---

## Requirements and Compatibility

| Component | Requirement | Notes |
| :--- | :--- | :--- |
| **Windows PC** | Python 3.10+ on Windows | Windows 11 used during development; Windows 10 needs separate acceptance testing. No `pip` packages needed |
| **Android Phone** | Modern Android browser | Optional [HTTP Shortcuts](https://http-shortcuts.rmy.ch/) for Quick Settings and Share integration; compatibility requires device verification |
| **Network** | Both devices on a trusted private LAN | Port `5050` accessible only to local-subnet clients on Windows Private network profile |

---

## Security and Privacy

ClipBridge is designed for **trusted home or private Wi-Fi networks**:
* **Authentication**: All endpoints except `/ping` require a matching `secret_key` (`X-Secret-Key` header or `?key=` query parameter), verified with constant-time `secrets.compare_digest`.
* **Local storage**: ClipBridge does not use a cloud service. Uploaded and cached history files under `%TEMP%\ClipBridge` are bounded by `max_history_items` (`25`), `retention_minutes` (`60`), and `max_storage_mb` (`1024`), and orphaned files from prior sessions are reclaimed on server startup (preserving files still held on the live Windows clipboard). **Clean Temp** and **Clear** are non-destructive by default and warn before deleting files still referenced by the Windows clipboard. Do not use shared computers for sensitive transfers.
* **Upload & filename safeguards**: Text payloads are capped at 2 MB, file uploads are capped at 512 MB by default (`max_upload_mb` in `config.json`, bounded by `max_storage_mb`), concurrent inflight uploads are isolated and resource-bounded, and filenames are sanitized against path traversal and Windows reserved device names.
* **Important limitation (No TLS)**: Transfers and bearer credentials travel over **unencrypted HTTP**. Use only a trusted private LAN; never expose port `5050` to the internet. Browser links containing `?key=` may be retained in browser history or copied accidentally. ClipBridge is not end-to-end encrypted and is not recommended for secrets or sensitive documents.

Read [`SECURITY.md`](./SECURITY.md) for vulnerability reporting and full security details.

---

## Troubleshooting

Common issues (such as `timeout socket closed`, Windows Firewall Public network profiles, or Chrome clipboard permissions) are documented in [`docs/TROUBLESHOOTING.md`](./docs/TROUBLESHOOTING.md).

---

## Current Limitations

* This is a **source-first project**; there is no verified Windows installer or published GitHub Release yet. Python 3.10+ is required.
* Android browser mode works without a dedicated ClipBridge APK. Quick Settings and Share-menu integration require the separate HTTP Shortcuts app.
* Windows 10 compatibility, real-device Android workflows, and Windows clipboard/Toast behavior need manual acceptance testing before a broad release.
* Temporary files remain unencrypted on disk under `%TEMP%\ClipBridge` while active or within the configured retention window (`retention_minutes`, `max_storage_mb`).

See [release readiness and manual testing checklist](./docs/RELEASE_READINESS.md).

## Roadmap

* [ ] Optional QR code rendered in the terminal for instant mobile pairing
* [ ] Optional self-signed HTTPS / TLS mode for local Wi-Fi encryption
* [ ] Standalone Windows `.exe` release via PyInstaller (for users without Python installed)
* [x] Configurable auto-cleanup TTL and disk quota for staged and history files

---

## Community and License

* **Found a bug or have an idea?** Open a [GitHub Issue](https://github.com/zeyadAhmed01/ClipBridge/issues).
* **Want to contribute?** Read [`CONTRIBUTING.md`](./CONTRIBUTING.md) and [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md).
* **License:** Released under the [MIT License](./LICENSE) by **Zeyad Ahmed**.

If ClipBridge saves you time, consider giving the repository a ⭐ on GitHub!
