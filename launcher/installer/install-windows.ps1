# Install SpeechCraft for the current Windows user.
# Requires Windows PowerShell 5.1 or PowerShell 7. This script does not invoke
# cmd.exe and does not request administrator privileges.
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Archive,
    [string]$Distribution,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Error 'SpeechCraft Windows installation requires Windows 10 or Windows 11 x64.'
    exit 1
}

$here = Split-Path -Parent $PSCommandPath
$exe = Join-Path $here 'speechcraft.exe'
if (-not (Test-Path -LiteralPath $exe)) {
    Write-Error 'speechcraft.exe must be in the same directory as install-windows.ps1.'
    exit 1
}

if ($Uninstall) {
    & $exe --windows-uninstall
    exit $LASTEXITCODE
}

if (-not $Archive) {
    Write-Error 'Pass -Archive with the verified SpeechCraft Linux x86-64 .tar.gz archive. Its .sha256 and .release.json files must sit beside it.'
    exit 2
}

if ($Distribution) {
    & $exe --windows-install --linux-archive $Archive --distribution $Distribution
} else {
    & $exe --windows-install --linux-archive $Archive
}
exit $LASTEXITCODE
