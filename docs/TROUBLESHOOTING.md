# Troubleshooting

## 1. `timeout socket closed` in HTTP Shortcuts

1. **Verify the server is running**: Visit `http://localhost:5050/ping` on your Windows PC. It should return `{"status": "ok", "service": "ClipBridge", ...}`.
2. **Check Windows Firewall (Public Wi-Fi)**: Windows 11 often marks Wi-Fi networks as **Public**. Run `manage.bat` as Administrator and press `1`, or run in an Administrator terminal:
   ```powershell
   netsh advfirewall firewall add rule name="ClipBridge_5050" dir=in action=allow protocol=TCP localport=5050 profile=any
   ```
3. **Verify the Wi-Fi IPv4 address**: Run `ipconfig` on Windows and use the IP under **Wireless LAN adapter Wi-Fi** (ignore VirtualBox, WSL, or Hyper-V adapters).
4. **Same Wi-Fi / AP Isolation**: Ensure both devices are on the same Wi-Fi network (not Guest Wi-Fi or mobile data).

## 2. PC -> Android image doesn't auto-copy to clipboard in Chrome

Browsers restrict `navigator.clipboard.write()` on plain `http://` origins.
1. In Android Chrome, open `chrome://flags/#unsafely-treat-insecure-origin-as-secure`.
2. Add `http://<your-pc-ip>:5050`, set it to **Enabled**, and tap **Relaunch**.
3. Alternatively, long-press the image preview on the ClipBridge web page and tap **Copy image**.

## 3. Toast "Save to Downloads" button does nothing

Run `manage.bat` as Administrator and press **`1`** to re-register the `clipbridge://` protocol in `HKCU\Software\Classes\clipbridge`.
