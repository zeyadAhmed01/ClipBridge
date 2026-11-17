# ClipBridge public release readiness checklist

Status: **Launch preparation — not yet approved for a public binary release.**

The source was recovered from the repository's uploaded `ClipBridge-v1.0.0.zip` into a review branch. Automated checks and documentation are necessary but **do not substitute for real Windows/Android acceptance testing**.

## Required release gates

- [ ] GitHub PR merged after code and secret review; source appears as individual files on `main`
- [ ] All GitHub Actions checks green on both Windows and Ubuntu, on supported Python versions
- [ ] Run `python -m unittest discover -s tests -v` and inspect results, not just exit code
- [ ] Run `python clipboard_bridge.py` on a fresh Windows 11 installation with Python 3.10+
- [ ] Confirm the manager does not terminate any unrelated process that uses port 5050
- [ ] Confirm the firewall rule is limited to **Private / LocalSubnet**, and does not allow public-network connections
- [ ] Verify Android browser text transfer in **both directions** using a trusted home LAN
- [ ] Verify HTTP Shortcuts **Import from URL**, Quick Settings tile, and Share-menu send from a real Android device
- [ ] Verify single and multi-file send, large transfer behavior, file save, Windows Toast actions, and clipboard paste in Explorer and an image app
- [ ] Verify unauthorized requests fail, and that regenerated keys invalidate old shortcuts
- [ ] Verify no secrets/personal paths or private transfer data exist in source, history, screenshots, or demo assets
- [ ] Determine a safe disk-retention policy for staged/history copies; add cleanup and concurrency tests
- [ ] Confirm browser image-copy fallback on actual Android Chrome without weakening secure-context policy
- [ ] Capture a real screenshot or short demo with non-sensitive content and add it to README
- [ ] Recheck README requirements and screenshots against reality
- [ ] Prepare version tag, GitHub Release notes, and checksums for real downloadable assets (if any)
- [ ] Only then publish a GitHub Release and announce a supported version

## Manual test evidence to record

| Scenario | Host OS / Python | Android browser / HTTP Shortcuts version | Result | Notes |
| --- | --- | --- | --- | --- |
| Clean Windows install | Pending | n/a | Not tested | |
| Android → Windows text | Pending | Pending | Not tested | |
| Windows → Android text | Pending | Pending | Not tested | |
| Android screenshot → Windows dual-format paste | Pending | Pending | Not tested | |
| Windows image → Android preview/save | Pending | Pending | Not tested | |
| Android → Windows multiple files | Pending | Pending | Not tested | |
| Windows → Android file download | Pending | Pending | Not tested | |
| Unauthorized key, wrong network, firewall | Pending | Pending | Not tested | |
| Manager install / stop / uninstall | Pending | n/a | Not tested | |

Keep this record factual. Do not mark anything passing without direct evidence.

## Known high-priority residual risks

1. **Unbounded temp/history storage.** Session history caps the in-memory count, but files copied to `history_files` can remain after eviction/restart. A quota or deterministic cleanup strategy is needed.
2. **Concurrent uploads.** Upload handlers currently share/clear one staging directory; verify isolation across concurrent requests.
3. **Plain HTTP + bearer URLs.** Clipboard data and tokens are not encrypted in transit; limit use to a trusted LAN. Avoid public-network exposure and sharing import URLs.
4. **Physical integration tests.** CI HTTP tests do not exercise the real Win32 clipboard, Windows Toast UI, Android Quick Settings, or the Share sheet.
5. **Distribution friction.** No verified standalone Windows binary or code-signed installer is published; source installation currently needs Python.

## GitHub repository settings requiring owner action

- Add About description: `Move text, screenshots, and files between Windows and Android over local Wi-Fi—no custom companion APK.`
- Add accurate topics such as `windows`, `android`, `clipboard`, `file-transfer`, `local-network`, `python`, `open-source`, and `productivity`
- Configure GitHub private vulnerability reporting where available
- Add a true product social preview and a short real demo
- Consider branch protection with required CI when checks are stable
- Keep Releases unpublished until manual gates pass

