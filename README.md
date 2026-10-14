# TF-Flasher

토스 프론트 **1 / 1.5세대**(RK3399, Rockchip)와 **2세대**(Qualcomm, EDL)에서 Toss 앱(TossPlace, TossManagement, TossplaceInstaller, TossplaceOTA)을 지운 펌웨어를 굽는 GUI.
디시 SFF갤 가이드 [#1](https://gall.dcinside.com/mgallery/board/view?id=sff&no=1716928) · [#2](https://gall.dcinside.com/mgallery/board/view?id=sff&no=1718687) · [#3](https://gall.dcinside.com/mgallery/board/view?id=sff&no=1718754) · [2세대 EDL](https://gall.dcinside.com/mgallery/board/view/?id=sff&no=1750496) 절차를 macOS/Linux/Windows에서 자동화한 것.
RKDevTool / RKDumper / lpunpack / WSL 필요 없음.

## 설치
```sh
# debugfs (ext4 system)
brew install e2fsprogs            # Linux: sudo apt install e2fsprogs
# rkdeveloptool (brew에 없음 → 소스 빌드)
brew install libusb automake autoconf pkg-config   # Linux: sudo apt install libusb-1.0-0-dev libudev-dev dh-autoreconf pkg-config
git clone https://github.com/rockchip-linux/rkdeveloptool && cd rkdeveloptool
autoreconf -i && ./configure && make CXXFLAGS="-O2 -Wno-error" && sudo cp rkdeveloptool /usr/local/bin/
# -Wno-error: 최신 clang에서 VLA 경고(-Wvla-cxx-extension)가 에러로 빌드 실패하는 것 회피
```
Linux는 udev 규칙 없으면 `sudo`로 실행.

2세대용 edl:
```sh
brew install libusb               # Linux: sudo apt install libusb-1.0-0
git clone --recurse-submodules https://github.com/bkerler/edl && cd edl
pip3 install -r requirements.txt && pip3 install .
```
firehose 로더(`prog_*.elf`/`.mbn`)는 기기에 맞는 걸 직접 구해야 함. 비워두면 edl 내장 로더 자동탐색.

### Windows
**자동 설치** (PowerShell):
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1        # 1/1.5세대
powershell -ExecutionPolicy Bypass -File install.ps1 -Edl   # 2세대 edl까지
```
winget으로 Python 3·MSYS2 설치 → rkdeveloptool 빌드해서 `tools\`에 → (edl) → Zadig 실행까지 함.
Zadig 창에서 드라이버 설치(아래 3번)만 직접 하면 됨.

**수동 설치**:
1. [Python 3](https://www.python.org/downloads/) 설치 (tkinter 포함, "Add to PATH" 체크).
2. GitHub Actions `windows` 워크플로 결과물(`TF-Flasher-windows`) 받아 압축 해제 → `tools\rkdeveloptool.exe` 포함.
   직접 빌드: MSYS2 MINGW64에서 `pacman -S git autoconf automake make mingw-w64-x86_64-{gcc,libusb,pkgconf}` 후 위와 같은 빌드, exe와 DLL을 `tools\`에 복사.
3. 드라이버: 기기를 Loader/MaskROM 모드로 연결 → [Zadig](https://zadig.akeo.ie)에서 Rockchip 장치(VID `2207`)에 **WinUSB** 설치.
   RKDevTool용 Rockusb 드라이버로는 rkdeveloptool(libusb)이 장치를 못 잡음. 모드마다 장치가 따로 잡히면 각각 설치.
4. 2세대: `pip install` 로 edl 설치 후 Zadig에서 `QUSB__BULK`(VID `05c6`, PID `9008`)에 WinUSB.
5. `py tf_flasher.py`

Windows엔 debugfs가 없어서 ext4 system은 삭제 대신 EROFS와 같은 **이름변경**(`*.apk`→`*.ap~`, 체크섬 재계산)으로 처리.
htree 디렉터리 등 지원 안 되는 형태면 중단됨 → WSL에서 Linux 절차로. Armbian **DTB 주입**은 debugfs가 필요해서 WSL 필요.
`tools\` 폴더에 둔 실행파일(rkdeveloptool, debugfs 등)은 PATH보다 먼저 쓰임.

## 사용
```sh
python3 tf_flasher.py
```
상단에서 세대 선택.

**1/1.5세대**
1. 리셋 버튼 누른 채 데이터 되는 5핀 케이블로 전원 연결 → Loader 모드.
   초기화 화면이 뜨는 기기면 케이스 열고 BOOT-GND 쇼트 후 연결 → MaskROM 모드 (이 때 `rk3399_loader_*.bin` 선택 필요).
2. **장치 확인 → 백업 → 패치 → 플래시** (또는 **전체 실행**).
3. 부팅 후 설정 → 공장초기화.
4. 부팅 안 되면 다시 모드 진입 후 **복구**.

**Armbian 설치 (1/1.5세대, 실험적)**
토스 프론트용 Armbian은 없어서 다른 RK3399 보드 이미지(Rock Pi 4, NanoPC-T4 등)를 쓰고 기기용 DTB를 직접 지정해야 함.
1. 먼저 **백업** (`_head` + super 포함 필수. 없으면 설치 거부).
2. Armbian 이미지(`.img` / `.img.xz`), DTB(`.dtb`, 선택) 지정 → **Armbian 설치**.
   eMMC를 LBA 0부터 통째로 덮어 Android는 지워짐. DTB를 지정하면 `/boot/dtb/rockchip/`에 넣고 `armbianEnv.txt`의 `fdtfile`을 바꿈.
   **DTB 추출**: 백업의 resource/boot 이미지에서 DTB를 뽑아 `work/dtb/`에 저장 (`dtc` 있으면 `.dts`도. `brew install dtc`).
   보통 `rk-kernel.dtb`가 기기 것. Android vendor 커널용이라 Armbian(메인라인)엔 그대로 안 맞을 수 있음 → DTS 작성 참고용.
3. DTB를 지정 안 하면 다른 보드 DTB로 부팅됨 → 화면/터치/네트워크 안 될 가능성 큼. 시리얼 콘솔(UART2, 1500000 baud) 권장.
4. 안 뜨거나 되돌리려면 BOOT-GND 쇼트 → MaskROM (로더 지정) → **복구**. GPT가 없어진 걸 감지하면 `_head`도 같이 복구함.
   userdata는 덮였으니 복구 후 공장초기화.

**2세대** (원글 #1750496)
1. 분해 후 USB-C 뽑은 상태로 내부 MicroUSB를 PC에 연결.
2. 1V8 + BOOT 테스트포인트를 **두꺼운** 핀셋으로 누른 채 USB-C 연결 → EDL(9008) 진입. 얇은 핀셋은 안 됨.
3. 로더 지정 후 **백업 → 패치 → 플래시**. 부팅 후 공장초기화.
4. 2세대는 system 수정 후 AVB/dm-verity 때문에 부팅 안 될 수 있음 (미검증). 그럼 **복구** 후
   "vbmeta verity 끄기" 체크하고 다시 패치/플래시. 부트로더 잠긴 기기면 이것도 안 먹음 (원글의 ABL 수정 필요, 미구현).

## system 파일시스템별 처리
| 종류 | 방법 |
|---|---|
| ext4 | `debugfs rm` 으로 APK 삭제 |
| ext4 + shared_blocks(dedup) | `debugfs unlink` (공유 블록 안 건드리고 엔트리만 제거) |
| ext4 (debugfs 없음, Windows) | 디렉터리 엔트리 `*.apk` → `*.ap~` 제자리 이름변경 + 디렉터리 블록 crc32c 재계산 |
| EROFS | 읽기전용이라 삭제 불가 → 디렉터리 엔트리 `*.apk` → `*.ap~` 제자리 이름변경. PackageManager가 무시함 |

이미지 크기·LP 메타데이터는 그대로, system extent만 덮어씀.

백업은 `backup/` (1세대 `*.img`, 2세대 `*.bin`), 플래시할 이미지는 `work/flash/<파티션>.img`.
테스트: `python3 test_lp.py` (e2fsprogs + erofs-utils 필요: `brew install erofs-utils`).

## 주의
- 2세대: 펌웨어 1.609.1 이후 OTA로 막혔다는 언급 있음. system이 ext4 shared_blocks/EROFS면 패치 단계에서 중단됨.
- 렌탈 기기는 토스 소유일 수 있음. 본인 소유 기기에만 사용.
- 펌웨어/백업 이미지 공유 금지 (`.gitignore`로 제외됨).
- 원글 작성자도 "정답이 아닐 수 있다"고 함. 실기기 테스트 안 된 도구, 책임은 본인.
