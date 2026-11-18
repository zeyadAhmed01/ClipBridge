# Changelog

All notable changes to **ClipBridge** will be documented in this file.

## [1.0.0] - 2026-11-18

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
* Per-installation isolation (`%TEMP%\ClipBridge\installs\<install_id>` with `.owner.json` and `.server.lock`), read-only `--status`/`--info` (without generating or displaying unsaved keys when unconfigured), non-destructive installation preflight (`--preflight-install`), ownership-validated startup (`ClipBridge_<install_id>.vbs`), firewall (`ClipBridge_<port>_<install_id>`), and `clipbridge://` uninstall commands, and lock-held fail-closed offline `--cleanup`.
* Interactive Windows CLI manager (`manage.bat`) separating non-elevated install/restart/status/stop/cleanup/uninstall from Administrator-only Private/LocalSubnet firewall configuration (option `7`).
* Security hardening: constant-time key comparison (blank key rejection), upload size limits (`413`), Windows reserved filename sanitization, Base64-encoded PowerShell payloads (no filename interpolation), log key redaction, `X-Frame-Options: DENY`, and same-origin JSON responses.
