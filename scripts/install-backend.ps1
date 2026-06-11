<#
.SYNOPSIS
  EchoDesk · 一键 backend 安装脚本（Windows / PowerShell 版，对应 install-backend.sh）

.DESCRIPTION
  在用户 Windows 机器上准备 EchoDesk.exe 运行所需的 Python 资源：
    %USERPROFILE%\.echodesk\source\backend\         backend 源码副本
    %USERPROFILE%\.echodesk\source\backend\.venv\    独立 venv（Electron resolvePython 第一候选）
    %USERPROFILE%\.echodesk\config.json              默认用户配置（已存在则保留）
    %USERPROFILE%\.echodesk\logs\                    backend log 目录

.PARAMETER RepoPath
  显式指定仓库路径；不传则按脚本位置推断（scripts\install-backend.ps1 的上级）。

.PARAMETER Uninstall
  删除整个 %USERPROFILE%\.echodesk（会要求确认）。

.PARAMETER ResetConfig
  仅把 config.json 重置为默认（保留 db / log）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-backend.ps1

.NOTES
  退出码：0 成功 / 1 缺 Python / 2 参数路径错 / 3 pip 失败 / 4 smoke 失败
  兼容：Windows 10/11 x64，Python 3.11 / 3.12
#>

[CmdletBinding()]
param(
    [string]$RepoPath = "",
    [switch]$Uninstall,
    [switch]$ResetConfig
)

$ErrorActionPreference = "Stop"

function Info($m) { Write-Host "--> $m" -ForegroundColor Blue }
function Ok($m)   { Write-Host "OK  : $m" -ForegroundColor Green }
function Warn($m) { Write-Host "WARN: $m" -ForegroundColor Yellow }
function Err($m)  { Write-Host "ERROR: $m" -ForegroundColor Red }

$EchodeskHome = Join-Path $env:USERPROFILE ".echodesk"
$SourceDir    = Join-Path $EchodeskHome "source"
$BackendDir   = Join-Path $SourceDir "backend"
$VenvDir      = Join-Path $BackendDir ".venv"
$VenvPy       = Join-Path $VenvDir "Scripts\python.exe"
$UserConfig   = Join-Path $EchodeskHome "config.json"
$LogDir       = Join-Path $EchodeskHome "logs"

# 默认 config：不内置任何 key（避免泄露）。
# 推荐对外分发填 EchoGatewayUrl/Token（网关模式，自动改写所有上游）；
# 留空则走直连模式（需自己填 yunwu_open_key + heyi 各地址）。
$DefaultConfig = [ordered]@{
    port                = 8769
    log_level           = "INFO"
    stt_backend         = "firered"
    stt_firered_url     = "http://localhost:8090"
    stt_language        = "zh"
    tts_enabled         = $true
    tts_provider        = "qwen3_tts"
    tts_qwen3_url       = "http://localhost:8094"
    llm_main_provider   = "yunwu"
    llm_main_model      = "MiniMax-M2.7"
    llm_main_base_url   = "https://yunwu.ai/v1"
    llm_fast_base_url   = "http://localhost:7860/v1"
    yunwu_open_key      = ""
    tavily_api_key      = ""
    diarizer_enabled    = $true
    echo_gateway_url    = ""
    echo_gateway_token  = ""
}

# ---------- 卸载 ----------
if ($Uninstall) {
    Info "卸载 EchoDesk backend 数据"
    if (-not (Test-Path $EchodeskHome)) { Ok "无需卸载（$EchodeskHome 不存在）"; exit 0 }
    Warn "这会删除 $EchodeskHome 下所有数据（会议库 / 录音 / 索引 / 配置 / venv / log）"
    $ans = Read-Host "确认彻底删除? (yes/NO)"
    if ($ans -ne "yes") { Info "已取消"; exit 0 }
    Remove-Item -Recurse -Force $EchodeskHome
    Ok "已删除 $EchodeskHome"
    exit 0
}

Write-Host ""
Write-Host "==== EchoDesk · install-backend.ps1 (Windows) ====" -ForegroundColor Blue
Write-Host ""

# ---------- step 1：解析仓库 ----------
Info "step 1: 解析仓库源路径"
if ([string]::IsNullOrWhiteSpace($RepoPath)) {
    $RepoPath = Split-Path -Parent $PSScriptRoot   # scripts\ 的上级 = 仓库根
}
if (-not (Test-Path (Join-Path $RepoPath "backend\requirements.txt"))) {
    Err "$RepoPath 不像 echo-demo 仓库（缺 backend\requirements.txt）"
    exit 2
}
Ok "仓库源: $RepoPath"

