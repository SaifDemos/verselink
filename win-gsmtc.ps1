param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('list', 'state', 'cmd', 'seek', 'art')]
    [string]$Op,
    [string]$Player = '',
    [string]$Command = '',
    [double]$Seconds = 0
)

# Windows media backend for VerseLink. Called by verselink.py; prints one JSON
# object per invocation. Uses the built-in Windows.GSMTC APIs, so it works with
# any app that integrates with Windows media controls (no Python packages).

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
} catch {}

function Write-Json($obj) {
    $obj | ConvertTo-Json -Compress -Depth 6 | Write-Output
}

try {
    [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
    [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null

    $mgr = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync().GetAwaiter().GetResult()
    $sessions = @($mgr.GetSessions())

    function Select-Session {
        if ($Player) {
            return $sessions | Where-Object { $_.SourceAppUserModelId -eq $Player } | Select-Object -First 1
        }
        $s = $mgr.GetCurrentSession()
        if (-not $s) {
            $s = $sessions | Where-Object {
                $_.GetPlaybackInfo().PlaybackStatus.ToString() -ne 'Closed'
            } | Select-Object -First 1
        }
        return $s
    }

    function Get-Media($session) {
        try { return $session.TryGetMediaPropertiesAsync().GetAwaiter().GetResult() }
        catch { return $null }
    }

    switch ($Op) {
        'list' {
            $ids = @($sessions |
                ForEach-Object { $_.SourceAppUserModelId } |
                Where-Object { $_ })
            Write-Json @{ players = $ids }
        }
        'state' {
            $s = Select-Session
            if (-not $s) {
                Write-Json @{ status = 'none' }
                break
            }
            $info = $s.GetPlaybackInfo()
            $status = switch ($info.PlaybackStatus.ToString()) {
                'Playing' { 'Playing' }
                'Paused' { 'Paused' }
                'Stopped' { 'Stopped' }
                default { 'Paused' }
            }
            $media = Get-Media $s
            $title = ''
            $artist = ''
            $album = ''
            if ($media) {
                $title = [string]$media.Title
                $artist = [string]$media.Artist
                $album = [string]$media.AlbumTitle
            }
            $pos = 0.0
            $dur = 0.0
            try {
                $tl = $s.GetTimelineProperties()
                $pos = [math]::Max(0, $tl.Position.TotalSeconds)
                $dur = [math]::Max(0, $tl.EndTime.TotalSeconds)
            } catch {}
            Write-Json @{
                status = $status
                id = $s.SourceAppUserModelId
                title = $title
                artist = $artist
                album = $album
                pos = [double]$pos
                dur = [double]$dur
            }
        }
        'cmd' {
            $s = Select-Session
            $ok = $false
            if ($s) {
                $ok = switch ($Command) {
                    'play-pause' { [bool]$s.TryTogglePlayPauseAsync().GetAwaiter().GetResult() }
                    'next' { [bool]$s.TrySkipNextAsync().GetAwaiter().GetResult() }
                    'previous' { [bool]$s.TrySkipPreviousAsync().GetAwaiter().GetResult() }
                    default { $false }
                }
            }
            Write-Json @{ ok = $ok }
        }
        'seek' {
            $s = Select-Session
            $ok = $false
            if ($s) {
                $target = [TimeSpan]::FromSeconds($Seconds)
                $ok = [bool]$s.TryChangePlaybackPositionAsync($target).GetAwaiter().GetResult()
            }
            Write-Json @{ ok = $ok }
        }
        'art' {
            $s = Select-Session
            $b64 = $null
            if ($s) {
                $media = Get-Media $s
                $thumb = if ($media) { $media.Thumbnail } else { $null }
                if ($thumb) {
                    $stream = $thumb.OpenReadAsync().GetAwaiter().GetResult()
                    try {
                        $size = [uint32]$stream.Size
                        if ($size -gt 0 -and $size -le 12582912) {
                            $reader = [Windows.Storage.Streams.DataReader]::new($stream)
                            try {
                                $loaded = $reader.LoadAsync($size).GetAwaiter().GetResult()
                                $bytes = New-Object byte[] $loaded
                                $reader.ReadBytes($bytes)
                                $b64 = [Convert]::ToBase64String($bytes)
                            } finally { $reader.Dispose() }
                        }
                    } finally { $stream.Dispose() }
                }
            }
            Write-Json @{ b64 = $b64 }
        }
    }
} catch {
    Write-Json @{ error = $_.Exception.Message }
}
