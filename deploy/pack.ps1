# pack.ps1 — 打成可直接 scp 到 instabox 的部署包（LF 换行，tar.gz）
#   .\pack.ps1
# 产物: dist\jd-auto-deploy.tar.gz
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$stage = Join-Path $env:TEMP "jd-auto-stage"
$dist = Join-Path $root 'dist'

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path (Join-Path $stage 'scripts'), $dist | Out-Null

Copy-Item (Join-Path $root 'deploy\install.sh') (Join-Path $stage 'install.sh')
Copy-Item (Join-Path $root 'deploy\docker-compose.yml') (Join-Path $stage 'docker-compose.yml')
Copy-Item (Join-Path $root 'scripts\*.py') (Join-Path $stage 'scripts')
Copy-Item (Join-Path $root 'README.md') (Join-Path $stage 'README.md') -ErrorAction SilentlyContinue

# bash 脚本必须是 LF，Windows 写出来默认可能带 CRLF
$sh = Join-Path $stage 'install.sh'
$text = [IO.File]::ReadAllText($sh) -replace "`r`n", "`n"
[IO.File]::WriteAllText($sh, $text, (New-Object System.Text.UTF8Encoding($false)))

$out = Join-Path $dist 'jd-auto-deploy.tar.gz'
if (Test-Path $out) { Remove-Item $out -Force }
tar -czf $out -C $stage .
if ($LASTEXITCODE -ne 0) { throw "tar 失败" }

"{0}  {1:N0} KB" -f $out, ((Get-Item $out).Length / 1KB)
tar -tzf $out | Out-String
