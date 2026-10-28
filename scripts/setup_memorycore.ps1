# TencentDB-Agent-Memory local gateway: one-shot setup (idempotent).
# Steps: locate Node -> download repo -> npm install/build -> write LLM env
#        -> start gateway -> health check -> backfill local memories.
# ASCII only on purpose (PowerShell 5.1 + UTF-8 no BOM safety).
param(
  [string]$RepoDir = "E:\TencentDB-Agent-Memory",
  [string]$Branch = "feat/server_team",
  [int]$Port = 8420
)
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Step($msg) { Write-Host ""; Write-Host ("== " + $msg + " ==") -ForegroundColor Cyan }
function Ok($msg)   { Write-Host ("OK   " + $msg) -ForegroundColor Green }
function Fail($msg) { Write-Host ("FAIL " + $msg) -ForegroundColor Red }

function Find-Node {
  $cmd = Get-Command node -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  foreach ($c in @(
      (Join-Path $ProjectDir "NapCat.Shell.Windows.Node\node.exe"),
      (Join-Path $ProjectDir "NapCat.Shell.Windows.Node.old_20260816\node.exe"))) {
    if (Test-Path $c) { return $c }
  }
  return $null
}

function Get-NodeVersion($node) {
  $v = & $node -v 2>$null
  if ($LASTEXITCODE -eq 0 -and $v -match "v?(\d+)\.(\d+)") {
    return @([int]$Matches[1], [int]$Matches[2])
  }
  return $null
}

function Get-EnvFromFile($path, $key) {
  if (-not (Test-Path $path)) { return $null }
  $re = "^" + [regex]::Escape($key) + "\s*=\s*(.*)$"
  foreach ($line in Get-Content $path -Encoding UTF8) {
    if ($line -match $re) {
      $v = $Matches[1].Trim().Trim('"').Trim("'")
      if ($v) { return $v }
    }
  }
  return $null
}

function Download-Repo {
  $zip = Join-Path $env:TEMP "tencentdb-agent-memory.zip"
  $urls = @(
    "https://codeload.github.com/TencentCloud/TencentDB-Agent-Memory/zip/refs/heads/$Branch",
    "https://codeload.github.com/TencentCloud/TencentDB-Agent-Memory/zip/refs/heads/main"
  )
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  foreach ($u in $urls) {
    Step ("Downloading " + $u)
    try {
      Invoke-WebRequest -Uri $u -OutFile $zip -TimeoutSec 600 -UseBasicParsing
      if ((Get-Item $zip).Length -gt 100000) { Ok "downloaded"; return $true }
    } catch {
      Write-Host ("download failed: " + $_.Exception.Message)
    }
  }
  return $false
}

function Install-Repo($node) {
  $npm = Join-Path (Split-Path -Parent $node) "npm.cmd"
  if (-not (Test-Path $npm)) { $npm = "npm" }
  Step "npm install (this takes a few minutes)"
  & $npm install
  if ($LASTEXITCODE -ne 0) {
    Write-Host "npm install failed, retrying with npmmirror registry..."
    & $npm install --registry=https://registry.npmmirror.com
    if ($LASTEXITCODE -ne 0) { Fail "npm install failed twice"; return $false }
  }
  Step "npm run build"
  & $npm run build
  if ($LASTEXITCODE -ne 0) {
    Write-Host "build reported failure; some versions run from src/ directly, continuing..."
  }
  return $true
}

function Write-EnvFile {
  $envPath = Join-Path $RepoDir "memorycore-env.ps1"
  $base = Get-EnvFromFile (Join-Path $ProjectDir ".env") "DEEPSEEK_BASE_URL"
  $key  = Get-EnvFromFile (Join-Path $ProjectDir ".env") "DEEPSEEK_API_KEY"
  $model = "deepseek-v4-flash"
  if (-not $base) {
    $base = Read-Host "DEEPSEEK_BASE_URL (press Enter for https://api.deepseek.com)"
    if (-not $base) { $base = "https://api.deepseek.com" }
  }
  if (-not $key) { $key = Read-Host "DEEPSEEK_API_KEY (required)" }
  if (-not $key) { Fail "DeepSeek API key is required"; return $false }
  $lines = @()
  $lines += '$env:TDAI_LLM_BASE_URL = "' + $base + '"'
  $lines += '$env:TDAI_LLM_API_KEY = "' + $key + '"'
  $lines += '$env:TDAI_LLM_MODEL = "' + $model + '"'
  $lines | Out-File -FilePath $envPath -Encoding ASCII
  Ok ("LLM env written: " + $envPath)
  return $true
}

