# Bot launcher (invoked by the .bat files in this folder)
# 用法：启动Discord机器人.bat / 启动QQ机器人.bat 传入 -Platform；
# 不带参数时读取 config.json 的 platform。
param([string]$Platform = '')
$ErrorActionPreference = 'Continue'
$base = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $base

$cfg = Get-Content (Join-Path $base 'config.json') -Raw -Encoding UTF8 -ErrorAction SilentlyContinue | ConvertFrom-Json
$cfgPlatform = if ($cfg -and $cfg.platform) { [string]$cfg.platform } else { 'qq' }
$platform = if ($Platform) { $Platform } else { $cfgPlatform }
$platFile = Join-Path $base ("config." + $platform + ".json")
$platCfg = Get-Content $platFile -Raw -Encoding UTF8 -ErrorAction SilentlyContinue | ConvertFrom-Json
if ($platform -eq 'discord') {
    Write-Host "Platform: discord"
    if (-not ($platCfg.discord -and $platCfg.discord.token)) {
        Write-Host "[WARN] config.discord.json 里 discord.token 为空，无法登录 Discord。"
    }
} else {
    Write-Host "Platform: qq"
    if (-not ($platCfg -and $platCfg.onebot_ws_url)) {
        Write-Host "[WARN] config.qq.json 里 onebot_ws_url 为空，无法连接 NapCat。"
    }
}

function Test-Port([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[ERROR] Python not found. Please install Python 3.10+ first."
    Read-Host "Press Enter to exit"
    exit 1
}

& python -c "import websocket, requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing dependencies ..."
    & python -m pip install -r requirements.txt
}

if ($platform -ne 'discord' -and -not (Test-Port 3001)) {
    # QQ 接入方式：framework=官方 QQ 客户端 + NapCat.Framework（更稳，推荐）
    # shell=NapCat.Shell 无头模式；留空时自动检测 Framework 是否存在。
    $qqMode = ''
    try {
        $qqCfg = Get-Content (Join-Path $base 'config.qq.json') -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
        $qqMode = [string]$qqCfg.login_mode
    } catch { }
    $fw = Join-Path $base 'NapCat.Framework'
    if (-not (Test-Path -LiteralPath (Join-Path $fw 'napimain.exe'))) {
        $alt = Join-Path (Split-Path $base -Parent) 'NapCat.Framework'
        if (Test-Path -LiteralPath (Join-Path $alt 'napimain.exe')) { $fw = $alt }
    }
    $fwReady = Test-Path -LiteralPath (Join-Path $fw 'napimain.exe')
    if (-not $qqMode) { $qqMode = 'shell' }

    if ($qqMode -eq 'framework') {
        if (-not $fwReady) {
            Write-Host "[ERROR] login_mode=framework, but NapCat.Framework was not found."
            Write-Host "        Put NapCat.Framework next to bot.py or in its parent directory."
            Read-Host "Press Enter to exit"
            exit 1
        }
        Write-Host "Starting official QQ client (NapCat Framework mode) ..."
        $watchdog = Join-Path $base 'start_qq_framework_watchdog.ps1'
        if (Test-Path -LiteralPath $watchdog) {
            Start-Process powershell -ArgumentList @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $watchdog + '"')
            ) -WindowStyle Minimized
        } else {
            Write-Host "[WARN] start_qq_framework_watchdog.ps1 not found; cannot launch QQ."
        }
    } else {
        Write-Host "Starting NapCat.Shell (headless mode) ..."
        $napcatDir = Join-Path $base 'NapCat.Shell.Windows.Node'
        if (-not (Test-Path -LiteralPath $napcatDir)) {
            $napcatDir = Join-Path (Split-Path $base -Parent) 'NapCat.Shell.Windows.Node'
        }
        if (-not (Test-Path -LiteralPath (Join-Path $napcatDir 'index.js'))) {
            Write-Host "[ERROR] NapCat.Shell.Windows.Node not found (index.js)."
            Read-Host "Press Enter to exit"
            exit 1
        }
        Start-Process cmd -ArgumentList '/k', ('cd /d "' + $napcatDir + '" && node.exe index.js')
    }

    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        if (Test-Port 3001) { $ok = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ok) {
        if ($qqMode -eq 'framework') {
            Write-Host "[WARN] OneBot port 3001 not ready after 60s."
            Write-Host "       A QQ login window may be waiting (scan / SMS / auto login)."
            Write-Host "       Log: logs\qq-framework-watchdog.log ; NapCat logs: $fw\logs"
        } else {
            Write-Host "[WARN] NapCat did not start in 60s. Check the NapCat window."
        }
    }
}

if ($platform -ne 'discord' -and (Test-Port 6098)) {
    Write-Host "[WARN] AstrBot is running (port 6098). Both bots would reply to the same message."
    $ans = Read-Host "Close AstrBot first. Continue anyway? [Y/N]"
    if ($ans -notmatch '^[Yy]') { exit 1 }
}

Write-Host "Starting $platform bot ..."
Start-Process python -ArgumentList '-u', 'bot.py', '--platform', $platform -WorkingDirectory $base
Write-Host "Done. The bot window is open."
Write-Host "管理台: http://127.0.0.1:8008 （机器人启动后自动开启）"
exit 0
