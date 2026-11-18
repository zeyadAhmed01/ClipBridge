# Contributing to ClipBridge

Thank you for considering contributing to **ClipBridge**!

## Development Setup

1. Fork and clone the repository:
   ```powershell
   git clone https://github.com/zeyadAhmed01/ClipBridge.git
   cd ClipBridge
   ```
2. Ensure **Python 3.10+** is installed (`python --version`). ClipBridge uses **zero third-party runtime dependencies**.
3. Run the automated test suite (works on both Windows and Linux):
   ```powershell
   python -m unittest discover -s tests -v
   ```
4. To test the live server locally on Windows:
   ```powershell
   python clipboard_bridge.py
   ```
   *(A `config.json` file will be generated automatically and is ignored by `.gitignore`.)*

## Pull Request Guidelines

* Keep changes focused and minimal—ClipBridge intentionally avoids heavy frameworks and external runtime dependencies.
* Never commit `config.json`, personal IP addresses, or secret keys.
* Update or add unit tests in `tests/test_clipbridge.py` for any parser, validation, or API changes.
* If you modify Windows-only behavior (`ctypes` Win32 clipboard, PowerShell Toast notifications, or `manage.bat`), note how you tested it on Windows 10/11 in your PR description.
* Follow the [Code of Conduct](./CODE_OF_CONDUCT.md).
