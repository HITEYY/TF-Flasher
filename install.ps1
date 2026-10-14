# TF-Flasher Windows 설치 스크립트
# 한 줄 설치: irm https://raw.githubusercontent.com/HITEYY/TF-Flasher/master/get.ps1 | iex
# 실행: powershell -ExecutionPolicy Bypass -File install.ps1 [-Edl] [-Rebuild] [-SkipZadig] [-NoLaunch]
#   -Edl       2세대(Qualcomm EDL)용 edl도 설치
#   -Rebuild   tools\rkdeveloptool.exe 있어도 다시 빌드
#   -SkipZadig Zadig(USB 드라이버) 받기/실행 생략
#   -NoLaunch  끝나고 TF-Flasher 실행 안 함
#   -Diag      설치 없이 USB 연결/드라이버 진단만 (한 줄: $env:TF_DIAG=1 후 irm ... | iex)
# 하는 일: Python 3 / MSYS2 설치(winget) -> rkdeveloptool 빌드 -> tools\ 에 exe+DLL -> (edl) -> Zadig
param([switch]$Edl, [switch]$Rebuild, [switch]$SkipZadig, [switch]$NoLaunch, [switch]$Diag)
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

function Get-Zadig {
    $zadig = Join-Path $tools 'zadig.exe'
    if (-not (Test-Path $zadig)) {
        $rel = Invoke-RestMethod 'https://api.github.com/repos/pbatard/libwdi/releases/latest'
        $asset = $rel.assets | Where-Object name -like 'zadig-*.exe' | Select-Object -First 1
        if (-not $asset) { throw 'Zadig 다운로드 주소 못 찾음. https://zadig.akeo.ie 에서 직접 받으세요.' }
        Invoke-WebRequest $asset.browser_download_url -OutFile $zadig
    }
    $zadig
}

$ZadigHelp = @'
Zadig에서:
  1. Options -> List All Devices 체크
  2. USB ID 2207 (Rockchip) 또는 05C6 9008 (QUSB__BULK) 장치 선택 (이름이 비어 있을 수 있음)
  3. 드라이버 WinUSB 선택 -> Install/Replace Driver
  4. 케이블 뽑았다 다시 꽂고 진단 다시 실행
  (Loader/MaskROM 모드가 다른 장치로 잡히면 각각 설치)
'@

# ---------- 진단 ----------
# 장치 하나 -> (판정, 해결법). Win32_PnPEntity: PNPDeviceID, Name, Service, ConfigManagerErrorCode
function Diagnose-Device($d) {
    $id, $svc, $err = $d.PNPDeviceID, "$($d.Service)", [int]$d.ConfigManagerErrorCode
    if ($id -match 'VID_0000&PID_0002' -or $err -eq 43) {
        return 'BAD', '장치 설명자 요청 실패 = 신호 문제 (드라이버 아님). USB 2.0 포트/USB 2.0 허브 경유, 짧은 데이터 케이블로 바꾸기'
    }
    $kind = if ($id -match 'VID_05C6&PID_9008') { 'EDL(2세대)' } elseif ($id -match 'VID_2207') { 'Rockchip(1/1.5세대)' } else { $null }
    if (-not $kind) { return 'OTHER', "문제 있는 USB 장치 (코드 $err)" }
    if ($svc -ieq 'WinUSB') {
        if ($err -eq 0) { return 'OK', "$kind + WinUSB 정상" }
        return 'DRIVER', "$kind WinUSB인데 오류 코드 $err. 뽑았다 다시 꽂기, 안 되면 Zadig로 재설치"
    }
    if (-not $svc -or $err -eq 28) { return 'DRIVER', "$kind 드라이버 없음 -> Zadig로 WinUSB 설치" }
    return 'DRIVER', "$kind 가 '$svc' 드라이버로 잡힘 (RKDevTool/Qualcomm 드라이버 등). libusb용 아님 -> Zadig로 WinUSB로 Replace"
}

function Find-Devices {
    Get-CimInstance Win32_PnPEntity | Where-Object {
        $_.PNPDeviceID -match '^USB\\VID_(2207|05C6&PID_9008|0000&PID_0002)' -or
        ($_.PNPDeviceID -like 'USB\*' -and $_.ConfigManagerErrorCode -eq 43)
    }
}

function Run-Diag {
    Step 'USB 진단'
    $devs = @(Find-Devices)
    if (-not $devs) {
        Write-Host '대상 장치 없음. 지금 기기를 Loader/MaskROM(2세대는 EDL) 모드로 연결하세요. 30초 대기...'
        foreach ($i in 1..15) {
            Start-Sleep 2
            $devs = @(Find-Devices)
            if ($devs) { break }
        }
    }
    if (-not $devs) {
        Write-Host @'
30초 동안 아무 장치도 안 잡힘 = 연결 자체가 안 됨 (드라이버 문제 아님).
  - 충전 전용 케이블 아닌지 (가장 흔함). 파일 전송 되던 케이블로
  - 허브/동글 빼고 본체 포트에 직접, 가능하면 USB 2.0 포트
  - 모드 진입 확인: 화면 꺼지고 LED만 켜져야 함. 안 되면 BOOT-GND 쇼트(MaskROM)
  - 2세대는 외부 USB-C 아니라 내부 MicroUSB
'@ -ForegroundColor Yellow
        return
    }
    $needZadig = $false
    foreach ($d in $devs) {
        $state, $msg = Diagnose-Device $d
        $color = @{ OK = 'Green'; DRIVER = 'Yellow' }[$state]
        if (-not $color) { $color = 'Red' }
        Write-Host ("[{0}] {1}  ({2}, 드라이버: {3})" -f $state, $d.Name, $d.PNPDeviceID, $(if ($d.Service) { $d.Service } else { '없음' }))
        Write-Host "  -> $msg" -ForegroundColor $color
        if ($state -eq 'DRIVER') { $needZadig = $true }
        if ($state -eq 'OK' -and $d.PNPDeviceID -match 'VID_2207') {
            $rk = Join-Path $tools 'rkdeveloptool.exe'
            if (Test-Path $rk) { & $rk ld } else { Write-Host '  tools\rkdeveloptool.exe 없음 -> install.ps1 먼저' }
        }
    }
    if ($needZadig) {
        Write-Host $ZadigHelp
        Start-Process (Get-Zadig)
    }
}

if ($Diag) {
    Run-Diag
    return
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
    Write-Host "기기를 Loader/MaskROM(2세대는 EDL) 모드로 연결한 상태에서 $ZadigHelp"
    Start-Process (Get-Zadig)
}

Step '완료'
Write-Host "다음부터 실행: cd `"$root`"; py tf_flasher.py  (또는 한 줄 설치 명령 다시 실행 = 업데이트+실행)"
if (-not $NoLaunch) {
    Set-Location $root
    py -3 (Join-Path $root 'tf_flasher.py')
}
