#Requires -Version 5.1
<#
.SYNOPSIS
  竞赛演示前预检：venv、.env Key、livekit-server、常用端口。
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Check([bool]$Ok, [string]$Label, [string]$Detail = "") {
    $mark = if ($Ok) { "[OK]" } else { "[!!]" }
    $color = if ($Ok) { "Green" } else { "Yellow" }
    if ($Detail) {
        Write-Host ("{0} {1} — {2}" -f $mark, $Label, $Detail) -ForegroundColor $color
    } else {
        Write-Host ("{0} {1}" -f $mark, $Label) -ForegroundColor $color
    }
}

function Test-TcpPort([int]$Port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(400)
        if ($ok -and $client.Connected) {
            $client.EndConnect($iar) | Out-Null
            $client.Close()
            return $true
        }
        $client.Close()
        return $false
    } catch {
        return $false
    }
}

Write-Host ""
Write-Host "AI Interviewer 演示预检" -ForegroundColor Cyan
Write-Host ("项目目录: {0}" -f $Root)
Write-Host ""

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
$hasVenv = Test-Path $venvPython
Write-Check $hasVenv "Python venv" $(if ($hasVenv) { $venvPython } else { "缺少 .venv，请先 python -m venv .venv && pip install -r requirements.txt" })

$envPath = Join-Path $Root ".env"
$hasEnv = Test-Path $envPath
Write-Check $hasEnv ".env 文件" $(if ($hasEnv) { $envPath } else { "请复制 .env.example 为 .env 并填入 Key" })

$llmReady = $false
$speechReady = $false
if ($hasEnv) {
    $envMap = @{}
    Get-Content -LiteralPath $envPath -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $parts = $line.Split("=", 2)
        if ($parts.Count -eq 2) {
            $envMap[$parts[0].Trim()] = $parts[1].Trim().Trim('"').Trim("'")
        }
    }
    $llmKey = $envMap["LLM_API_KEY"]
    if (-not $llmKey) { $llmKey = $envMap["SILICONFLOW_API_KEY"] }
    $speechKey = $envMap["SILICONFLOW_API_KEY"]
    $llmReady = [bool]$llmKey -and $llmKey -ne "sk-dummy" -and $llmKey -notmatch "your_.*_api_key"
    $speechReady = [bool]$speechKey -and $speechKey -ne "sk-dummy" -and $speechKey -notmatch "your_.*_api_key"
}
Write-Check $llmReady "LLM_API_KEY" $(if ($llmReady) { "已配置" } else { "缺失或仍是占位值" })
Write-Check $speechReady "SILICONFLOW_API_KEY" $(if ($speechReady) { "已配置" } else { "缺失或仍是占位值（STT/TTS）" })

$livekitCmd = Get-Command livekit-server -ErrorAction SilentlyContinue
Write-Check ([bool]$livekitCmd) "livekit-server" $(
    if ($livekitCmd) { $livekitCmd.Source } else { "不在 PATH；可用 scripts/install_livekit_tools.sh（WSL）或自行安装" }
)

$lkUp = Test-TcpPort 7880
$httpUp = Test-TcpPort 8080
Write-Check $lkUp "端口 7880 (LiveKit)" $(if ($lkUp) { "已有进程在听" } else { "空闲，待启动" })
Write-Check $httpUp "端口 8080 (前端)" $(if ($httpUp) { "已有进程在听" } else { "空闲，待启动" })

Write-Host ""
Write-Host "三进程启动命令：" -ForegroundColor Cyan
Write-Host "  1) livekit-server --dev"
Write-Host "  2) .\.venv\Scripts\python.exe server.py"
Write-Host "  3) .\.venv\Scripts\python.exe agent.py connect --room ai-room"
Write-Host ""
Write-Host "或直接运行:  .\scripts\start-demo.ps1"
Write-Host "无 Key 演示:  http://localhost:8080/?demo=1"
Write-Host ""

if (-not ($hasVenv -and $hasEnv -and $llmReady -and $speechReady)) {
    exit 1
}
exit 0
