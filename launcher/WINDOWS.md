# SpeechCraft on Windows 11 with Ubuntu WSL2

SpeechCraft runs its existing Linux application inside WSL2. The browser UI is served from Ubuntu and opened in your normal Windows browser. This workflow does not install a native Windows application.

## Requirements

- Windows 11 x86-64 with WSL2 virtualization support enabled.
- Ubuntu 24.04 LTS (primary) or Ubuntu 22.04 LTS on WSL2.
- An ordinary, non-root Ubuntu account.
- At least 1 GiB free before installation; allow substantially more for prepared Python environments and ML models. Model downloads can require many gigabytes.
- SpeechCraft's application, runtime, model and project files must stay in the Ubuntu Linux filesystem (normally under `/home/<user>`), not `/mnt/c` or another Windows-mounted drive.

## Install WSL2 and Ubuntu

In PowerShell, run the one-time Windows prerequisite:

```powershell
wsl --install -d Ubuntu-24.04
```

Restart Windows if prompted, open Ubuntu from the Start menu, and finish account setup with a normal username. SpeechCraft's installer does not install WSL, change Windows features or configure Windows drivers.

Verify from PowerShell:

```powershell
wsl --list --verbose
```

The Ubuntu row must show `VERSION 2`. In Ubuntu, check the guest and user:

```bash
uname -r
cat /etc/os-release
id -u
```

The kernel should contain `microsoft-standard-WSL2`, `/etc/os-release` should identify Ubuntu 24.04 or 22.04, and `id -u` must not print `0`. Convert an existing WSL1 Ubuntu distribution from PowerShell with `wsl --set-version Ubuntu-24.04 2`.

## Install SpeechCraft

Run this in Ubuntu, using the HTTPS URL for the verified SpeechCraft release candidate:

```bash
curl -fsSL https://<verified-speechcraft-installer-url>/install.sh | sh
```

The universal installer selects the pinned Linux x86-64 archive in Ubuntu WSL2. It verifies the release identity and SHA-256 pins before activating the per-user package. `curl`, `python3` 3.9+, `mktemp`, and standard Ubuntu utilities are required. If a prerequisite is missing, install it with Ubuntu's package manager and retry; the installer itself does not use `sudo`.

The installer registers `~/.local/bin` in `~/.profile` only when needed, using a marked, idempotent block. Open a fresh Ubuntu terminal after installation, then run:

```bash
command -v speechcraft
speechcraft
```

The launcher waits until the frontend is ready, then tries `wslview` and the Windows `cmd.exe` browser handoff. It stays running if browser opening fails and prints the manual URL:

```text
http://127.0.0.1:3002
```

SpeechCraft binds local services to `127.0.0.1`. It does not need a Linux GUI browser, a Windows service, or a firewall change.

## Files, projects and exports

The application is installed under `~/.local/share/speechcraft/app`; managed setup state, projects, recordings, models, runtimes, caches and exports are under `~/.local/share/speechcraft` on the WSL Linux filesystem. Keep the Ubuntu virtual disk on a drive with sufficient free space.

Use the existing Browse or drag-and-drop control in the Windows browser to select a recording from `C:\Users\...`. The browser uploads it to SpeechCraft in chunks; SpeechCraft stores a managed copy inside Ubuntu. You do not need to copy source recordings into WSL manually. Original files remain untouched. Generated exports can be downloaded from the existing UI through the browser.

## CPU and optional NVIDIA acceleration

CPU operation is supported and does not require an NVIDIA GPU or native Windows Python. On a compatible NVIDIA system, WSL2 can expose CUDA through the Windows NVIDIA driver. Install/update the Windows driver that supports WSL CUDA using NVIDIA's instructions; do not install a Linux display driver inside Ubuntu. CUDA is enabled only after the actual SpeechCraft runtime and inference checks pass. Otherwise SpeechCraft remains on CPU and reports the fallback.

## Commands and recovery

```bash
speechcraft status
speechcraft stop
speechcraft
speechcraft uninstall
speechcraft uninstall --yes
speechcraft uninstall --purge
```

Default uninstall removes the application while preserving projects, recordings, models and exports. `--purge` explicitly requests removal of SpeechCraft-managed persistent data. Review the command's confirmation before proceeding.

If Windows stopped WSL while SpeechCraft was running, reopen Ubuntu and run `speechcraft`; the launcher reconciles owned processes and stale state before starting. If startup fails, inspect the actionable error and launch logs in the SpeechCraft managed data directory. Check that the Ubuntu distribution is version 2, the current user is not root, and managed directories are not symlinks or Windows-backed mounts. Use `speechcraft status` before stopping processes; SpeechCraft will refuse to kill a process whose ownership cannot be verified.

If the browser does not open, visit `http://127.0.0.1:3002` in Windows after the launcher reports frontend readiness. Keep the Ubuntu launcher running while using the UI. Do not change the bind address or open a firewall port.

## Later Windows laptop acceptance checklist

Run this on a separate Windows 11 laptop; Linux simulations do not satisfy it:

1. Install fresh Ubuntu 24.04 with `wsl --install -d Ubuntu-24.04`; verify WSL2 and an ordinary user.
2. Run the actual verified curl installer and confirm the release identity and checksum; open a fresh Ubuntu terminal and confirm `command -v speechcraft`.
3. Launch SpeechCraft and confirm setup UI access from the Windows browser at `127.0.0.1:3002`; test status, stop, restart and project persistence.
4. Prepare managed Python, FFmpeg and model dependencies. Run CPU inference on a non-NVIDIA laptop; on NVIDIA hardware, record CUDA validation and any CPU fallback.
5. Upload a real Windows-hosted WAV or MP3 using Browse or drag-and-drop; run diarization, speaker selection, slicing, QC, Clip Lab, accept/reject review and verify a downloaded export.
6. Run default uninstall and confirm personal projects, recordings, models and exports remain. Test purge only against disposable test data.

The full pipeline checklist is an upcoming host acceptance run. This infrastructure work does not claim that Windows browser connectivity or real CUDA/model inference has passed.
