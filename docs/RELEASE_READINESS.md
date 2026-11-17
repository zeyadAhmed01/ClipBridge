# ClipBridge public release readiness checklist

Status: **Launch preparation — conditionally ready for owner migration & physical Android acceptance (not yet approved for public release).**

The source was recovered from the repository's uploaded `ClipBridge-v1.0.0.zip` into a review branch. Automated checks, isolated Windows 11 host verification, and documentation are complete, but **physical Android device acceptance and owner approval to replace the existing `D:\ClipboardBridge` installation on port 5050 remain required before merging or publishing a GitHub Release**.

## Required release gates

- [ ] GitHub PR merged after code and secret review; source appears as individual files on `main`
- [x] All GitHub Actions checks green on both Windows and Ubuntu, on supported Python versions
- [x] Run `python -m unittest discover -s tests -v` and inspect results, not just exit code (`29/29` passed)
- [ ] Run `python clipboard_bridge.py` on a fresh Windows 11 installation with Python 3.10+ (verified in an isolated directory/port `5059` on Windows 11 + Python 3.14.3; full default-port `5050` cutover pending owner approval to stop legacy `D:\ClipboardBridge`)
- [x] Confirm the manager does not terminate any unrelated process that uses port 5050 (verified live: `:STOP_PORT` left legacy PID `11348` on port `5050` running while stopping isolated PID `12040` on port `5059`)
- [x] Confirm ordinary server startup/restart is blocked in elevated sessions; configure Windows Firewall using option 7 only
- [ ] Confirm the firewall rule is limited to **Private / LocalSubnet**, and does not allow public-network connections (verified in `manage.bat` option 7 command and unit tests; live elevated rule replacement of legacy `ClipboardBridge_5050` requires interactive UAC Administrator session)
- [ ] Verify Android browser text transfer in **both directions** using a trusted home LAN
- [ ] Verify HTTP Shortcuts **Import from URL**, Quick Settings tile, and Share-menu send from a real Android device
- [ ] Verify single and multi-file send, large transfer behavior, file save, Windows Toast actions, and clipboard paste in Explorer and an image app (Win32 `CF_UNICODETEXT`, `.NET DataObject` `Bitmap` + `CF_HDROP`, `save_staged_files_to_downloads`, and WinRT `ToastNotificationManager` verified on Windows 11; interactive Explorer/image-app `Ctrl+V` and `clipbridge://save` protocol click pending legacy installation cutover + physical Android send)
- [x] Verify unauthorized requests fail, and that regenerated keys invalidate old shortcuts
- [x] Verify no secrets/personal paths or private transfer data exist in source, history, screenshots, or demo assets
- [x] Implement bounded disk retention, startup orphan cleanup, isolated concurrent uploads, and automated regression tests (Issues #2 and #3)
- [ ] Confirm browser image-copy fallback on actual Android Chrome without weakening secure-context policy
- [ ] Capture a real screenshot or short demo with non-sensitive content and add it to README
- [x] Recheck README requirements and screenshots against reality
- [x] Prepare version tag, GitHub Release notes, and checksums for real downloadable assets (if any)
- [ ] Only then publish a GitHub Release and announce a supported version

## Manual & host test evidence recorded

| Scenario | Host OS / Python | Android browser / HTTP Shortcuts version | Result | Notes |
| --- | --- | --- | --- | --- |
| Clean Windows install (isolated dir & port `5059`) | Windows 11 / Python 3.14.3 | n/a | **PASS (Isolated) / BLOCKED (Port 5050 cutover)** | Isolated server started non-elevated (`PID 12040`), answered `/ping`, and stopped cleanly; default port `5050`, `Startup\ClipboardBridge_SilentStartup.vbs`, and `HKCU\Software\Classes\clipbridge` currently belong to legacy `D:\ClipboardBridge` (`PID 11348`, backed up prior to testing) |
| Windows Win32 text & Unicode/multiline clipboard | Windows 11 / Python 3.14.3 | n/a (local Win32 + HTTP API) | **PASS** | Unmocked `win32_set_clipboard_text`, `win32_get_clipboard_text`, and `get_clipboard_content()` verified exact round-trip for ASCII, Arabic Unicode, emoji, and multiline text |
| Windows dual-format screenshot (`Bitmap` + `CF_HDROP`) | Windows 11 / Python 3.14.3 | n/a (local Win32/PowerShell) | **PASS (API) / BLOCKED (Physical Android + GUI paste)** | Unmocked `set_clipboard_files_and_optional_image` populated both `CF_HDROP` and `CF_BITMAP`/`CF_DIB` simultaneously on Windows 11 |
| Windows multi-file `FileDropList` & `Save to Downloads` | Windows 11 / Python 3.14.3 | n/a (local Win32/PowerShell) | **PASS (API) / BLOCKED (Interactive `clipbridge://save`)** | Unmocked multi-file `CF_HDROP`, `get_clipboard_content()` (`type: files`), WinRT Toast XML dispatch (`TOAST_OK`), and `save_staged_files_to_downloads` verified; clicking Toast `clipbridge://save` requires replacing legacy `HKCU\Software\Classes\clipbridge` |
| Active clipboard protection during temp cleanup | Windows 11 / Python 3.14.3 | n/a | **PASS** | Unmocked `GetClipboardSequenceNumber()` preserved `active_clipboard.png` on live Windows clipboard while reclaiming older staged transfer |
| Android → Windows text | Windows 11 / Python 3.14.3 | Pending physical Android device | **BLOCKED** | HTTP endpoint verified in tests; physical phone LAN test pending |
| Windows → Android text | Windows 11 / Python 3.14.3 | Pending physical Android device | **BLOCKED** | HTTP endpoint + Win32 read verified; physical phone LAN test pending |
| Android screenshot → Windows dual-format paste | Windows 11 / Python 3.14.3 | Pending physical Android device | **BLOCKED** | Requires physical Android Share / browser upload |
| Windows image → Android preview/save | Windows 11 / Python 3.14.3 | Pending physical Android device | **BLOCKED** | Unmocked Windows bitmap-to-PNG extraction verified (`\x89PNG\r\n\x1a\n`); Android Chrome preview/copy test pending |
| Android → Windows multiple files | Windows 11 / Python 3.14.3 | Pending physical Android device | **BLOCKED** | 8-thread concurrent multipart streaming verified over localhost; physical Android test pending |
| Windows → Android file download | Windows 11 / Python 3.14.3 | Pending physical Android device | **BLOCKED** | Requires physical Android browser / HTTP Shortcuts download |
| Unauthorized key, wrong network, firewall | Windows 11 / Python 3.14.3 | n/a | **PASS (Auth & non-admin refusal) / BLOCKED (UAC firewall apply)** | HTTP `401` verified on missing/wrong/blank keys; option `7` refused without elevation; applying `ClipBridge_5050` (`Private` / `LocalSubnet`) and removing legacy `ClipboardBridge_5050` (`RemoteIP: Any`) requires UAC Administrator session |
| Manager install / stop / uninstall / process scoping | Windows 11 / Python 3.14.3 | n/a | **PASS (Scoping & non-elevated checks) / BLOCKED (In-place cutover)** | Verified `:CHECK_ELEVATED` (`0`), `:READ_PORT`, `:CHECK_RUNNING` (`OCCUPIED:11348` on 5050, `RUNNING:12040` on 5059), and `:STOP_PORT` leaving `PID 11348` untouched |

Keep this record factual. Do not mark anything passing without direct evidence.

## Controlled migration checklist for legacy `D:\ClipboardBridge` (requires owner approval)

An older prototype installation (`D:\ClipboardBridge\clipboard_bridge.py`, running as elevated `PID 11348` on port `5050`, with `ClipboardBridge_SilentStartup.vbs` in Windows Startup, `HKCU\Software\Classes\clipbridge`, and firewall rule `ClipboardBridge_5050` allowing `RemoteIP: Any`) was backed up before testing. With owner approval:

1. In an **Administrator** PowerShell/Command Prompt, stop the elevated legacy process (`Stop-Process -Id 11348 -Force`) and remove the legacy broad firewall rule (`netsh advfirewall firewall delete rule name="ClipboardBridge_5050"`).
2. Remove `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ClipboardBridge_SilentStartup.vbs`.
3. In the **Administrator** window, run the new `manage.bat` and select **`[7] Configure Private LAN Firewall Only`** to create `ClipBridge_5050` restricted to `profile=private remoteip=localsubnet`, then close the Administrator window.
4. In a **normal (non-elevated)** window, run `manage.bat` and select **`[1] Install & Start Silently`** to register `clipbridge://`, generate `config.json`, create `ClipBridge_SilentStartup.vbs`, and start the server without elevation.
5. Re-import `/setup/shortcuts.json?key=<new_secret_key>` in Android HTTP Shortcuts.

## Known high-priority residual risks

1. **Plain HTTP + bearer URLs.** Clipboard data and tokens are not encrypted in transit; limit use to a trusted LAN. Avoid public-network exposure and sharing import URLs.
2. **Unencrypted local temp files while active/retained.** Files in `%TEMP%\ClipBridge` are bounded (`max_storage_mb`, `retention_minutes`) and reclaimed on cleanup/restart, but remain unencrypted while retained; avoid shared accounts/PCs for sensitive files.
3. **Physical Android & GUI integration tests.** Automated CI and local Windows API tests do not replace physical Android device testing or interactive Explorer/Toast clicks.
4. **Distribution friction.** No verified standalone Windows binary or code-signed installer is published; source installation currently needs Python 3.10+.

## GitHub repository settings status

- [x] Add About description: `Move text, screenshots, and files between Windows and Android over local Wi-Fi. No custom Android companion APK.` (configured via GitHub API)
- [x] Add accurate topics: `windows`, `android`, `clipboard`, `file-transfer`, `local-network`, `python`, `open-source`, `productivity` (configured via GitHub API)
- [x] Configure GitHub private vulnerability reporting (enabled via GitHub API)
- [ ] Add a true product social preview (upload via GitHub Settings → General → Social preview) and a short real demo
- [ ] Consider branch protection with required CI when checks are stable
- [x] Keep Releases unpublished until manual gates pass

---

## Prepared `v1.0.0` GitHub Release Draft (Do NOT publish until all gates pass)

* **Tag:** `v1.0.0` (verified no conflicting local or remote tag exists)
* **Target branch (after PR #1 merge):** `main`
* **Release Title:** `ClipBridge v1.0.0 — Lightweight Windows ↔ Android Clipboard, Screenshot & File Bridge`
* **Distribution format:** Source-only release (`git clone` or GitHub auto-generated source archives; no standalone `.exe` binary is distributed in `v1.0.0`)

### Draft Release Notes

```markdown
# ClipBridge v1.0.0

Lightweight, zero-dependency Python bridge for moving text, screenshots, and files between **Windows 10/11** and **Android** over a trusted private Wi-Fi network—with **no custom Android companion APK**.

## Highlights
- **Instant Bidirectional Text (`<1 ms` Win32 `ctypes`):** Reads and writes `CF_UNICODETEXT` directly in Python without spawning subprocesses.
- **Dual-Format Screenshot Paste (Android → Windows):** Places both a `Bitmap` and a `FileDropList` on the Windows clipboard so `Ctrl+V` pastes pixels into chat/design apps or a `.png` file into File Explorer.
- **Chunked Multi-File Transfers & Isolated Staging:** Streams single or multiple files in 64 KB chunks into isolated per-transfer staging folders with interrupted-upload cleanup.
- **Bounded Temporary Storage & Clipboard Protection:** Enforces configurable retention (`max_storage_mb`: `1024 MB`, `retention_minutes`: `60 min`, `max_history_items`: `25`), protects active Windows clipboard files via `GetClipboardSequenceNumber()` and reference leases, and cleans orphaned files on startup.
- **Actionable Windows Toast Notifications:** Native WinRT Toast buttons for **Save to Downloads** (`clipbridge://save`) and **Open Downloads** (`clipbridge://open`).
- **Flexible Android Workflows:** Use the built-in mobile web dashboard (`http://<pc-ip>:5050/?key=<secret_key>`) with zero Android installs, or import `/setup/shortcuts.json` into the open-source [HTTP Shortcuts](https://http-shortcuts.rmy.ch/) app for Quick Settings tile and Share-menu actions.
- **Safe Windows Manager (`manage.bat`):** Separates normal-user server startup/restart from Administrator-only Private/LocalSubnet firewall setup (option `7`).

## Requirements & Supported Platforms
- **Windows PC:** Windows 11 (or Windows 10) with **Python 3.10+** in `PATH` (zero `pip` packages required).
- **Android Device:** Modern Android browser (Chrome/Firefox/Edge), plus optional [HTTP Shortcuts](https://http-shortcuts.rmy.ch/) for Quick Settings and Share-menu integration.
- **Network:** Both devices on the **same trusted Private Wi-Fi LAN** (`LocalSubnet`).

## Installation
1. Clone the repository:
   ```powershell
   git clone https://github.com/zeyadAhmed01/ClipBridge.git
   cd ClipBridge
   ```
2. Run `manage.bat` as a **normal Windows user** and press **`1`** (**Install & Start Silently**).
3. If Windows Firewall blocks incoming connections from your phone, open a **separate Administrator** `manage.bat` window, press **`7`** (**Configure Private LAN Firewall Only**), and close the elevated window.
4. Connect from Android using the **Web Dashboard URL** or paste the **Import from URL** link into HTTP Shortcuts.

## Security Limitations & Known Issues
- **Plain HTTP Transport (No TLS):** Transfers and bearer credentials (`X-Secret-Key` / `?key=`) travel over unencrypted HTTP. Use **only** on a trusted private home LAN; never expose port `5050` to public networks or port-forward it to the internet.
- **Local Temporary Files:** Staged and history files remain unencrypted under `%TEMP%\ClipBridge` until expired (`retention_minutes`) or cleaned via **Clean Temp** / option `6`. Avoid transferring sensitive material on shared PCs.
- **Android Browser Image Copy over HTTP:** Mobile browsers restrict automatic `navigator.clipboard.write()` for images on plain `http://` origins; long-press the image preview in the web dashboard and tap **Copy image** or **Save image**.
```
