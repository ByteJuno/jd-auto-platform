# run.ps1 — 这个平台唯一的入口。兼容 Windows PowerShell 5.1 / PowerShell 7。
#
# 直接跑可能被杀执行策略拦住，用同目录的 run.cmd 包一层最省事:
#     .\run.cmd up
#
#     .\run.ps1 up           # 起青龙
#     .\run.ps1 init         # 装依赖 + 建订阅 + 建任务（先改好 .env 的 QL_PASS）
#     .\run.ps1 push         # 把 ck.txt 推入青龙
#     .\run.ps1 watch        # ck 存活巡检
#     .\run.ps1 ps           # 容器 + 任务状态
#     .\run.ps1 logs         # 跟日志
#     .\run.ps1 down         # 停
param(
  [Parameter(Position = 0)]
  [ValidateSet('up', 'down', 'init', 'push', 'watch', 'ps', 'logs', 'upgrade', 'help')]
  [string]$Cmd = 'help',
  [string]$File = 'ck.txt',
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

# Windows 控制台默认 GBK，python 打中文会花屏
try {
  [Console]::OutputEncoding = [Text.Encoding]::UTF8
  $env:PYTHONUTF8 = '1'
  $env:PYTHONIOENCODING = 'utf-8'
} catch { }

function Import-DotEnv {
  $p = Join-Path $root '.env'
  if (-not (Test-Path $p)) {
    Write-Host "[!] 没有 .env，先执行: Copy-Item .env.example .env" -ForegroundColor Yellow
    return
  }
  Get-Content $p | ForEach-Object {
    $line = $_.Trim()
    if (-not $line) { return }
    if ($line.StartsWith('#')) { return }
    $i = $line.IndexOf('=')
    if ($i -lt 1) { return }
    $k = $line.Substring(0, $i).Trim()
    $v = $line.Substring($i + 1).Trim().Trim('"').Trim("'")
    Set-Item -Path "Env:$k" -Value $v
  }
}

function Invoke-Py {
  param([string[]]$PyArgs)
  & python @PyArgs
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Import-DotEnv

$port = $env:QL_PORT
if (-not $port) { $port = '5700' }

switch ($Cmd) {
  'up' {
    docker compose up -d
    Write-Host ""
    Write-Host "[ok] 面板: http://127.0.0.1:$port" -ForegroundColor Green
    Write-Host "     首次进面板设置 admin 密码，然后改 .env 里的 QL_PASS" -ForegroundColor DarkGray
    Start-Sleep -Seconds 3
    docker compose ps
  }
  'down' { docker compose down }
  'logs' { docker compose logs -f --tail=120 }
  'ps' {
    docker compose ps
    Write-Host ""
    docker exec ql ql check 2>$null
    if ($LASTEXITCODE -ne 0) {
      Write-Host "(容器未起或 ql CLI 不可用)" -ForegroundColor DarkGray
    }
  }
  'upgrade' {
    docker compose pull
    docker compose up -d
    docker image prune -f | Out-Null
    Write-Host "[ok] 已升级" -ForegroundColor Green
  }
  'init' {
    $a = @((Join-Path $root 'scripts\ql_bootstrap.py'))
    if ($DryRun) { $a += '--dry-run' }
    Invoke-Py $a
  }
  'push' {
    $a = @((Join-Path $root 'scripts\ck_push.py'), '--file', (Join-Path $root $File))
    if ($DryRun) { $a += '--dry-run' }
    Invoke-Py $a
  }
  'watch' {
    $a = @((Join-Path $root 'scripts\ck_watch.py'))
    if (Test-Path (Join-Path $root $File)) {
      $a += @('--file', (Join-Path $root $File))
    } else {
      $a += '--from-ql'
    }
    Invoke-Py $a
  }
  default {
    Write-Host "用法: .\run.ps1 <up|init|push|watch|ps|logs|upgrade|down> [-DryRun] [-File ck.txt]"
    Write-Host ""
    Write-Host "  up       起青龙容器 (5700)"
    Write-Host "  init     装依赖 + 建订阅 + 建定时任务，幂等"
    Write-Host "  push     把 ck 推入青龙，按 pt_pin 去重"
    Write-Host "  watch    ck 存活巡检，失效推告警"
    Write-Host "  ps       容器状态 + 内置自检"
    Write-Host "  logs     跟容器日志"
    Write-Host "  upgrade  拉新镜像重建"
    Write-Host "  down     停容器"
    Write-Host ""
    Write-Host "建议第一次: .\run.ps1 up  →  面板设密码  →  .\run.ps1 init -DryRun  →  .\run.ps1 init"
  }
}
