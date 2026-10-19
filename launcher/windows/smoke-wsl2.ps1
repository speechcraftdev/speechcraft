param(
    [Parameter(Mandatory = $true)] [string] $InstallerUrl,
    [string] $Distribution = 'Ubuntu-24.04'
)

$ErrorActionPreference = 'Stop'
if ($InstallerUrl -notmatch '^https://[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._~:/-]+$') {
    throw 'InstallerUrl must be a plain HTTPS URL without credentials, query parameters, or shell metacharacters.'
}

function Invoke-Wsl([string] $Command) {
    & wsl.exe -d $Distribution -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) { throw "WSL command failed with exit code $LASTEXITCODE: $Command" }
}

try {
    $identity = Invoke-Wsl 'uname -r; grep -E "^(ID|VERSION_ID)=" /etc/os-release; test "$(id -u)" -ne 0'
    if (($identity -join "`n") -notmatch 'microsoft-standard-WSL2' -or ($identity -join "`n") -notmatch 'ID=ubuntu' -or ($identity -join "`n") -notmatch 'VERSION_ID="?24\.04') {
        throw "Expected an unprivileged Ubuntu 24.04 WSL2 guest. Guest reported: $($identity -join ' ')"
    }

    Invoke-Wsl "curl -fsSL '$InstallerUrl' | sh"
    Invoke-Wsl 'test -x "$HOME/.local/bin/speechcraft" && command -v speechcraft'
    Invoke-Wsl 'speechcraft status'

    Invoke-Wsl 'nohup speechcraft > "$HOME/.local/share/speechcraft/setup-controller/wsl-smoke-launch.log" 2>&1 </dev/null &'
    $deadline = [DateTime]::UtcNow.AddMinutes(2)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:3002/setup' -TimeoutSec 4
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $ready) { throw 'Windows could not reach the WSL-hosted setup UI at http://127.0.0.1:3002/setup within two minutes.' }

    Invoke-Wsl 'speechcraft status'
    Invoke-Wsl 'speechcraft stop'
    Invoke-Wsl 'nohup speechcraft > "$HOME/.local/share/speechcraft/setup-controller/wsl-smoke-restart.log" 2>&1 </dev/null &'
    $deadline = [DateTime]::UtcNow.AddMinutes(2)
    $restarted = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:3002/setup' -TimeoutSec 4
            if ($response.StatusCode -eq 200) { $restarted = $true; break }
        } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $restarted) { throw 'The frontend did not recover after restart.' }

    Invoke-Wsl 'mkdir -p "$HOME/.local/share/speechcraft/setup-controller"; printf smoke-data > "$HOME/.local/share/speechcraft/setup-controller/wsl-smoke-preserve-marker"; speechcraft uninstall --yes; test -f "$HOME/.local/share/speechcraft/setup-controller/wsl-smoke-preserve-marker"; printf "default uninstall preserved persistent data\n"'
    Write-Host 'WSL2 install, command resolution, frontend access from Windows, status, stop, restart and safe uninstall passed.'
}
catch {
    Write-Error $_
    try {
        Invoke-Wsl 'tail -n 120 "$HOME/.local/share/speechcraft/setup-controller/launcher.log" 2>/dev/null || true; tail -n 120 "$HOME/.local/share/speechcraft/setup-controller/wsl-smoke-launch.log" 2>/dev/null || true'
    } catch { Write-Warning "Could not retrieve WSL logs: $_" }
    exit 1
}
