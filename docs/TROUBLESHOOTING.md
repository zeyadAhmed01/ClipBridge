# Troubleshooting

## 1. `timeout socket closed` in HTTP Shortcuts

1. **Verify the server is running**: Visit `http://localhost:5050/ping` on your Windows PC. It should return `{"status": "ok", "service": "ClipBridge", ...}`.
2. **Check your trusted network profile**: Windows may classify Wi-Fi as **Public**. Only switch a network you personally trust to **Private** in Windows Settings; do **not** make public networks private to use ClipBridge. On a trusted private LAN, run `manage.bat` as Administrator and press `1`, or run in an elevated terminal:
   ```powershell
   netsh advfirewall firewall add rule name="ClipBridge_5050" dir=in action=allow protocol=TCP localport=5050 profile=private remoteip=localsubnet
   ```
3. **Verify the Wi-Fi IPv4 address**: Run `ipconfig` on Windows and use the IP under **Wireless LAN adapter Wi-Fi** (ignore VirtualBox, WSL, or Hyper-V adapters).
4. **Same Wi-Fi / AP Isolation**: Ensure both devices are on the same Wi-Fi network (not Guest Wi-Fi or mobile data).

## 2. PC -> Android image doesn't auto-copy to clipboard in Chrome

Browsers restrict `navigator.clipboard.write()` on plain `http://` origins.
1. Open the image preview from the ClipBridge mobile web dashboard.
2. Long-press the image and use **Copy image** or **Save image** if supported by your browser.
3. Automatic programmatic clipboard writes need a secure context (HTTPS); ClipBridge currently uses HTTP. Do not disable browser security protections as a general workaround.

## 3. Toast "Save to Downloads" button does nothing

Run `manage.bat` and press **`1`** to re-register the `clipbridge://` protocol in `HKCU\Software\Classes\clipbridge`.
