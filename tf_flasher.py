#!/usr/bin/env python3
"""TF-Flasher: 토스 프론트에서 Toss 앱을 지운 펌웨어를 굽는 도구.

참고: 디시 SFF갤 #1716928, #1718687, #1718754 (1/1.5세대), #1750496 (2세대 EDL)
절차: 장치 확인 -> 전 파티션 백업 -> super 내 system에서 APK 삭제 -> super 플래시
필요: 1/1.5세대 rkdeveloptool, 2세대 edl(bkerler/edl) + firehose 로더, 공통 e2fsprogs(debugfs)
"""
import os
import queue
import re
import shutil
import struct
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKUP = ROOT / "backup"
WORK = ROOT / "work"
TABLE = BACKUP / "partitions.txt"
FLASH = WORK / "flash"

TOSS_APKS = [
    "/system/app/TossPlace/TossPlace.apk",
    "/system/priv-app/TossManagement/TossManagement.apk",
    "/system/priv-app/TossplaceInstaller/TossplaceInstaller.apk",
    "/system/priv-app/TossplaceOTA/TossplaceOTA.apk",
]

log = print  # GUI가 교체


class Fail(Exception):
    pass


# ---------- 외부 도구 ----------

def run(cmd, quiet=False):
    if not quiet:
        log("$ " + " ".join(map(str, cmd)))
    try:
        p = subprocess.run(list(map(str, cmd)), capture_output=True, text=True)
    except FileNotFoundError:
        raise Fail(f"{cmd[0]} 를 찾을 수 없음. README의 설치 방법 참고.")
    out = (p.stdout + p.stderr).strip()
    if out and not quiet:
        log(out)
    return p.returncode, out


def rk(*args):
    code, out = run(["rkdeveloptool", *args])
    if code != 0 or "fail" in out.lower():
        raise Fail(f"rkdeveloptool {args[0]} 실패")
    return out


def find_debugfs():
    for p in [shutil.which("debugfs"),
              "/opt/homebrew/opt/e2fsprogs/sbin/debugfs",
              "/usr/local/opt/e2fsprogs/sbin/debugfs",
              "/sbin/debugfs", "/usr/sbin/debugfs"]:
        if p and os.path.exists(p):
            return p
    raise Fail("debugfs 없음. macOS: brew install e2fsprogs / Linux: apt install e2fsprogs")


# ---------- 1. 장치 확인 ----------

def check_device(loader=None):
    out = rk("ld")
    m = re.search(r"Mode=(\w+)", out)
    if not m:
        raise Fail("Rockusb 장치 없음. 리셋 버튼 누른 채 전원 연결(안되면 BOOT-GND 쇼트)")
    mode = m.group(1)
    log(f"모드: {mode}")
    if mode.lower() == "maskrom":
        if not loader:
            raise Fail("MaskROM 모드: 로더 파일(rk3399_loader_*.bin) 선택 필요")
        rk("db", loader)
        log("로더 다운로드 완료")
    return mode


# ---------- 2. 백업 ----------

def parse_ppt(out):
    """rkdeveloptool ppt 출력 -> [(name, start_lba)]"""
    rows = re.findall(r"^\s*\d+\s+([0-9a-fA-F]+)\s+(\S+)\s*$", out, re.M)
    return [(name, int(lba, 16)) for lba, name in rows]


def flash_sectors():
    out = rk("rfi")
    m = re.search(r"Flash Size:\s*(\d+)\s*Sectors", out)
    if not m:
        raise Fail("플래시 크기 확인 실패")
    return int(m.group(1))


def backup(include_userdata=False):
    parts = parse_ppt(rk("ppt"))
    if not any(n == "super" for n, _ in parts):
        raise Fail("super 파티션 없음 - 1/1.5세대 아닐 수 있음")
    total = flash_sectors()
    BACKUP.mkdir(exist_ok=True)
    table = []
    for i, (name, start) in enumerate(parts):
        end = parts[i + 1][1] if i + 1 < len(parts) else total
        if i + 1 == len(parts) and not include_userdata:
            log(f"{name} 스킵 (마지막/grow 파티션)")
            continue
        table.append((name, start, end - start))
    # 파티션 테이블/로더 영역. 보관만 하고 복구 시엔 안 씀.
    table.insert(0, ("_head", 0, parts[0][1]))
    for name, start, count in table:
        log(f"백업 {name}: LBA {start:#x}, {count} sectors ({count * 512 >> 20} MiB)")
        rk("rl", start, count, BACKUP / f"{name}.img")
        if (BACKUP / f"{name}.img").stat().st_size != count * 512:
            raise Fail(f"{name} 백업 크기 불일치")
    TABLE.write_text("".join(f"{n} {s} {c}\n" for n, s, c in table))
    log(f"백업 완료 -> {BACKUP}")


