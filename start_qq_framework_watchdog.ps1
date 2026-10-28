# QQ official-client watchdog (NapCat Framework mode) for LuoTavern.
#
# Runs the official QQ NT client with the NapCat Framework loader injected.
# QQ handles login / "auto login"; NapCat only exposes OneBot 11 on port 3001.
#
# - adopts an already running QQ client (never starts a second one)
# - relaunches QQ when it exits
# - if OneBot port 3001 was online and disappears for > 4 minutes, restarts QQ
# - only kills QQ processes that THIS watchdog launched
# - single instance (named mutex)
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File start_qq_framework_watchdog.ps1
#   ... -Once     start/adopt once, do not restart
#
# ASCII only on purpose: Windows PowerShell reads .ps1 as ANSI without a BOM.

param([switch]$Once)

$ErrorActionPreference = 'Continue'
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch { }

$base = Split-Path -Parent $MyInvocation.MyCommand.Path
$fw = Join-Path $base 'NapCat.Framework'
if (-not (Test-Path -LiteralPath (Join-Path $fw 'napimain.exe'))) {
    $parent = Split-Path -Parent $base
    $alt = Join-Path $parent 'NapCat.Framework'
    if (Test-Path -LiteralPath (Join-Path $alt 'napimain.exe')) { $fw = $alt }
}
$napimain = Join-Path $fw 'napimain.exe'
$inject = Join-Path $fw 'napiloader.dll'
$mainLoader = Join-Path $fw 'nativeLoader.cjs'
$logDir = Join-Path $base 'logs'
$logFile = Join-Path $logDir 'qq-framework-watchdog.log'
$port = 3001
$healthSeconds = 15
$portLostGraceSeconds = 240
$heartbeatSeconds = 600

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-Log([string]$msg) {
    $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    Write-Host $line
    try { Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8 } catch { }
}

function Rotate-Log {
    try {
        if ((Test-Path -LiteralPath $logFile) -and ((Get-Item -LiteralPath $logFile).Length -gt 5MB)) {
            Move-Item -LiteralPath $logFile -Destination ($logFile + '.1') -Force
        }
    } catch { }
}

function Test-Port([int]$p) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $p, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        try { $client.Close() } catch { }
    }
}

function Resolve-QQPath {
    try {
        $reg = Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ' -ErrorAction Stop
        $uninstall = [string]$reg.UninstallString
        if ($uninstall -match '"([^"]+)"') {
            $dir = Split-Path -Parent $matches[1]
        } else {
            $dir = Split-Path -Parent $uninstall
        }
        $exe = Join-Path $dir 'QQ.exe'
        if (Test-Path -LiteralPath $exe) { return $exe }
    } catch { }
    foreach ($fallback in @('C:\Program Files\Tencent\QQNT\QQ.exe',
                            'C:\Program Files (x86)\Tencent\QQNT\QQ.exe')) {
        if (Test-Path -LiteralPath $fallback) { return $fallback }
    }
    return $null
}

