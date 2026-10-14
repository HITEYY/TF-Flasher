# TF-Flasher Windows 설치 스크립트
# 실행: powershell -ExecutionPolicy Bypass -File install.ps1 [-Edl] [-Rebuild] [-SkipZadig]
#   -Edl       2세대(Qualcomm EDL)용 edl도 설치
#   -Rebuild   tools\rkdeveloptool.exe 있어도 다시 빌드
#   -SkipZadig Zadig(USB 드라이버) 받기/실행 생략
# 하는 일: Python 3 / MSYS2 설치(winget) -> rkdeveloptool 빌드 -> tools\ 에 exe+DLL -> (edl) -> Zadig
param([switch]$Edl, [switch]$Rebuild, [switch]$SkipZadig)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = $PSScriptRoot
$tools = Join-Path $root 'tools'
$msys = 'C:\msys64'
New-Item -ItemType Directory -Force $tools | Out-Null

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Winget-Install($id, $extra = @()) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget 없음. Microsoft Store에서 '앱 설치 관리자'를 설치하거나 $id 를 직접 설치하세요."
    }
    winget install -e --id $id --accept-source-agreements --accept-package-agreements @extra
    if ($LASTEXITCODE -ne 0) { throw "$id 설치 실패 (winget 코드 $LASTEXITCODE)" }
    Refresh-Path
}

# MSYS2 bash로 스크립트 실행 (인자 인용 문제 피하려고 파일로 넘김)
function Msys($script) {
    $f = Join-Path $env:TEMP 'tf-flasher-msys.sh'
    [IO.File]::WriteAllText($f, $script.Replace("`r`n", "`n"))
    $env:MSYSTEM = 'MINGW64'
    $env:CHERE_INVOKING = '1'
    & "$msys\usr\bin\bash.exe" -l ($f -replace '\\', '/')
    if ($LASTEXITCODE -ne 0) { throw "MSYS2 작업 실패 (코드 $LASTEXITCODE)" }
}

# ---------- Python ----------
Step 'Python 3'
if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 --version
} else {
    Winget-Install 'Python.Python.3.12' @('--override', '/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1')
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Python 설치 후에도 py 없음. 새 PowerShell 창에서 다시 실행하세요.' }
}
py -3 -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('tkinter') else 1)"
if ($LASTEXITCODE -ne 0) { throw 'tkinter 없음. Python 재설치 시 "tcl/tk and IDLE" 체크.' }

# ---------- rkdeveloptool ----------
Step 'rkdeveloptool (1/1.5세대)'
$rk = Join-Path $tools 'rkdeveloptool.exe'
if ((Test-Path $rk) -and -not $Rebuild) {
    Write-Host "이미 있음: $rk (-Rebuild 로 다시 빌드)"
} else {
    if (-not (Test-Path "$msys\usr\bin\bash.exe")) { Winget-Install 'MSYS2.MSYS2' }
    if (-not (Test-Path "$msys\usr\bin\bash.exe")) { throw "MSYS2가 $msys 에 없음" }
    $env:TF_TOOLS = $tools
    Msys @'
set -e
pacman -Sy --needed --noconfirm git autoconf automake make \
    mingw-w64-x86_64-gcc mingw-w64-x86_64-libusb mingw-w64-x86_64-pkgconf
T=$(cygpath -u "$TF_TOOLS")
rm -rf /tmp/rkdeveloptool
git clone --depth 1 https://github.com/rockchip-linux/rkdeveloptool /tmp/rkdeveloptool
cd /tmp/rkdeveloptool
autoreconf -i
./configure
# -Wno-error: 최신 gcc/clang 경고가 에러로 빌드 실패하는 것 회피
make CXXFLAGS="-O2 -Wno-error"
cp rkdeveloptool.exe "$T/"
ldd rkdeveloptool.exe | awk '$3 ~ /mingw64/ {print $3}' | xargs -r cp -t "$T/"
'@
}
& $rk -v
if ($LASTEXITCODE -ne 0) { throw 'rkdeveloptool.exe 실행 실패 (DLL 누락?)' }

# ---------- edl (2세대) ----------
if ($Edl) {
    Step 'edl (2세대)'
    $src = Join-Path $tools 'edl-src'
    $git = "$msys\usr\bin\git.exe"
    if (-not (Test-Path $git)) {
        if (-not (Test-Path "$msys\usr\bin\bash.exe")) { Winget-Install 'MSYS2.MSYS2' }
        Msys 'pacman -Sy --needed --noconfirm git'
    }
    if (Test-Path $src) { Remove-Item -Recurse -Force $src }
    & $git clone --depth 1 --recurse-submodules --shallow-submodules https://github.com/bkerler/edl $src
    if ($LASTEXITCODE -ne 0) { throw 'edl clone 실패' }
    py -3 -m pip install -r (Join-Path $src 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'edl 의존성 설치 실패' }
    py -3 -m pip install $src
    if ($LASTEXITCODE -ne 0) { throw 'edl 설치 실패' }
    # pip Scripts 폴더가 PATH에 없으면 사용자 PATH에 추가
    if (-not (Get-Command edl -ErrorAction SilentlyContinue)) {
        $scripts = py -3 -c "import sysconfig; print(sysconfig.get_path('scripts'))"
        $user = [Environment]::GetEnvironmentVariable('Path', 'User')
        [Environment]::SetEnvironmentVariable('Path', "$user;$scripts", 'User')
        Refresh-Path
        Write-Host "PATH에 추가: $scripts"
    }
}

# ---------- Zadig (USB 드라이버) ----------
if (-not $SkipZadig) {
    Step 'Zadig (USB 드라이버)'
    $zadig = Join-Path $tools 'zadig.exe'
    if (-not (Test-Path $zadig)) {
        $rel = Invoke-RestMethod 'https://api.github.com/repos/pbatard/libwdi/releases/latest'
        $asset = $rel.assets | Where-Object name -like 'zadig-*.exe' | Select-Object -First 1
        if (-not $asset) { throw 'Zadig 다운로드 주소 못 찾음. https://zadig.akeo.ie 에서 직접 받으세요.' }
        Invoke-WebRequest $asset.browser_download_url -OutFile $zadig
    }
    Write-Host @'
기기를 Loader/MaskROM(2세대는 EDL) 모드로 연결한 상태에서:
  1. Options -> List All Devices 체크
  2. USB ID 2207 (Rockchip) 또는 05C6 9008 (QUSB__BULK) 장치 선택
  3. 드라이버 WinUSB 선택 -> Install/Replace Driver
  (모드마다 장치가 따로 잡히면 각각 설치. RKDevTool 드라이버는 WinUSB로 바뀜)
'@
    Start-Process $zadig
}

Step '완료'
Write-Host "실행: cd `"$root`"; py tf_flasher.py"
