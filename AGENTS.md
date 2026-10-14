# TF-Flasher — AI 에이전트용 CLI 사용법

토스 프론트(1/1.5세대 RK3399, 2세대 Qualcomm EDL)에서 Toss 앱을 지운 펌웨어를 굽는 도구.
에이전트는 **항상 CLI로** 실행. 인자 없이 실행하면 GUI가 떠서 멈춤.

```sh
python3 tf_flasher.py <action> --non-interactive [옵션]
```
Windows는 `py tf_flasher.py ...`. 작업 디렉터리 무관 (경로는 스크립트 위치 기준).

## 동작

| action | 하는 일 | 기기 필요 | 확인(`-y`) 필요 |
|---|---|---|---|
| `check` | 장치/모드 확인. MaskROM이면 `--loader` 다운로드 | O | |
| `backup` | 전 파티션 → `backup/` (+`partitions.txt`) | O | |
| `patch` | `backup/super.*`에서 Toss APK 제거 → `work/flash/*.img` | X | |
| `flash` | `work/flash/*.img`를 기기에 쓰고 재부팅 | O | O |
| `restore` | `backup/`으로 전부 되돌리고 재부팅 | O | O |
| `all` | backup → patch → flash | O | O |
| `dtb` | `backup/`의 resource/boot에서 DTB 추출 → `work/dtb/` | X | |
| `armbian` | eMMC 전체를 Armbian 이미지로 덮음 (1/1.5세대만) | O | O |

## 옵션

| 옵션 | 의미 |
|---|---|
| `--non-interactive` | 질문 안 함. 에이전트는 항상 붙일 것 |
| `-y`, `--yes` | 확인 필요한 동작 승인. **사용자가 명시적으로 승인했을 때만** |
| `--edl` | 2세대(Qualcomm EDL). 없으면 1/1.5세대(Rockchip) |
| `--loader PATH` | 1/1.5세대: MaskROM일 때만 `rk3399_loader_*.bin` 필요. 2세대: firehose `.elf/.mbn` (비우면 edl 자동탐색) |
| `--userdata` | userdata까지 백업 (느림) |
| `--verity-off` | `patch` 시 vbmeta verity 끔 (2세대 부팅 안 될 때. 언락 기기만 효과) |
| `--image PATH` | `armbian`: `.img` / `.img.xz` |
| `--dtb PATH` | `armbian`: 기기용 DTB (선택, 없으면 화면/터치 안 될 가능성 큼) |

## 종료코드 / 출력

- `0` 성공 (stdout 마지막 줄 `✔ 성공`)
- `1` 실패 (stderr에 `✘ <이유>`) — 이유를 읽고 사용자에게 전달
- `2` `--non-interactive`인데 `-y` 없음 → 사용자에게 승인 받고 `-y` 붙여 재실행

stdout: 실행한 외부 명령은 `$ ` 로 시작, 나머지는 진행 로그. 줄 단위로 바로 출력됨.

## 에이전트 규칙

1. **기기 모드 진입은 사람이 함.** 에이전트는 못 함. 기기 필요한 동작 전에 사용자에게 요청하고 `check`로 확인.
   - 1/1.5세대: 리셋 버튼 누른 채 데이터 5핀 케이블로 전원 연결(Loader). 초기화 화면이 뜨면 BOOT-GND 쇼트 후 연결(MaskROM, `--loader` 필요).
   - 2세대: USB-C 뺀 상태로 내부 MicroUSB 연결 → 1V8+BOOT를 두꺼운 핀셋으로 누른 채 USB-C 연결(EDL 9008).
2. **`-y`는 사용자 승인 후에만.** `flash`/`restore`/`all`/`armbian`은 eMMC를 덮어씀.
3. **이미 패치한 기기에서 `backup`/`all` 다시 실행 금지.** `backup/`이 패치본으로 덮여 원본 복구 불가. 실행 전 `backup/`이 있으면 사용자에게 확인, 필요하면 다른 곳에 복사.
4. 동작 사이마다 같은 `--edl` / `--loader`를 넘길 것 (상태 저장 안 함). 1/1.5세대 MaskROM이면 매 동작마다 `--loader` 필요.
5. `armbian`은 `backup/`에 `_head`와 `super`가 있어야 함. 없으면 거부됨.

## 표준 절차

1/1.5세대 (Loader 모드):
```sh
python3 tf_flasher.py check --non-interactive
python3 tf_flasher.py backup --non-interactive
python3 tf_flasher.py patch --non-interactive
python3 tf_flasher.py flash --non-interactive -y    # 사용자 승인 후
```
→ 부팅 후 사용자가 설정 → 공장초기화. 부팅 안 되면 다시 모드 진입 → `restore -y`.

2세대:
```sh
python3 tf_flasher.py backup --edl --loader prog.elf --non-interactive
python3 tf_flasher.py patch --non-interactive
python3 tf_flasher.py flash --edl --loader prog.elf --non-interactive -y
# 부팅 안 되면: restore → patch --verity-off → flash
```

## 자주 나는 실패

| stderr | 조치 |
|---|---|
| `rkdeveloptool 를 찾을 수 없음` / `edl ...` | 도구 미설치. README 설치 절 안내 |
| `Rockusb 장치 없음` | 모드 진입 안 됨. 사용자에게 재시도 요청 |
| `MaskROM 모드: 로더 파일 ... 필요` | `--loader` 지정 |
| `backup/super.img(.bin) 없음` / `백업 먼저 하세요` | `backup` 먼저 |
| `지울 Toss APK가 하나도 없음` | 이미 패치됐거나 다른 펌웨어. 백업이 패치본인지 확인 |
| `debugfs 없음` | macOS `brew install e2fsprogs`, Linux `apt install e2fsprogs`. Windows ext4는 debugfs 없이 처리 |
| `... 크기가 백업과 다름` | 다른 기기 백업이거나 손상. 진행 중단, 사용자에게 보고 |