function Start-QQClient {
    $before = @(Get-Process QQ -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    $env:NAPCAT_INJECT_PATH = $inject
    $env:NAPCAT_LAUNCHER_PATH = $napimain
    $env:NAPCAT_MAIN_PATH = ($mainLoader -replace '\\', '/')
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', ('cd /d "' + $fw + '" && napiLoader.bat') -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 8
    $after = @(Get-Process QQ -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    $new = @($after | Where-Object { $before -notcontains $_ })
    return @{ New = $new; All = $after }
}

# ---------- preconditions ----------
foreach ($f in @($napimain, $inject, $mainLoader)) {
    if (-not (Test-Path -LiteralPath $f)) {
        Write-Log "[ERROR] framework file missing: $f"
        exit 1
    }
}
$qqExe = Resolve-QQPath
if (-not $qqExe) {
    Write-Log '[ERROR] official QQ.exe not found (registry / C:\Program Files\Tencent\QQNT)'
    exit 1
}
Write-Log ("[fw-watchdog] QQ={0} framework={1}" -f $qqExe, $fw)

# ---------- single instance ----------
$mutex = New-Object System.Threading.Mutex($false, 'Local\LuoTavern-QQFrameworkWatchdog')
try {
    if (-not $mutex.WaitOne(0)) {
        Write-Log '[WARN] another framework watchdog is already running; exit.'
        exit 0
    }
} catch {
    Write-Log "[WARN] mutex check failed: $($_.Exception.Message)"
}

$delay = 8
$restarts = 0
$qqPids = @()
$owned = $false

# Adopt an already running client (do not interrupt a login window the user is using).
$existing = @(Get-Process QQ -ErrorAction SilentlyContinue)
if ($existing.Count -gt 0) {
    $qqPids = @($existing | Select-Object -ExpandProperty Id)
    Write-Log ("[fw-watchdog] adopting running QQ client (pids: {0})" -f ($qqPids -join ','))
} else {
    $started = Start-QQClient
    $qqPids = @($started.New)
    $owned = $true
    Write-Log ("[fw-watchdog] launched QQ client (pids: {0})" -f ($qqPids -join ','))
}

try {
    while ($true) {
        $online = $false
        $lastOnline = $null
        $lastBeatAt = Get-Date
        $startedAt = Get-Date

        while ($true) {
            Start-Sleep -Seconds $healthSeconds
            $now = Get-Date
            $qqNow = @(Get-Process QQ -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
            $portUp = Test-Port $port

            if ($portUp) {
                if (-not $online) { Write-Log '[fw-watchdog] OneBot port is up (online)' }
                $online = $true
                $lastOnline = $now
            } elseif ($qqNow.Count -eq 0) {
                Write-Log '[fw-watchdog] QQ client exited'
                break
            } elseif ($online -and $lastOnline -and ($now - $lastOnline).TotalSeconds -ge $portLostGraceSeconds) {
                Write-Log ('[fw-watchdog] OneBot port lost for {0}s; restarting QQ client' -f $portLostGraceSeconds)
                break
            }

            if (($now - $lastBeatAt).TotalSeconds -ge $heartbeatSeconds) {
                Write-Log ("[fw-watchdog] alive (qq={0} port={1} online={2})" -f $qqNow.Count, (Test-Port $port), $online)
                $lastBeatAt = $now
            }
        }

        # Stop only the client we launched; never kill a user-owned QQ account.
        if ($owned) {
            foreach ($pidValue in $qqPids) {
                try { & taskkill.exe /PID $pidValue /T /F | Out-Null } catch { }
            }
        } else {
            Write-Log '[fw-watchdog] QQ was adopted; not killing it. Restart QQ manually if needed.'
        }
        Start-Sleep -Seconds 2

        if (-not $owned) {
            $still = @(Get-Process QQ -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
            if ($still.Count -gt 0) {
                $qqPids = @($still)
                Write-Log '[fw-watchdog] adopted QQ still running; keep watching (no duplicate launch)'
                continue
            }
        }

        $restarts++
        if ($Once) {
            Write-Log '[fw-watchdog] -Once: stop.'
            break
        }
        if (((Get-Date) - $startedAt).TotalSeconds -ge 60) { $delay = 8 }
        Write-Log ("[fw-watchdog] restart #{0} in {1}s ..." -f $restarts, $delay)
        Start-Sleep -Seconds $delay
        $delay = [Math]::Min(120, [Math]::Max(8, $delay * 2))

        $started = Start-QQClient
        $qqPids = @($started.New)
        $owned = $true
        Write-Log ("[fw-watchdog] relaunched QQ client (pids: {0})" -f ($qqPids -join ','))
    }
} finally {
    try { $mutex.ReleaseMutex() } catch { }
    try { $mutex.Dispose() } catch { }
}
