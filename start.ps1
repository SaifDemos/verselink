# Start VerseLink in the background and print the URLs to open on other devices.
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$Bind = "0.0.0.0",
    [string]$Player = ""
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$runDir = Join-Path $PSScriptRoot ".run"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$pidFile = Join-Path $runDir "verselink.pid"
$logFile = Join-Path $runDir "verselink.log"
$errFile = Join-Path $runDir "verselink.err.log"

function Get-LanUrls([int]$p) {
    try {
        [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
            Where-Object {
                $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
                -not $_.IPAddressToString.StartsWith("127.")
            } |
            ForEach-Object { $_.IPAddressToString } |
            Select-Object -Unique |
            ForEach-Object { "  network: http://${_}:$p" }
    } catch {}
}

if (Test-Path $pidFile) {
    $oldPid = 0
    [void][int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$oldPid)
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Host "VerseLink is already running (pid $oldPid)."
        Write-Host "  local:   http://127.0.0.1:$Port"
        Get-LanUrls $Port
        exit 0
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $exe = $pyLauncher.Source
    $pre = @("-3")
} else {
    $pyLauncher = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pyLauncher) {
        Write-Host "error: Python 3 was not found on PATH. Install it from https://python.org" -ForegroundColor Red
        exit 1
    }
    $exe = $pyLauncher.Source
    $pre = @()
}

$argList = $pre + @("verselink.py", "--host", $Bind, "--port", "$Port")
if ($Player) {
    $argList += @("--player", $Player)
}

$proc = Start-Process -FilePath $exe -ArgumentList $argList `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $logFile -RedirectStandardError $errFile
Set-Content -LiteralPath $pidFile -Value $proc.Id

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 200
    if ($proc.HasExited) { break }
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 -Uri "http://127.0.0.1:$Port/ping" | Out-Null
        $ready = $true
        break
    } catch {}
}

if (-not $ready) {
    if (-not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "VerseLink failed to start." -ForegroundColor Red
    if (Test-Path $errFile) { Get-Content -LiteralPath $errFile -Tail 30 }
    if (Test-Path $logFile) { Get-Content -LiteralPath $logFile -Tail 30 }
    exit 1
}

Write-Host "VerseLink is running (pid $($proc.Id))."
Write-Host "  local:   http://127.0.0.1:$Port"
if ($Bind -notin @("0.0.0.0", "::", "")) {
    Write-Host "  network: http://${Bind}:$Port"
} else {
    Get-LanUrls $Port
    Write-Host "  (anyone on this network can control the player; use -Bind 127.0.0.1 to stay local)"
    Write-Host "  If another device cannot connect, allow Python through Windows Firewall."
}
Write-Host "  log:     $logFile"