function Start-Gateway {
  $start = Join-Path $ProjectDir "scripts\start_memorycore.ps1"
  if (-not (Test-Path $start)) { Fail "start_memorycore.ps1 missing"; return $false }
  Step "starting gateway (minimized window keeps running in background)"
  $p = Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File",('"' + $start + '"') -WindowStyle Minimized -PassThru
  Write-Host ("gateway process started, PID " + $p.Id)
  for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try {
      $r = Invoke-WebRequest -Uri ("http://127.0.0.1:" + $Port + "/health") -UseBasicParsing -TimeoutSec 2
      if ($r.StatusCode -eq 200) {
        Ok ("gateway healthy on http://127.0.0.1:" + $Port)
        return $true
      }
    } catch {}
  }
  Fail "gateway did not become healthy within 120s; check the minimized window logs"
  return $false
}

function Find-Python {
  if (Get-Command python -ErrorAction SilentlyContinue) { return "python" }
  if (Get-Command py -ErrorAction SilentlyContinue) { return "py -3" }
  return $null
}

# ---------------- main ----------------
Step "1/6 locating Node.js"
$node = Find-Node
if (-not $node) { Fail "node.exe not found; install Node.js LTS (>=22.16) from nodejs.org"; exit 1 }
Write-Host ("node: " + $node)
$ver = Get-NodeVersion $node
if ($ver) { Write-Host ("version: " + $ver[0] + "." + $ver[1]) }
if (-not $ver -or $ver[0] -lt 22) {
  Fail ("Node >= 22.16 required. Please install Node.js LTS, then run this again.")
  exit 1
}

$coreDir = Join-Path $RepoDir "MemoryCore"
if (-not (Test-Path (Join-Path $coreDir "package.json"))) {
  Step "2/6 downloading TencentDB-Agent-Memory"
  if (-not (Download-Repo)) { Fail "download failed"; exit 1 }
  Step "3/6 extracting"
  $zip = Join-Path $env:TEMP "tencentdb-agent-memory.zip"
  $tmp = Join-Path $env:TEMP "tam_extract"
  if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
  Expand-Archive -Path $zip -DestinationPath $tmp -Force
  $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
  if (-not $inner) { Fail "extract failed"; exit 1 }
  New-Item -ItemType Directory -Path $RepoDir -Force | Out-Null
  Move-Item (Join-Path $inner.FullName "*") $RepoDir -Force
  Remove-Item $tmp -Recurse -Force
  Ok ("extracted to " + $RepoDir)
} else {
  Ok ("MemoryCore already installed: " + $coreDir)
}

if (-not (Test-Path (Join-Path $RepoDir "memorycore-env.ps1"))) {
  Step "4/6 writing LLM config from project .env"
  if (-not (Write-EnvFile)) { exit 1 }
} else {
  Ok "LLM env already configured"
}

if (-not (Test-Path (Join-Path $coreDir "node_modules"))) {
  Set-Location $coreDir
  if (-not (Install-Repo $node)) { exit 1 }
} else {
  Ok "node_modules already installed"
}

Step "5/6 starting gateway + health check"
if (-not (Start-Gateway)) { exit 1 }

Step "6/6 backfilling existing memories (local -> TencentDB)"
$py = Find-Python
if ($py) {
  Push-Location $ProjectDir
  if ($py -eq "python") {
    & python scripts\migrate_to_tencentdb.py --apply
  } else {
    & cmd /c "py -3 scripts\migrate_to_tencentdb.py --apply"
  }
  Pop-Location
  if ($LASTEXITCODE -eq 0) { Ok "memory backfill finished" }
} else {
  Write-Host "python not found; skip backfill. Run manually: python scripts\migrate_to_tencentdb.py --apply"
}

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " DONE. The gateway runs in a minimized window (kept alive in background)."
Write-Host " Verify:  python bot.py --check   ->  should show backend: hybrid, healthy"
Write-Host " Daily use: double-click start_tencentdb_gateway.bat"
Write-Host "==============================================================" -ForegroundColor Green
