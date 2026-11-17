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
* Interactive Windows CLI manager (`manage.bat`) for install, status, restart, stop, and uninstall.
* Security hardening: constant-time key comparison, upload size limits (`413`), Windows reserved filename sanitization, log key redaction, and same-origin JSON responses.
