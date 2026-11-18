# Security Policy

## Supported Versions

| Version | Supported |
| :--- | :--- |
| `1.0.x` | Yes |
| `< 1.0.0` | No |

## Security Model & Known Limitations

* **Local Network Transport**: ClipBridge serves over plain **HTTP** on port `5050` by default. **Neither transfers nor authentication credentials are encrypted in transit.** Use only a trusted private/home LAN. Do not expose port `5050` to the internet or use ClipBridge over untrusted Wi-Fi.
* **Bearer credentials**: The generated secret grants access to clipboard and transfer endpoints. Web URLs and imported Android shortcuts can contain that secret; avoid forwarding them, using browser-sync on shared devices, or pasting them into bug reports. A `?key=` link may be stored in browser history. Regenerate and update all paired shortcuts if the key is exposed.
* **Network exposure**: The Windows manager restricts its suggested firewall rule to the Private profile and local-subnet peers. The server itself listens on available interfaces, so confirm host firewall behavior and never configure internet port forwarding.
* **Authentication**: All endpoints except `/ping` require a matching `secret_key` (`X-Secret-Key` header or `?key=` query parameter), validated with `secrets.compare_digest` (blank keys are rejected and regenerated).
* **Response & Command Hardening**: HTTP responses send `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and `X-Frame-Options: DENY`. PowerShell clipboard and Toast helpers receive file paths and XML payloads as Base64-encoded UTF-8 data rather than interpolated command strings.
* **Payload Limits & Resource Bounds**: Text payloads are capped at 2 MB, file uploads are capped at `min(max_upload_mb, max_storage_mb)` (512 MB upload default, 1024 MB storage default), multipart uploads are bounded to 100 files per request, concurrent uploads are isolated and bounded (`503` when busy/over budget), and interrupted/truncated uploads abort cleanly without modifying clipboard or history.
* **Local transfer retention & installation isolation**: Temporary staged and history files are isolated per installation under `%TEMP%\ClipBridge\installs\<install_id>` (protected by an `.owner.json` manifest and `.server.lock`), bounded by `max_history_items` (`25`), `retention_minutes` (`60`), and `max_storage_mb` (`1024`). Orphaned files from previous sessions of the same installation are reclaimed on server startup (preserving files still referenced by the live Windows clipboard). Offline CLI `--cleanup` fails closed on HTTP `401`, occupied ports, timeouts, opaque/unverifiable processes, or missing/foreign ownership. Uninstall (`--unregister`, `--uninstall-startup`, `--uninstall-firewall`, and `manage.bat` option `5`) verifies canonical installation ownership before removing integrations so legacy or parallel copies are left intact; read-only `--status` / `--info` never modifies registry, startup, firewall, or temp storage. **Clean Temp** (`POST /api/cleanup`) and **Clear** (`POST /api/history/clear`) are non-destructive by default and warn before removing files still needed by the Windows clipboard. Files remain unencrypted on disk while active or within the retention window—do not transfer sensitive material on shared accounts/devices, and clear temporary files when finished.

## Reporting a Vulnerability

Please **do not** open a public GitHub Issue for security vulnerabilities.

Instead, report vulnerabilities privately via:
* **GitHub Private Vulnerability Reporting** (if enabled on the repository), or
* Email directly to **zeyadahmed@sci.psu.edu.eg** with the subject `[ClipBridge Security] <short summary>`.

Include steps to reproduce and the affected version. The maintainer will review reports as capacity allows; please avoid public disclosure before a fix can be assessed.
