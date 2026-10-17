# SpeechCraft native Windows acceptance.
# This script is the later hardware procedure. It is not evidence of acceptance
# when it has not been run on Windows 10/11 with Ubuntu WSL2.
# Marker: PENDING_NATIVE_WINDOWS
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Archive,
    [string]$Distribution,
    [switch]$IncludeCuda
)

$ErrorActionPreference = 'Stop'
Write-Host 'PENDING_NATIVE_WINDOWS: this procedure has not been accepted until every step below passes on a real Windows machine.'

if ($env:OS -ne 'Windows_NT') {
    Write-Error 'Refusing to pretend this ran on Windows. Run accept-windows.ps1 on Windows 10 or 11 x64 with Ubuntu WSL2.'
    exit 2
}

function Step([string]$Name, [scriptblock]$Body) {
    Write-Host "== $Name"
    & $Body
}

Step 'Windows architecture' {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Host is not Windows x64.' }
}

Step 'WSL present' {
    $wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
    if (-not (Test-Path -LiteralPath $wsl)) { throw 'wsl.exe is not in System32.' }
    & $wsl --status
}

if (-not $Archive) { throw 'Pass -Archive with the verified Linux x86-64 archive.' }
$here = Split-Path -Parent $PSCommandPath
$installer = Join-Path $here 'install-windows.ps1'

Step 'Install into Ubuntu WSL2' {
    if ($Distribution) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -Archive $Archive -Distribution $Distribution
    } else {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -Archive $Archive
    }
    if ($LASTEXITCODE -ne 0) { throw "installer exited $LASTEXITCODE" }
}

Write-Host 'Open a new Windows Terminal window. The steps below must be run there so the user PATH is visible.'
Write-Host @'
Then confirm, in order:
1. Get-Command speechcraft resolves to %LOCALAPPDATA%\SpeechCraft\launcher\speechcraft.exe.
2. speechcraft --help prints the Linux usage text.
3. speechcraft opens http://127.0.0.1:3002/setup in the default Windows browser.
4. The setup page, controller, and backend are reached only through that loopback origin. No public bind and no direct browser call to port 4317.
5. Complete setup in the browser. Enter a Hugging Face token only in the masked setup field, never as a command argument or environment variable.
6. Confirm managed Linux Python, FFmpeg, Silero, Whisper Large-v3 CPU, and authorized pyannote Community-1.
7. Run a real offline diarization and a multi-speaker dataset workflow, including permitted TitaNet inference.
8. If -IncludeCuda was requested, run the existing CUDA probes. A Windows GPU without a passing WSL probe is a CPU fallback, not CUDA success.
9. speechcraft stop shuts the application down. speechcraft again opens /login without setup, without a second model download, and without asking for the Hugging Face token again.
10. speechcraft status and speechcraft stop work from a second terminal.
11. Install a newer archive over the same Ubuntu distribution and confirm projects and models remain.
12. install-windows.ps1 -Uninstall removes only the Windows command. Ubuntu and ~/.local/share/speechcraft data remain.

Do not mark this file's result as passed from a Linux packaging machine.
'@

if ($IncludeCuda) {
    Write-Host 'CUDA is optional and remains pending until the WSL CUDA probes pass on this machine.'
}

Write-Host 'Also confirm the five hardening fixes: failed PATH registration restores the previous launcher and configuration; Ctrl+C requests Linux shutdown only when this launch owns the services; an unrelated localhost server is not opened; the package version comes from the pinned Git revision; a package without windows-release.json is refused.'
exit 2
