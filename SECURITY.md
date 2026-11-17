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
* **Authentication**: All endpoints except `/ping` require a matching `secret_key` (`X-Secret-Key` header or `?key=` query parameter), validated with `secrets.compare_digest`.
* **Payload Limits**: Text payloads are capped at 2 MB and file uploads are capped at 512 MB by default (`max_upload_mb` in `config.json`). These are per-request limits; they do not enforce a total disk quota.
* **Local transfer retention**: Staged files and duplicated history files may remain under `%TEMP%\ClipBridge` after shutdown. There is no automatic TTL/disk quota. Do not transfer sensitive material on shared devices; review and clear temporary files when finished.

## Reporting a Vulnerability

Please **do not** open a public GitHub Issue for security vulnerabilities.

Instead, report vulnerabilities privately via:
* **GitHub Private Vulnerability Reporting** (if enabled on the repository), or
* Email directly to **zeyadahmed@sci.psu.edu.eg** with the subject `[ClipBridge Security] <short summary>`.

Include steps to reproduce and the affected version. The maintainer will review reports as capacity allows; please avoid public disclosure before a fix can be assessed.