def read_table():
    if not TABLE.exists():
        raise Fail("백업 먼저 하세요")
    return [(n, int(s), int(c)) for n, s, c in
            (l.split() for l in TABLE.read_text().splitlines() if l.strip())]


# ---------- 3. 패치 ----------

LP_GEOMETRY_MAGIC = 0x616C4467
LP_HEADER_MAGIC = 0x414C5030
LP_METADATA_OFFSET = 4096 + 4096 * 2  # reserved + geometry x2


def parse_lp(path, want=("system", "system_a", "system_b")):
    """super 이미지 LP 메타데이터 -> {파티션명: [(byte_off, byte_len)]} (extent 있는 system* 만)"""
    with open(path, "rb") as f:
        f.seek(4096)
        if struct.unpack("<I", f.read(4))[0] != LP_GEOMETRY_MAGIC:
            raise Fail("super.img LP geometry 아님 (raw super 이미지 맞나?)")
        f.seek(LP_METADATA_OFFSET)
        hdr = f.read(256)
    magic, _maj, _min, header_size = struct.unpack_from("<IHHI", hdr, 0)
    if magic != LP_HEADER_MAGIC:
        raise Fail("LP metadata header magic 불일치")
    # magic,major,minor,header_size,checksum[32],tables_size,checksum[32] = 80 bytes
    p_off, p_n, p_sz, e_off, e_n, e_sz = struct.unpack_from("<6I", hdr, 80)
    with open(path, "rb") as f:
        base = LP_METADATA_OFFSET + header_size
        f.seek(base + p_off)
        parts = f.read(p_n * p_sz)
        f.seek(base + e_off)
        exts = f.read(e_n * e_sz)
    found = {}
    for i in range(p_n):
        name, _attr, first, num, _grp = struct.unpack_from("<36s4I", parts, i * p_sz)
        name = name.rstrip(b"\0").decode()
        if name not in want or not num:
            continue
        found[name] = []
        for j in range(first, first + num):
            sectors, ttype, tdata, _src = struct.unpack_from("<QIQI", exts, j * e_sz)
            if ttype != 0:
                raise Fail(f"{name} extent가 linear 아님")
            found[name].append((tdata * 512, sectors * 512))
    if not found:
        raise Fail("super 안에 system 파티션 없음")
    return found


def fs_type(img):
    with open(img, "rb") as f:
        sb = f.read(2048)[1024:]
    if struct.unpack_from("<I", sb, 0)[0] == EROFS_MAGIC:
        return "erofs"
    if struct.unpack_from("<H", sb, 0x38)[0] != 0xEF53:
        raise Fail("system이 ext4/EROFS 아님")
    # shared_blocks(dedup) 면 rm이 공유 블록을 해제하므로 디렉터리 엔트리만 뗀다
    return "ext4-shared" if struct.unpack_from("<I", sb, 0x64)[0] & 0x4000 else "ext4"


def ext4_remove(img, path, shared):
    debugfs = find_debugfs()
    stat = lambda: "Inode:" in run([debugfs, "-R", f"stat {path}", img], quiet=True)[1]
    if not stat():
        return False
    run([debugfs, "-w", "-R", f"{'unlink' if shared else 'rm'} {path}", img])
    if stat():
        raise Fail(f"{path} 삭제 실패")
    return True


# EROFS는 읽기전용 FS라 지울 수 없음 -> 디렉터리 엔트리 이름 끝 'k'를 '~'로 바꿔
# PackageManager가 *.apk 로 인식 못 하게 함 (같은 길이라 제자리 수정 가능).
EROFS_MAGIC = 0xE0F5E1E2


