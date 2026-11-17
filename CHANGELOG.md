# Changelog

All notable changes to **ClipBridge** will be documented in this file.

## [1.0.0] - 2026-11-17

### Added
* Bidirectional text, screenshot, and multi-file transfer server (`clipboard_bridge.py`) using Python standard library + Win32 `ctypes` (`CF_UNICODETEXT`) + PowerShell `-EncodedCommand`.
* Dual-format Windows clipboard injection (`Bitmap` + `FileDropList`) for incoming screenshots.
* Native Windows 10/11 Toast notifications with `Save to Downloads` and `Open Downloads` (`clipbridge://`) actions.
* 64 KB chunked multipart file streaming (`stream_multipart_to_staged_dir`).
* Auto-generated `config.json` with random `secret_key` and dynamic `Downloads` folder detection.
* Dynamic `/setup/shortcuts.json` endpoint for 1-click **Import from URL** in Android HTTP Shortcuts.
* Built-in mobile web dashboard with session transfer history.
* Isolated per-upload staging directories (`.staging_<token>` → `tx_<session>_<seq>_<token>`) with interrupted/failed upload cleanup and concurrent upload resource bounds.
* Bounded on-disk temporary storage (`max_storage_mb`: 1024 MB default, `retention_minutes`: 60 min default, `max_history_items`: 25), active Windows clipboard (`GetClipboardSequenceNumber`) and lease protection, startup orphan recovery, and non-destructive cleanup API/UI/CLI (`--cleanup`, `POST /api/cleanup`, `GET /api/storage`).
* Interactive Windows CLI manager (`manage.bat`) for install, status, restart, stop, non-destructive temp cleanup, and uninstall (process-scoped stop, Private/LocalSubnet firewall scoping, port wait/verification).
* Security hardening: constant-time key comparison (blank key rejection), upload size limits (`413`), Windows reserved filename sanitization, Base64-encoded PowerShell payloads (no filename interpolation), log key redaction, `X-Frame-Options: DENY`, and same-origin JSON responses.
