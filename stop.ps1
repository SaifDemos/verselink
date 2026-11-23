# Stop the VerseLink background service started by start.ps1.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$pidFile = Join-Path $PSScriptRoot ".run\verselink.pid"

if (-not (Test-Path $pidFile)) {
    Write-Host "VerseLink is not running (no pid file)."
    exit 0
}

$processId = 0
[void][int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$processId)
if (-not $processId) {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "VerseLink is not running (empty pid file)."
    exit 0
}

$proc = Get-Process -Id $processId -ErrorAction SilentlyContinue
if (-not $proc) {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "VerseLink is not running (stale pid $processId)."
    exit 0
}

$cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue).CommandLine
if ($cmdLine -and $cmdLine -notmatch "verselink\.py") {
    Write-Host "Refusing to stop pid ${processId}: it is not verselink.py." -ForegroundColor Red
    exit 1
}

Stop-Process -Id $processId -Force
Remove-Item -LiteralPath $pidFile -Force
Write-Host "VerseLink stopped (pid $processId)."