# ---------- step 2：检查 Python 3.11/3.12 ----------
Info "step 2: 检查 Python 3.11+"
$python = $null
$cands = @()
if ($env:ECHO_INSTALL_PYTHON) { $cands += $env:ECHO_INSTALL_PYTHON }
# py launcher 优先（Windows 官方安装器自带）
$cands += @("py -3.11", "py -3.12", "python", "python3")
foreach ($c in $cands) {
    try {
        $parts = $c.Split(" ")
        $exe = $parts[0]
        $args = $parts[1..($parts.Length - 1)] + @("--version")
        $v = & $exe $args 2>&1 | Out-String
        if ($v -match "Python 3\.(11|12)\.") { $python = $c; break }
    } catch { continue }
}
if (-not $python) {
    Err "没找到 Python 3.11 或 3.12。请从 https://www.python.org/downloads/ 安装（勾选 Add to PATH），或 winget install Python.Python.3.11"
    exit 1
}
Ok "Python: $python"

# ---------- step 3：准备目录 ----------
Info "step 3: 准备 $EchodeskHome"
New-Item -ItemType Directory -Force -Path $EchodeskHome, $SourceDir, $LogDir | Out-Null
Ok "目录就绪"

# ---------- step 4：同步 backend 源码（robocopy） ----------
Info "step 4: 同步 backend 源码到 $BackendDir"
New-Item -ItemType Directory -Force -Path $BackendDir | Out-Null
# robocopy /MIR 镜像；排除 .venv / 缓存。退出码 0-7 都算成功（8+ 才是错误）
$src = Join-Path $RepoPath "backend"
robocopy $src $BackendDir /MIR /NFL /NDL /NJH /NJS /NP `
    /XD ".venv" "__pycache__" ".pytest_cache" "htmlcov" `
    /XF "*.pyc" ".coverage" | Out-Null
if ($LASTEXITCODE -ge 8) { Err "robocopy 同步失败（code=$LASTEXITCODE）"; exit 2 }
Ok "backend 源码同步完成"

# ---------- step 5：创建/复用 venv ----------
Info "step 5: 创建 venv $VenvDir"
if (-not (Test-Path $VenvPy)) {
    $parts = $python.Split(" ")
    & $parts[0] ($parts[1..($parts.Length - 1)] + @("-m", "venv", $VenvDir))
    Ok "venv 已创建"
} else {
    Ok "venv 复用现有"
}

# ---------- step 6：装依赖 ----------
Info "step 6: pip install -r requirements.txt（首次约 3-10 min，含 torch）"
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -r (Join-Path $BackendDir "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { Err "pip install 失败"; exit 3 }
Ok "依赖装好"

# ---------- step 6.5：ppt_ib_deck node deps（best-effort） ----------
$deckDir = Join-Path $BackendDir "app\adapters\skill\assets\ppt_ib_deck"
if ((Test-Path (Join-Path $deckDir "package.json")) -and (Get-Command npm -ErrorAction SilentlyContinue)) {
    Info "step 6.5: 装 ppt_ib_deck node deps"
    Push-Location $deckDir
    try { npm install --silent --no-audit --no-fund } catch { Warn "npm install 失败 → @生成 ppt 会 fallback legacy" }
    Pop-Location
} else {
    Warn "step 6.5: 跳过 ppt_ib_deck node deps（无 npm 或资产缺失）→ @生成 ppt fallback legacy"
}

# ---------- step 7：写 config ----------
Info "step 7: 写 $UserConfig"
if ((Test-Path $UserConfig) -and (-not $ResetConfig)) {
    Ok "config.json 已存在，保留（用 -ResetConfig 重置）"
} else {
    ($DefaultConfig | ConvertTo-Json) | Set-Content -Path $UserConfig -Encoding UTF8
    Ok "config.json 已写入默认值"
    Warn "网关模式：填 echo_gateway_url + echo_gateway_token（推荐对外分发）"
    Warn "直连模式：填 yunwu_open_key；否则 @生成/纪要 功能灰"
}

# ---------- step 8：smoke test ----------
Info "step 8: smoke test（import + 启停一次）"
Push-Location $BackendDir
$env:ECHO_USER_DIR = $EchodeskHome
& $VenvPy -c "from app.config import get_settings; from app.main import create_app; print('import ok')"
if ($LASTEXITCODE -ne 0) { Err "import 失败"; Pop-Location; exit 4 }
Ok "import 干净"

$proc = Start-Process -FilePath $VenvPy `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8769", "--log-level", "warning") `
    -PassThru -WindowStyle Hidden `
    -RedirectStandardError (Join-Path $env:TEMP "echodesk-smoke.log")
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8769/healthz" -TimeoutSec 1 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
}
if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
Pop-Location
if (-not $healthy) { Err "smoke 15s 内 healthz 不通；看 $env:TEMP\echodesk-smoke.log"; exit 4 }
Ok "smoke 通过"

Write-Host ""
Write-Host "==== 安装完成 ====" -ForegroundColor Green
Write-Host @"
EchoDesk backend 就位：
  venv:   $VenvPy
  config: $UserConfig
  data:   $EchodeskHome

下一步：
  1. 安装/打开 EchoDesk（双击 EchoDesk Setup .exe 或 portable exe）
  2. Electron 会自动 spawn backend

要填的配置（编辑 $UserConfig，重启生效）：
  - 推荐：echo_gateway_url + echo_gateway_token（网关模式，凭证在服务端）
  - 或直连：yunwu_open_key + tavily_api_key
"@ -ForegroundColor Green