def erofs_hide(img, path):
    with open(img, "r+b") as f:
        def rd(off, n):
            f.seek(off)
            return f.read(n)

        sb = rd(1024, 128)
        bs = 1 << sb[12]
        root_nid, = struct.unpack_from("<H", sb, 14)
        meta, = struct.unpack_from("<I", sb, 40)

        def dir_entries(nid):
            off = meta * bs + nid * 32
            ino = rd(off, 64)
            fmt, icount = struct.unpack_from("<HH", ino, 0)
            ext, layout = fmt & 1, (fmt >> 1) & 7
            size = struct.unpack_from("<Q" if ext else "<I", ino, 8)[0]
            raw, = struct.unpack_from("<I", ino, 16)
            inline = off + (64 if ext else 32) + (12 + (icount - 1) * 4 if icount else 0)
            if layout not in (0, 2):
                raise Fail("EROFS 디렉터리 레이아웃 미지원")
            out = []
            for i in range(-(-size // bs)):
                ln = min(bs, size - i * bs)
                boff = inline if layout == 2 and i == size // bs else raw * bs + i * bs
                blk = rd(boff, ln)
                n = struct.unpack_from("<H", blk, 8)[0] // 12
                for k in range(n):
                    cnid, nameoff = struct.unpack_from("<QH", blk, k * 12)
                    end = struct.unpack_from("<H", blk, k * 12 + 20)[0] if k + 1 < n else ln
                    name = blk[nameoff:end].split(b"\0")[0]
                    out.append((name, cnid, boff + nameoff))
            return out

        nid, parts = root_nid, path.strip("/").encode().split(b"/")
        for comp in parts[:-1]:
            nid = next((c for nm, c, _ in dir_entries(nid) if nm == comp), None)
            if nid is None:
                return False
        entries = dir_entries(nid)
        hit = next((e for e in entries if e[0] == parts[-1]), None)
        if not hit:
            return False
        new = parts[-1][:-1] + b"~"
        names = [new if e is hit else e[0] for e in entries]
        if names != sorted(names):
            raise Fail(f"{path}: 이름 바꾸면 정렬 깨짐")
        f.seek(hit[2])
        f.write(new)
    return True


def strip_apks(sysimg):
    fs = fs_type(sysimg)
    log(f"system 파일시스템: {fs}")
    removed = 0
    for apk in TOSS_APKS:
        # system-as-root 이미지면 /system/..., 아니면 / 기준
        for path in (apk, apk[len("/system"):]):
            ok = erofs_hide(sysimg, path) if fs == "erofs" else \
                ext4_remove(sysimg, path, fs == "ext4-shared")
            if ok:
                log(f"제거됨: {path}")
                removed += 1
                break
        else:
            log(f"경고: {apk} 없음")
    return removed


def disable_verity(src, dst):
    """vbmeta 헤더 flags(offset 120, BE)에 HASHTREE_DISABLED|VERIFICATION_DISABLED"""
    data = bytearray(Path(src).read_bytes())
    if data[:4] != b"AVB0":
        raise Fail(f"{src} vbmeta 아님")
    flags, = struct.unpack_from(">I", data, 120)
    struct.pack_into(">I", data, 120, flags | 3)
    Path(dst).write_bytes(data)


def find_super():
    # Rockchip 백업은 super.img, edl 백업은 super.bin
    found = [BACKUP / n for n in ("super.img", "super.bin") if (BACKUP / n).exists()]
    if not found:
        raise Fail("backup/super.img(.bin) 없음. 백업 먼저")
    return max(found, key=lambda f: f.stat().st_mtime)


def patch(super_in=None, out_dir=FLASH, verity_off=False):
    """out_dir/<파티션>.img 로 플래시할 이미지 생성 (super + 선택적으로 vbmeta*)"""
    super_in = Path(super_in or find_super())
    out_dir = Path(out_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    super_out = out_dir / "super.img"
    slots = parse_lp(super_in)

    log("super 복사 중...")
    shutil.copyfile(super_in, super_out)
    sysimg = out_dir.parent / "system.img"
    removed = 0
    for name, extents in slots.items():
        log(f"{name} extents: {[(hex(o), l) for o, l in extents]}")
        with open(super_out, "rb") as src, open(sysimg, "wb") as dst:
            for off, length in extents:
                src.seek(off)
                dst.write(src.read(length))
        removed += strip_apks(sysimg)
        if sysimg.stat().st_size != sum(l for _, l in extents):
            raise Fail("system.img 크기 변함")
        with open(sysimg, "rb") as src, open(super_out, "r+b") as dst:
            for off, length in extents:
                dst.seek(off)
                dst.write(src.read(length))
    sysimg.unlink()
    if not removed:
        shutil.rmtree(out_dir)
        raise Fail("지울 Toss APK가 하나도 없음 - 이미 패치됐거나 다른 펌웨어")

    if verity_off:
        vbs = sorted(super_in.parent.glob(f"vbmeta*{super_in.suffix}"))
        if not vbs:
            raise Fail("백업에 vbmeta 없음")
        for vb in vbs:
            disable_verity(vb, out_dir / f"{vb.stem}.img")
            log(f"verity 끔: {vb.stem}")
    log(f"패치 완료 ({removed}개 제거) -> {out_dir}")
    return removed


def flash_items():
    items = sorted(FLASH.glob("*.img"))
    if not any(i.stem == "super" for i in items):
        raise Fail("패치 먼저 하세요")
    for img in items:
        orig = [BACKUP / f"{img.stem}{e}" for e in (".img", ".bin") if (BACKUP / f"{img.stem}{e}").exists()]
        if not orig or orig[0].stat().st_size != img.stat().st_size:
            raise Fail(f"{img.name} 크기가 백업과 다름")
    return items


# ---------- 4. 플래시 / 5. 복구 ----------

def flash():
    table = {n: s for n, s, _ in read_table()}
    for img in flash_items():
        log(f"{img.stem} 플래시 중... (케이블 건드리지 말 것)")
        rk("wl", table[img.stem], img)
    rk("rd")
    log("완료. 부팅 후 설정 -> 공장초기화 하세요.")


def restore():
    for name, start, count in read_table():
        if name.startswith("_"):
            continue
        img = BACKUP / f"{name}.img"
        if img.stat().st_size != count * 512:
            raise Fail(f"{img} 크기 이상")
        log(f"복구 {name}")
        rk("wl", start, img)
    rk("rd")
    log("복구 완료")


# ---------- 2세대: Qualcomm EDL (bkerler/edl) ----------
# 진입: 내부 MicroUSB 연결 → 1V8+BOOT 두꺼운 핀셋으로 누른 채 USB-C 연결 (USB-C 전원 먼저 꽂지 말 것)

def edl(loader, *args):
    cmd = ["edl", *args] + ([f"--loader={loader}"] if loader else [])
    code, out = run(cmd)
    if code != 0 or re.search(r"\berror\b", out, re.I):
        raise Fail(f"edl {args[0]} 실패 (EDL 모드/로더 확인)")
    return out


def edl_check(loader):
    out = edl(loader, "printgpt")
    if "super" not in out:
        raise Fail("GPT에 super 없음")
    return out


def edl_backup(loader, include_userdata=False):
    BACKUP.mkdir(exist_ok=True)
    (BACKUP / "gpt.txt").write_text(edl_check(loader))
    edl(loader, "rl", BACKUP, "--genxml", *([] if include_userdata else ["--skip=userdata"]))
    if not (BACKUP / "super.bin").exists():
        raise Fail("super.bin 백업 안 됨")
    log(f"백업 완료 -> {BACKUP}")


def edl_flash(loader):
    for img in flash_items():
        log(f"{img.stem} 플래시 중... (케이블 건드리지 말 것)")
        edl(loader, "w", img.stem, img)
    edl(loader, "reset")
    log("완료. 부팅 후 설정 -> 공장초기화 하세요.")


def edl_restore(loader):
    edl(loader, "wl", BACKUP, "--skip=userdata")
    edl(loader, "reset")
    log("복구 완료")


# ---------- GUI ----------

def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    global log
    q = queue.Queue()
    log = q.put

    root = tk.Tk()
    root.title("TF-Flasher (토스 프론트)")
    hints = {
        "rk": "1/1.5세대: 리셋 버튼 누른 채 USB(데이터 5핀) 연결.\n"
              "초기화 화면이 뜨면 케이스 열고 BOOT-GND 쇼트 후 다시 시도.\n"
              "로더는 MaskROM 모드일 때만 필요 (rk3399_loader_*.bin). 반드시 백업 먼저.",
        "edl": "2세대: USB-C 빼고 내부 MicroUSB 연결 → 1V8+BOOT를 두꺼운 핀셋으로 누른 채 USB-C 연결.\n"
               "firehose 로더(.elf/.mbn) 지정 (비우면 edl 자동탐색). 반드시 백업 먼저.\n"
               "주의: 2세대 system 수정 후 부팅(AVB/verity)은 미검증. 안 되면 복구.",
    }
    gen = tk.StringVar(value="rk")
    sel = tk.Frame(root)
    sel.pack(anchor="w", padx=8, pady=(6, 0))
    hint = tk.Label(root, justify="left", fg="#b00", text=hints["rk"])
    for value, text in (("rk", "1/1.5세대 (Rockchip)"), ("edl", "2세대 (Qualcomm EDL)")):
        tk.Radiobutton(sel, text=text, value=value, variable=gen,
                       command=lambda: hint.config(text=hints[gen.get()])).pack(side="left")
    hint.pack(anchor="w", padx=8, pady=4)

    loader = tk.StringVar()
    userdata = tk.BooleanVar()
    row = tk.Frame(root)
    row.pack(fill="x", padx=8)
    tk.Label(row, text="로더:").pack(side="left")
    tk.Entry(row, textvariable=loader, width=40).pack(side="left", fill="x", expand=True)
    tk.Button(row, text="찾기", command=lambda: loader.set(
        filedialog.askopenfilename() or loader.get())).pack(side="left")
    verity = tk.BooleanVar()
    tk.Checkbutton(root, text="userdata도 백업 (느림)", variable=userdata).pack(anchor="w", padx=8)
    tk.Checkbutton(root, text="vbmeta verity 끄기 (부팅 안 될 때. 부트로더 언락 기기만 효과)",
                   variable=verity).pack(anchor="w", padx=8)

    btns = tk.Frame(root)
    btns.pack(fill="x", padx=8, pady=4)
    out = scrolledtext.ScrolledText(root, width=90, height=25)
    out.pack(fill="both", expand=True, padx=8, pady=4)

    buttons = []
    worker = [None]

    def start(fn, confirm=None):
        if worker[0] and worker[0].is_alive():
            return
        if confirm and not messagebox.askyesno("확인", confirm):
            return

        def job():
            try:
                fn()
                log("✔ 성공")
            except Fail as e:
                log(f"✘ {e}")
            except Exception as e:
                log(f"✘ 예외: {e!r}")
        for b in buttons:
            b.config(state="disabled")
        worker[0] = threading.Thread(target=job, daemon=True)
        worker[0].start()

    def poll():
        while not q.empty():
            out.insert("end", q.get() + "\n")
            out.see("end")
        if worker[0] and not worker[0].is_alive():
            for b in buttons:
                b.config(state="normal")
            worker[0] = None
        root.after(100, poll)

    def do_check():
        if gen.get() == "edl":
            edl_check(loader.get())
        else:
            check_device(loader.get())

    def do_backup():
        if gen.get() == "edl":
            edl_backup(loader.get(), userdata.get())
        else:
            check_device(loader.get())
            backup(userdata.get())

    def do_flash():
        if gen.get() == "edl":
            edl_flash(loader.get())
        else:
            check_device(loader.get())
            flash()

    def do_restore():
        if gen.get() == "edl":
            edl_restore(loader.get())
        else:
            check_device(loader.get())
            restore()

    def do_patch():
        patch(verity_off=verity.get())

    def everything():
        do_backup()
        do_patch()
        do_flash()

    actions = [
        ("장치 확인", do_check, None),
        ("백업", do_backup, None),
        ("패치", do_patch, None),
        ("플래시", do_flash, "패치된 super를 기기에 씁니다. 계속?"),
        ("복구", do_restore, "백업 이미지로 전부 되돌립니다. 계속?"),
        ("전체 실행", everything,
         "백업 -> 패치 -> 플래시 순서로 진행합니다. 계속?"),
    ]
    for text, fn, confirm in actions:
        b = tk.Button(btns, text=text, command=lambda f=fn, c=confirm: start(f, c))
        b.pack(side="left", padx=2)
        buttons.append(b)

    poll()
    root.mainloop()


if __name__ == "__main__":
    gui()
