# Troubleshooting

## 1. Connection failure or `timeout socket closed`

1. **Verify the server is running on Windows:** Open `http://127.0.0.1:5050/ping` in a browser on your Windows PC (or run `manage.bat` option **`[2]`**). It should return `{"status": "ok", "service": "ClipBridge", ...}` and report `Server Status : ONLINE`.
2. **Confirm same Wi-Fi & no AP/guest isolation:** Both your Android phone and Windows PC must be connected to the same private Wi-Fi network. Disable Guest Wi-Fi / client isolation on your router and disconnect mobile VPNs that route LAN traffic away from the local subnet.
3. **Configure Windows Firewall:** Proceed to [Section 2](#2-windows-firewall-configuration-option-7) below.

---

## 2. Windows Firewall configuration (Option `[7]`)

Windows may classify a Wi-Fi connection as **Public**, which blocks local inbound connections by default.

1. **Check your Windows network profile:** Only switch a Wi-Fi network you personally trust (such as your home router) to **Private** in **Windows Settings → Network & internet → Wi-Fi → Properties**. Never switch a public café, airport, or hotel network to Private.
2. **Use `manage.bat` option `[7]` as Administrator:**
   * Right-click `manage.bat` → **Run as administrator**.
   * Select **`[7] Configure Private LAN Firewall Only`**.
   * This verifies rule ownership (`--check-firewall`) and creates the installation-scoped inbound rule `ClipBridge_<port>_<install_id>` restricted to `profile=private` and `remoteip=localsubnet`.
   * **Close the Administrator window immediately afterward** and run the server only from a normal, non-elevated user session.
3. **Manual inspection (optional):** To inspect your installation's rule name, run `python clipboard_bridge.py --install-id` to get `<install_id>`, then run:
   ```powershell
   netsh advfirewall firewall show rule name="ClipBridge_5050_<install_id>"
   ```

---

## 3. Incorrect Wi-Fi IP address or port

If your PC has WSL, Hyper-V, Docker, VirtualBox, or VPN adapters installed, double-check that your phone is using your PC's actual **Wi-Fi IPv4 address**:

1. Run `ipconfig` in Windows PowerShell or Command Prompt.
2. Look under **Wireless LAN adapter Wi-Fi** (or your active Ethernet LAN adapter) for **IPv4 Address** (for example, `192.168.1.x`).
3. Ignore `172.x.x.x` (WSL/Hyper-V) or `192.168.56.x` (VirtualBox) virtual interfaces.

---

## 4. Existing port `5050` conflict

If `manage.bat` option **`[2]`** shows `Server Status : STOPPED (Port 5050 is used by another process: OCCUPIED:<pid>)`, or option **`[1]`** aborts during `--preflight-install`:

1. Discover which process currently owns port `5050`:
   ```powershell
   Get-NetTCPConnection -LocalPort 5050 -State Listen | Select-Object LocalAddress, LocalPort, OwningProcess
   ```
2. If the process is an older ClipBridge installation, follow [Section 8](#8-migrating-from-an-older-clipbridge-installation).
3. If another application requires port `5050` and no other ClipBridge installation owns `clipbridge://`, edit `"port"` in `config.json` (for example, `5051`), re-run `manage.bat` option **`[7]`** (Administrator) for the new port, and restart via option **`[3]`**.

---

## 5. Android browser clipboard permissions over HTTP

Modern mobile browsers restrict automatic `navigator.clipboard.write()` calls—especially for images—on unencrypted `http://` origins:

1. **Images (`PC → Android`):** Tap **Pull PC Clipboard** in the mobile web dashboard, **long-press the image preview**, and select **Copy image** or **Save image**.
2. **Do not weaken browser security flags:** Avoid enabling Chrome flags such as `unsafely-treat-insecure-origin-as-secure` for general browsing.

---

## 6. HTTP Shortcuts `Import from URL` failures

1. Run `manage.bat` option **`[2]`** (or `python clipboard_bridge.py --status`) and verify that `config.json` has been created (if it shows `[Not configured yet]`, run option **`[1]`** first).
2. Copy the exact URL shown under **1-Click Android 'Import from URL'**:
   `http://<your-pc-ip>:5050/setup/shortcuts.json?key=<your-secret-key>`
3. Test opening that URL in your Android browser first—if it downloads or displays JSON, paste the same URL into **HTTP Shortcuts → Settings → Import / Export → Import from URL**.
4. Alternatively, transfer [`http_shortcuts_android.json`](../http_shortcuts_android.json) to your phone, import it from file in HTTP Shortcuts, and set the `pc_ip` and `secret_key` variables in **HTTP Shortcuts → Variables**.

---

## 7. Windows Toast buttons do nothing

Clicking **Save to Downloads** (`clipbridge://save`) or **Open Downloads** (`clipbridge://open`) invokes the `HKCU\Software\Classes\clipbridge` protocol handler:

1. Run `manage.bat` option **`[2]`** (`python clipboard_bridge.py --status`) and check the `Protocol Handler` line.
2. If it shows `Not registered`, run `manage.bat` as a normal user and select **`[1]`** (or run `python clipboard_bridge.py --register-only` when no foreign installation conflicts).
3. If it shows `Registered to another installation (...)`, follow [Section 8](#8-migrating-from-an-older-clipbridge-installation) so Toast clicks are not routed to a stale script path.

---

## 8. Migrating from an older ClipBridge installation

Older ClipBridge / `ClipboardBridge` prototypes used a shared startup file (`ClipboardBridge_SilentStartup.vbs`), an unscoped firewall rule (`ClipboardBridge_5050`), and a direct `HKCU\Software\Classes\clipbridge` entry. To prevent accidental data or service disruption, `manage.bat` option **`[1]`** detects these conflicts during `--preflight-install` and aborts without modifying them.

To cut over cleanly to the current version:

1. **Rediscover the active legacy process ID dynamically** (never assume a historical PID is still current):
   ```powershell
   Get-NetTCPConnection -LocalPort 5050 -State Listen | ForEach-Object {
       Get-CimInstance Win32_Process -Filter "ProcessId = $($_.OwningProcess)" | Select-Object ProcessId, Name, CommandLine
   }
   ```
2. **Stop the verified legacy process** (in an Administrator PowerShell window if the legacy server was started elevated):
   ```powershell
   Stop-Process -Id <verified_legacy_pid> -Force
   ```
3. **Remove the legacy firewall rule and legacy startup launcher:**
   ```powershell
   netsh advfirewall firewall delete rule name="ClipboardBridge_5050"
   Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\ClipboardBridge_SilentStartup.vbs" -ErrorAction SilentlyContinue
   reg delete "HKCU\Software\Classes\clipbridge" /f
   ```
4. **Configure the new Private/LocalSubnet firewall rule:** In the Administrator window, run the new `manage.bat`, select **`[7] Configure Private LAN Firewall Only`**, and **close the Administrator window**.
5. **Install and start as a normal user:** Open `manage.bat` normally (non-elevated) and select **`[1] Install & Start Silently`**.
6. **Re-import Android shortcuts:** Copy the new **Import from URL** link from option **`[2]`** into HTTP Shortcuts on your phone.
