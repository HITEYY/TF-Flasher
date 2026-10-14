# TF-Flasher

토스 프론트 **1 / 1.5세대**(RK3399, Rockchip)와 **2세대**(Qualcomm, EDL)에서 Toss 앱(TossPlace, TossManagement, TossplaceInstaller, TossplaceOTA)을 지운 펌웨어를 굽는 GUI.
디시 SFF갤 가이드 [#1](https://gall.dcinside.com/mgallery/board/view?id=sff&no=1716928) · [#2](https://gall.dcinside.com/mgallery/board/view?id=sff&no=1718687) · [#3](https://gall.dcinside.com/mgallery/board/view?id=sff&no=1718754) · [2세대 EDL](https://gall.dcinside.com/mgallery/board/view/?id=sff&no=1750496) 절차를 macOS/Linux에서 자동화한 것.
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
| EROFS | 읽기전용이라 삭제 불가 → 디렉터리 엔트리 `*.apk` → `*.ap~` 제자리 이름변경. PackageManager가 무시함 |

이미지 크기·LP 메타데이터는 그대로, system extent만 덮어씀.

백업은 `backup/` (1세대 `*.img`, 2세대 `*.bin`), 플래시할 이미지는 `work/flash/<파티션>.img`.
테스트: `python3 test_lp.py` (e2fsprogs + erofs-utils 필요: `brew install erofs-utils`).

## 주의
- 2세대: 펌웨어 1.609.1 이후 OTA로 막혔다는 언급 있음. system이 ext4 shared_blocks/EROFS면 패치 단계에서 중단됨.
- 렌탈 기기는 토스 소유일 수 있음. 본인 소유 기기에만 사용.
- 펌웨어/백업 이미지 공유 금지 (`.gitignore`로 제외됨).
- 원글 작성자도 "정답이 아닐 수 있다"고 함. 실기기 테스트 안 된 도구, 책임은 본인.
