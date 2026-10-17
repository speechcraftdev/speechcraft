# SpeechCraft on Windows

SpeechCraft runs the existing Linux application inside Ubuntu on WSL2. The Windows command is only a dispatcher.

```powershell
speechcraft
```

That starts or reuses the Linux application and opens `http://127.0.0.1:3002` in the default Windows browser. The first launch uses `/setup`. After onboarding is complete, later launches use `/login`.

## What you need

- Windows 10 or Windows 11, x64.
- An existing Ubuntu 22.04 or 24.04 distribution on WSL2, with an ordinary non-root user.
- The verified Linux x86-64 archive (`speechcraft-<version>-linux-x86_64.tar.gz`) and its `.sha256` and `.release.json` files.
- This Windows integration directory, containing `speechcraft.exe` and `install-windows.ps1`.

SpeechCraft does not install Ubuntu for you, does not request administrator rights for the per-user install, and does not change the system PATH. If WSL itself is missing, install and reboot it using Microsoft's WSL2 instructions, then rerun the installer.

## Install

Open a normal PowerShell window. Do not use an elevated prompt.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1 -Archive C:\Users\you\Downloads\speechcraft-0.1.0-linux-x86_64.tar.gz
```

If more than one suitable Ubuntu distribution is installed, choose one:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1 -Archive C:\Users\you\Downloads\speechcraft-0.1.0-linux-x86_64.tar.gz -Distribution Ubuntu-24.04
```

The installer copies the archive into the Ubuntu filesystem, runs the existing Linux per-user installer, and adds `%LOCALAPPDATA%\SpeechCraft\launcher` to the current user's PATH. Close and reopen Windows Terminal so the new PATH is visible.

Models, projects, Python environments, and the application stay in the Ubuntu home directory. They are not stored under `C:\`.

## Commands

```powershell
speechcraft
speechcraft --no-browser
speechcraft status
speechcraft status --json
speechcraft stop
speechcraft --help
```

`speechcraft` opens the Windows browser after the Linux launcher reports that the frontend is ready. `--no-browser` prints the URL and does not open a browser. `status` and `stop` use the same Linux application lifecycle. A second `speechcraft` reuses the running application instead of starting another backend.

An optional data directory must already be a Linux path, for example `--data-dir /home/you/speechcraft-data`. Windows paths and `/mnt/c` paths are rejected.

## Remove the Windows command

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1 -Uninstall
```

This removes `speechcraft.exe` and its user PATH entry. It does not unregister Ubuntu and does not delete models, projects, or application data.

## Development build limits

The Windows executable produced by the package script is not code-signed. SmartScreen trust is not established. Package creation does not prove that the command works on a physical Windows machine, that Windows can reach the WSL frontend, or that CUDA or model inference succeeded.

Native acceptance is the separate procedure in `accept-windows.ps1`.
