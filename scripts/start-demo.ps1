#Requires -Version 5.1
<#
.SYNOPSIS
  打开三个新窗口启动 LiveKit + 前端 + Agent（connect），便于竞赛演示。
#>
[CmdletBinding()]
param(
    [switch]$SkipPreflight,
    [string]$Room = "ai-room"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$preflight = Join-Path $PSScriptRoot "demo-preflight.ps1"
if (-not $SkipPreflight) {
    & $preflight
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "预检未通过。可先修配置，或加 -SkipPreflight 强行开窗（仍可能启动失败）。" -ForegroundColor Yellow
        Write-Host "无 Key 时请只用: http://localhost:8080/?demo=1" -ForegroundColor Yellow
        exit $LASTEXITCODE
    }
}

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

function Start-DemoWindow([string]$Title, [string]$Command) {
    $escaped = $Command.Replace("'", "''")
    Start-Process -FilePath "powershell.exe" -WorkingDirectory $Root -ArgumentList @(
        "-NoExit",
        "-Command",
        ("`$Host.UI.RawUI.WindowTitle = '{0}'; {1}" -f $Title.Replace("'", "''"), $escaped)
    ) | Out-Null
}

$livekit = Get-Command livekit-server -ErrorAction SilentlyContinue
if (-not $livekit) {
    Write-Host "找不到 livekit-server，跳过 LiveKit 窗口。请先安装并加入 PATH。" -ForegroundColor Yellow
} else {
    Start-DemoWindow "LiveKit" "livekit-server --dev"
}

Start-DemoWindow "Frontend" ("& '{0}' server.py" -f $python.Replace("'", "''"))
Start-Sleep -Seconds 1
Start-DemoWindow "Agent" ("& '{0}' agent.py connect --room {1}" -f $python.Replace("'", "''"), $Room)

Write-Host ""
Write-Host "已尝试打开演示窗口。" -ForegroundColor Green
Write-Host "浏览器打开: http://localhost:8080"
Write-Host "离线演示:   http://localhost:8080/?demo=1"
Write-Host ""
