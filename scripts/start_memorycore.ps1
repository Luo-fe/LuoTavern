# Start the local TencentDB-Agent-Memory gateway (foreground, shows logs).
# ASCII only (PowerShell 5.1 + UTF-8 no BOM safety).
param(
  [string]$RepoDir = "E:\TencentDB-Agent-Memory",
  [int]$Port = 8420
)
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

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

$core = Join-Path $RepoDir "MemoryCore"
if (-not (Test-Path (Join-Path $core "package.json"))) {
  Write-Host "MemoryCore not installed. Run setup_tencentdb_gateway.bat first."
  exit 1
}
$node = Find-Node
if (-not $node) {
  Write-Host "node.exe not found. Install Node.js LTS (>=22.16) from nodejs.org."
  exit 1
}
$envFile = Join-Path $RepoDir "memorycore-env.ps1"
if (-not (Test-Path $envFile)) {
  Write-Host "LLM env file missing: $envFile"
  exit 1
}
. $envFile
Set-Location $core
if (-not (Test-Path "node_modules")) {
  Write-Host "node_modules missing; run setup_tencentdb_gateway.bat first."
  exit 1
}
Write-Host ("starting MemoryCore gateway on http://127.0.0.1:" + $Port + " ...")
if (Test-Path "src\gateway\server.ts") {
  & $node --import tsx src/gateway/server.ts
} else {
  Write-Host "src/gateway/server.ts not found, trying npm start ..."
  & $node (Join-Path $core "node_modules\npm\bin\npm-cli.js") start
}
