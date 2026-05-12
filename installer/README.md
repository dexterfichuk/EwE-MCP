# EwE MCP Plugin — Inno Setup Installer

## Prerequisites

- **Inno Setup 6** — Download from [jrsoftware.org](https://jrsoftware.org/isdl.php) (v6.3+ recommended)
- **Built plugin DLLs** — `EwE_MCP_Plugin.dll` and `Newtonsoft.Json.dll` must be placed in the `installer/` directory (next to the `.iss` file) before compiling

## How to Build the Installer

1. Install Inno Setup 6 on a Windows machine (the build VM works).
2. Copy the two DLLs into `installer/`:
   ```
   installer/
   ├── EwE_MCP_Plugin.iss
   ├── EwE_MCP_Plugin.dll
   ├── Newtonsoft.Json.dll
   └── EwE_MCP_Plugin.cfg   (included default configuration template)
   ```
3. Compile:
   ```powershell
   iscc installer\EwE_MCP_Plugin.iss
   ```
4. The output installer EXE will be at `installer/output/EwE_MCP_Plugin_<version>_Setup.exe`.

## Customizing the EwE Path

The installer defaults to the standard EwE 6.7 install location:

```
C:\Program Files (x86)\Ecopath with Ecosim 6.7.0.18865_32-bit
```

If EwE is installed elsewhere, the user can change the path during installation. The installer warns if `EwE6.exe` is not found in the selected directory.

To change the default at build time, edit the `GetDefaultEwEDir` function in `EwE_MCP_Plugin.iss`.

## What Gets Installed

| File | Location |
|------|----------|
| `EwE_MCP_Plugin.dll` | `<EwE path>\Plugins\` |
| `Newtonsoft.Json.dll` | `<EwE path>\Plugins\` (only if not already present) |
| `EwE_MCP_Plugin.cfg` | `%APPDATA%\Ecopath with Ecosim\` (only if not already present) |

## Config File

On first install, a default `EwE_MCP_Plugin.cfg` is created in `%APPDATA%\Ecopath with Ecosim\` with these defaults:

```ini
[EwE_MCP]
host=127.0.0.1
port=9877
token=ewe-mcp-local-dev
```

If the file already exists (e.g. from a previous install), it is **not** overwritten.

## Testing Install / Uninstall

1. **Install**: Run the generated `.exe` and point it at your EwE directory. Verify the DLLs appear in the `Plugins\` subfolder.
2. **Load**: Launch EwE and confirm the plugin loads (check `%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log`).
3. **Uninstall**: Use "Add/Remove Programs" or the uninstaller shortcut. Verify:
   - `Plugins\EwE_MCP_Plugin.dll` is removed
   - `Plugins\Newtonsoft.Json.dll` is removed (only if no other EwE plugin needs it — the uninstaller always removes it, so reinstall if needed)
   - The config file in `%APPDATA%` is removed
   - **Other plugins and EwE itself remain intact**

## Versioning

The installer version is automatically read from `EwE_MCP_Plugin.dll` via Inno Setup's `GetVersionNumbersString`. If the DLL has no version resource, it falls back to `0.1.0`. To override, change the `#define AppVersion` line in the `.iss` file.
