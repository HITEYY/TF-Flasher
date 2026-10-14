# TF-Flasher one-line installer (ASCII only: piped through iex, so no BOM / no Korean here)
#   irm https://raw.githubusercontent.com/HITEYY/TF-Flasher/master/get.ps1 | iex
# Options via env vars before running:  $env:TF_EDL=1 (gen 2 edl), $env:TF_DIAG=1 (USB/driver check only),
#                                       $env:TF_DIR='D:\TF-Flasher'
# Downloads the repo into $HOME\TF-Flasher (backup/work/tools kept on update), runs install.ps1, launches the GUI.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $dir = if ($env:TF_DIR) { $env:TF_DIR } else { Join-Path $HOME 'TF-Flasher' }
    $zip = Join-Path $env:TEMP 'tf-flasher.zip'
    $tmp = Join-Path $env:TEMP 'tf-flasher-src'

    Write-Host "== Downloading TF-Flasher -> $dir" -ForegroundColor Cyan
    Invoke-WebRequest 'https://github.com/HITEYY/TF-Flasher/archive/refs/heads/master.zip' -OutFile $zip
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive $zip $tmp
    if (-not (Test-Path (Join-Path $tmp 'TF-Flasher-master\install.ps1'))) {
        throw 'Downloaded archive has no install.ps1 (GitHub cache right after a push?). Retry in a minute.'
    }
    New-Item -ItemType Directory -Force $dir | Out-Null
    Copy-Item -Recurse -Force (Join-Path $tmp 'TF-Flasher-master\*') $dir
    Remove-Item -Recurse -Force $tmp, $zip

    $opts = @()
    if ($env:TF_EDL) { $opts += '-Edl' }
    if ($env:TF_DIAG) { $opts += '-Diag' }
    # Run install.ps1 as a file (it has a UTF-8 BOM for Korean text) in the same PowerShell flavor
    & (Get-Process -Id $PID).Path -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dir 'install.ps1') @opts
    if ($LASTEXITCODE -ne 0) { Write-Host "install.ps1 failed (exit $LASTEXITCODE)" -ForegroundColor Red }
}
