# Security Policy

## Supported Versions

| Version | Supported |
| :--- | :--- |
| `1.0.x` | Yes |
| `< 1.0.0` | No |

## Security Model & Known Limitations

* **Local Network Transport**: ClipBridge serves over plain **HTTP** on port `5050` by default. It is intended **only for trusted private/home Wi-Fi networks**. Do not expose port `5050` to the public internet or use ClipBridge on untrusted public Wi-Fi.
* **Authentication**: All endpoints except `/ping` require a matching `secret_key` (`X-Secret-Key` header or `?key=` query parameter), validated with `secrets.compare_digest`.
* **Payload Limits**: Text payloads are capped at 2 MB and file uploads are capped at 512 MB by default (`max_upload_mb` in `config.json`).

## Reporting a Vulnerability

Please **do not** open a public GitHub Issue for security vulnerabilities.

Instead, report vulnerabilities privately via:
* **GitHub Private Vulnerability Reporting** (if enabled on the repository), or
* Email directly to **zeyadahmed@sci.psu.edu.eg** with the subject `[ClipBridge Security] <short summary>`.

Include steps to reproduce and the affected version. You will receive an acknowledgment within 72 hours.
