#!/usr/bin/env python3
"""TF-Flasher: 토스 프론트에서 Toss 앱을 지운 펌웨어를 굽는 도구.

참고: 디시 SFF갤 #1716928, #1718687, #1718754 (1/1.5세대), #1750496 (2세대 EDL)
절차: 장치 확인 -> 전 파티션 백업 -> super 내 system에서 APK 삭제 -> super 플래시
필요: 1/1.5세대 rkdeveloptool, 2세대 edl(bkerler/edl) + firehose 로더, 공통 e2fsprogs(debugfs)
"""
import lzma
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
TOOLS = ROOT / "tools"  # rkdeveloptool(.exe) 등을 여기 두면 PATH 없이 사용 (Windows용)
DTB_OUT = WORK / "dtb"

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
    cmd = list(map(str, cmd))
    cmd[0] = shutil.which(cmd[0], path=f"{TOOLS}{os.pathsep}{os.environ.get('PATH', '')}") or cmd[0]
    if not quiet:
        log("$ " + " ".join(cmd))
    try:
        # Windows: 한글 로케일(cp949) 대신 utf-8, GUI에서 콘솔창 안 뜨게
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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


def find_debugfs(required=True):
    for p in [shutil.which("debugfs", path=f"{TOOLS}{os.pathsep}{os.environ.get('PATH', '')}"),
              "/opt/homebrew/opt/e2fsprogs/sbin/debugfs",
              "/usr/local/opt/e2fsprogs/sbin/debugfs",
              "/sbin/debugfs", "/usr/sbin/debugfs"]:
        if p and os.path.exists(p):
            return p
    if required:
        raise Fail("debugfs 없음. macOS: brew install e2fsprogs / Linux: apt install e2fsprogs / "
                   "Windows: WSL에서 실행")


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


# debugfs 없을 때(Windows) ext4도 EROFS처럼 *.apk -> *.ap~ 제자리 이름변경.
# metadata_csum 이면 디렉터리 블록 체크섬(crc32c) 재계산.
CRC32C = []
for _n in range(256):
    for _ in range(8):
        _n = (_n >> 1) ^ (0x82F63B78 if _n & 1 else 0)
    CRC32C.append(_n)


def crc32c(crc, data):
    for b in data:
        crc = CRC32C[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc


def ext4_hide(img, path):
    with open(img, "r+b") as f:
        def rd(off, n):
            f.seek(off)
            return f.read(n)

        sb = rd(1024, 1024)
        bs = 1024 << struct.unpack_from("<I", sb, 0x18)[0]
        first, = struct.unpack_from("<I", sb, 0x14)
        ipg, = struct.unpack_from("<I", sb, 0x28)
        isz, = struct.unpack_from("<H", sb, 0x58)
        incompat, ro = struct.unpack_from("<II", sb, 0x60)
        dsz = struct.unpack_from("<H", sb, 0xFE)[0] if incompat & 0x80 else 32
        csum = ro & 0x400
        seed = struct.unpack_from("<I", sb, 0x270)[0] if incompat & 0x2000 else \
            crc32c(0xFFFFFFFF, sb[0x68:0x78])

        def inode(n):
            g, i = divmod(n - 1, ipg)
            gd = rd((first + 1) * bs + g * dsz, dsz)
            tbl = struct.unpack_from("<I", gd, 8)[0]
            if dsz >= 64:
                tbl |= struct.unpack_from("<I", gd, 0x28)[0] << 32
            return rd(tbl * bs + i * isz, isz)

        def blocks(ino):
            flags, = struct.unpack_from("<I", ino, 0x20)
            if flags & 0x10000000 or not flags & 0x80000:
                raise Fail("ext4 디렉터리 형식 미지원 (inline/blockmap) - debugfs 필요")
            out = []

            def walk(node):
                magic, n, _max, depth = struct.unpack_from("<4H", node, 0)
                if magic != 0xF30A:
                    raise Fail("ext4 extent 헤더 이상")
                for k in range(12, 12 + n * 12, 12):
                    if depth:
                        lo, hi = struct.unpack_from("<IH", node, k + 4)
                        walk(rd((hi << 32 | lo) * bs, bs))
                    else:
                        _l, ln, hi, lo = struct.unpack_from("<IHHI", node, k)
                        out.extend(range(hi << 32 | lo, (hi << 32 | lo) + (ln & 0x7FFF)))
            walk(ino[0x28:0x28 + 60])
            return out

        def entries(n):
            ino = inode(n)
            for blk in blocks(ino):
                data, p = rd(blk * bs, bs), 0
                while p + 8 <= bs:
                    child, rl, nl = struct.unpack_from("<IHB", data, p)
                    if rl < 8:
                        break
                    if child and nl:
                        yield child, data[p + 8:p + 8 + nl], blk, p + 8 + nl - 1, ino
                    p += rl

        nid, parts = 2, path.strip("/").encode().split(b"/")
        for comp in parts[:-1]:
            nid = next((c for c, nm, *_ in entries(nid) if nm == comp), None)
            if nid is None:
                return False
        hit = next((e for e in entries(nid) if e[1] == parts[-1]), None)
        if not hit:
            return False
        _, _, blk, last, dino = hit
        if struct.unpack_from("<I", dino, 0x20)[0] & 0x1000:
            raise Fail(f"{path}: htree 디렉터리 - debugfs 필요")
        data = bytearray(rd(blk * bs, bs))
        data[last] = ord("~")
        if csum:
            if data[bs - 12:bs - 4] != b"\0\0\0\0\x0c\0\0\xde":
                raise Fail("ext4 디렉터리 체크섬 tail 없음")
            c = crc32c(crc32c(crc32c(seed, struct.pack("<I", nid)), dino[0x64:0x68]), data[:bs - 12])
            struct.pack_into("<I", data, bs - 4, c)
        f.seek(blk * bs)
        f.write(data)
    return True


def strip_apks(sysimg):
    fs = fs_type(sysimg)
    debugfs = fs == "erofs" or find_debugfs(required=False)
    log(f"system 파일시스템: {fs}" + ("" if debugfs else " (debugfs 없음 -> 이름변경 방식)"))
    removed = 0
    for apk in TOSS_APKS:
        # system-as-root 이미지면 /system/..., 아니면 / 기준
        for path in (apk, apk[len("/system"):]):
            ok = erofs_hide(sysimg, path) if fs == "erofs" else \
                ext4_remove(sysimg, path, fs == "ext4-shared") if debugfs else ext4_hide(sysimg, path)
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


def current_parts():
    """기기 GPT. Armbian 등으로 덮여 없으면 []"""
    code, out = run(["rkdeveloptool", "ppt"], quiet=True)
    return parse_ppt(out) if code == 0 else []


def restore():
    # GPT가 없어졌으면(Armbian 설치 후) _head(GPT+로더 영역)도 되돌림
    head = not any(n == "super" for n, _ in current_parts())
    if head:
        log("기기에 Android 파티션 테이블 없음 -> _head도 복구")
    for name, start, count in read_table():
        if name.startswith("_") and not head:
            continue
        img = BACKUP / f"{name}.img"
        if img.stat().st_size != count * 512:
            raise Fail(f"{img} 크기 이상")
        log(f"복구 {name}")
        rk("wl", start, img)
    rk("rd")
    log("복구 완료")


# ---------- Armbian (1/1.5세대) ----------
# Armbian 이미지는 LBA 0부터 eMMC에 통째로 씀 (idbloader@64, u-boot@16384, rootfs).
# 토스 프론트용 이미지는 없음 -> 다른 RK3399 보드 이미지 + 기기용 DTB를 지정해서 씀.
# 안 뜨면 BOOT-GND 쇼트로 MaskROM 진입 후 복구 (bootrom이라 항상 가능).

ARMBIAN = WORK / "armbian.img"


def disk_parts(img):
    """MBR/GPT 디스크 이미지 -> [파티션 시작 byte offset]"""
    with open(img, "rb") as f:
        mbr = f.read(512)
        if mbr[510:] != b"\x55\xaa":
            raise Fail("파티션 테이블 없음 (Armbian .img 맞나?)")
        ents = [struct.unpack_from("<B3xB3xII", mbr, 446 + i * 16) for i in range(4)]
        if ents[0][1] != 0xEE:
            return [lba * 512 for _, t, lba, n in ents if t and n]
        f.seek(512)
        hdr = f.read(92)
        if hdr[:8] != b"EFI PART":
            raise Fail("GPT 헤더 이상")
        ent_lba, n, sz = struct.unpack_from("<QII", hdr, 72)
        f.seek(ent_lba * 512)
        raw = f.read(n * sz)
    return [struct.unpack_from("<Q", raw, i * sz + 32)[0] * 512
            for i in range(n) if raw[i * sz:i * sz + 16].strip(b"\0")]


def armbian_set_dtb(img, dtb):
    """rootfs(또는 /boot 파티션)에 dtb 넣고 armbianEnv.txt fdtfile 교체"""
    debugfs = find_debugfs()
    name = re.sub(r"[^\w.-]", "_", Path(dtb).name)
    local_dtb, env = WORK / name, WORK / "armbianEnv.txt"
    shutil.copyfile(dtb, local_dtb)
    for off in disk_parts(img):
        dev = f"{img}?offset={off}"
        for boot in ("/boot", ""):  # /boot가 별도 파티션이면 그 루트에 있음
            env.unlink(missing_ok=True)
            run([debugfs, "-R", f'dump {boot}/armbianEnv.txt "{env}"', dev], quiet=True)
            if env.exists():
                break
        else:
            continue
        lines = [l for l in env.read_text().splitlines() if not l.startswith("fdtfile=")]
        env.write_text("\n".join(lines + [f"fdtfile=rockchip/{name}"]) + "\n")
        cmds = WORK / "debugfs.cmd"
        cmds.write_text(f'cd {boot}/dtb/rockchip\nrm {name}\nwrite "{local_dtb}" {name}\n'
                        f'cd {boot or "/"}\nrm armbianEnv.txt\nwrite "{env}" armbianEnv.txt\n')
        run([debugfs, "-w", "-f", cmds, dev], quiet=True)
        out = run([debugfs, "-R", f"stat {boot}/dtb/rockchip/{name}", dev], quiet=True)[1]
        if "Inode:" not in out:
            raise Fail("DTB 쓰기 실패 (dtb/rockchip 디렉터리 없음?)")
        log(f"DTB 적용: fdtfile=rockchip/{name}")
        return
    raise Fail("이미지에서 armbianEnv.txt 못 찾음 (Armbian 이미지 맞나?)")


def prepare_armbian(src, dtb=None):
    src = Path(src)
    WORK.mkdir(exist_ok=True)
    log(f"{src.name} 준비 중...")
    with (lzma.open(src) if src.suffix == ".xz" else open(src, "rb")) as i, open(ARMBIAN, "wb") as o:
        shutil.copyfileobj(i, o, 1 << 20)
    size = ARMBIAN.stat().st_size
    if size % 512:
        os.truncate(ARMBIAN, size + 512 - size % 512)
    disk_parts(ARMBIAN)
    if dtb:
        armbian_set_dtb(ARMBIAN, dtb)
    else:
        log("경고: DTB 미지정 - 이미지 원래 보드 DTB로 부팅 (화면/터치 안 될 가능성 큼)")
    return ARMBIAN


def armbian_install(src, dtb=None):
    if not src:
        raise Fail("Armbian 이미지(.img/.img.xz) 선택 필요")
    table = read_table()
    names = {n for n, _, _ in table}
    if not {"_head", "super"} <= names:
        raise Fail("백업이 불완전함 (_head/super 없음). 백업 먼저")
    starts = {n: s for n, s, _ in table}
    for n, s in current_parts():
        if starts.get(n, s) != s:
            raise Fail(f"백업이 이 기기 것이 아님 ({n} 위치 다름)")
    img = prepare_armbian(src, dtb)
    if img.stat().st_size > flash_sectors() * 512:
        raise Fail("이미지가 eMMC보다 큼")
    log("Armbian 쓰는 중... (Android 전부 지워짐, 케이블 건드리지 말 것)")
    rk("wl", 0, img)
    rk("rd")
    log("완료. 안 뜨면 BOOT-GND 쇼트로 MaskROM 진입 후 [복구].")


# ---------- DTB 추출 ----------
# Rockchip Android는 DTB를 resource.img(RSCE, rk-kernel.dtb)나 boot.img(second/dtb 섹션)에 둠.
# vendor 4.4 커널용이라 메인라인 Armbian에 그대로는 안 맞음 -> 기기용 DTS 만들 때 참고용.

FDT_MAGIC = b"\xd0\x0d\xfe\xed"


def rsce_entries(data):
    """resource.img(RSCE) -> {byte_off: 이름}"""
    if data[:4] != b"RSCE":
        return {}
    _hsz, tbl, esz, n = struct.unpack_from("<BBBI", data, 8)
    out = {}
    for i in range(n):
        e = (tbl + i * esz) * 512
        if data[e:e + 4] != b"ENTR":
            break
        off, = struct.unpack_from("<I", data, e + 260)
        out[off * 512] = data[e + 4:e + 260].split(b"\0")[0].decode(errors="replace")
    return out


def fdt_root_props(blob):
    """루트 노드 문자열 속성 (model, compatible)"""
    _m, _sz, st, strs = struct.unpack_from(">4I", blob, 0)
    pos, props = st, {}
    tok, = struct.unpack_from(">I", blob, pos)
    if tok != 1:
        return props
    pos = (blob.index(b"\0", pos + 4) + 4) & ~3
    while True:
        tok, = struct.unpack_from(">I", blob, pos)
        if tok == 4:
            pos += 4
            continue
        if tok != 3:
            return props
        ln, noff = struct.unpack_from(">II", blob, pos + 4)
        name = blob[strs + noff:blob.index(b"\0", strs + noff)].decode()
        val = blob[pos + 12:pos + 12 + ln]
        if name in ("model", "compatible"):
            props[name] = ", ".join(v.decode(errors="replace") for v in val.split(b"\0") if v)
        pos = (pos + 12 + ln + 3) & ~3


def find_dtbs(data):
    """바이너리에서 FDT 블롭 찾기 -> [(off, blob)] (DTBO 오버레이 제외)"""
    out, i = [], data.find(FDT_MAGIC)
    while i >= 0:
        size, st, strs, rsv, ver, comp = struct.unpack_from(">6I", data, i + 4)
        if 0x40 <= size <= 8 << 20 and max(st, strs, rsv) < size and 16 <= ver <= 17 \
                and comp == 16 and i + size <= len(data):
            blob = data[i:i + size]
            if b"__fixups__" not in blob and b"fragment@" not in blob:
                out.append((i, blob))
                i = data.find(FDT_MAGIC, i + size)
                continue
        i = data.find(FDT_MAGIC, i + 4)
    return out


def extract_dtbs(src_dir=None, out_dir=None):
    src_dir, out_dir = Path(src_dir or BACKUP), Path(out_dir or DTB_OUT)
    kinds = ("resource", "boot", "vendor_boot", "recovery")  # resource 먼저: 엔트리 이름이 붙어 있음
    kind = lambda f: next((i for i, k in enumerate(kinds)
                           if f.stem == k or f.stem.startswith(k + "_")), None)
    files = sorted((f for f in src_dir.glob("*") if f.suffix in (".img", ".bin") and kind(f) is not None),
                   key=lambda f: (kind(f), f.name))
    if not files:
        raise Fail("백업에 resource/boot 이미지 없음. 백업 먼저")
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    dtc, seen = shutil.which("dtc"), set()
    for f in files:
        data = f.read_bytes()
        names = rsce_entries(data)
        for off, blob in find_dtbs(data):
            if blob in seen:
                continue
            seen.add(blob)
            name = re.sub(r"[^\w.-]", "_", names.get(off, "")) or f"{f.stem}_{off:x}.dtb"
            dst = out_dir / (name if name.endswith(".dtb") else name + ".dtb")
            dst.write_bytes(blob)
            props = fdt_root_props(blob)
            log(f"{dst.name} ({len(blob)} B, {f.name}@{off:#x}) "
                f"model={props.get('model', '?')} compatible={props.get('compatible', '?')}")
            if dtc:
                run([dtc, "-q", "-I", "dtb", "-O", "dts", "-o", dst.with_suffix(".dts"), dst], quiet=True)
    if not seen:
        raise Fail("DTB 못 찾음 (압축된 커널에 붙어있을 수 있음)")
    log(f"{len(seen)}개 추출 -> {out_dir}" + ("" if dtc else " (dts 변환은 brew install dtc)"))
    log("주의: Android vendor 커널용. Armbian(메인라인)엔 그대로 안 맞을 수 있음 - DTS 작성 참고용")
    return sorted(out_dir.glob("*.dtb"))


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


# ---------- 동작 (GUI/CLI 공용) ----------
# o: edl, loader, userdata, verity_off, image, dtb 속성을 가진 객체

def do_check(o):
    if o.edl:
        edl_check(o.loader)
    else:
        check_device(o.loader)


def do_backup(o):
    if o.edl:
        edl_backup(o.loader, o.userdata)
    else:
        check_device(o.loader)
        backup(o.userdata)


def do_patch(o):
    patch(verity_off=o.verity_off)


def do_flash(o):
    if o.edl:
        edl_flash(o.loader)
    else:
        check_device(o.loader)
        flash()


def do_restore(o):
    if o.edl:
        edl_restore(o.loader)
    else:
        check_device(o.loader)
        restore()


def do_all(o):
    do_backup(o)
    do_patch(o)
    do_flash(o)


def do_armbian(o):
    if o.edl:
        raise Fail("Armbian은 1/1.5세대(RK3399)만")
    check_device(o.loader)
    armbian_install(o.image, o.dtb)


# CLI 이름: (버튼 이름, 함수, 확인 문구)
ACTIONS = {
    "check": ("장치 확인", do_check, None),
    "backup": ("백업", do_backup, None),
    "patch": ("패치", do_patch, None),
    "flash": ("플래시", do_flash, "패치된 super를 기기에 씁니다. 계속?"),
    "restore": ("복구", do_restore, "백업 이미지로 전부 되돌립니다. 계속?"),
    "all": ("전체 실행", do_all, "백업 -> 패치 -> 플래시 순서로 진행합니다. 계속?"),
    "dtb": ("DTB 추출", lambda o: extract_dtbs(), None),
    "armbian": ("Armbian 설치", do_armbian,
                "eMMC 전체를 Armbian으로 덮습니다 (Android 삭제). 백업 확인했나요? 계속?"),
}


# ---------- CLI ----------

def cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="tf_flasher.py", description="인자 없이 실행하면 GUI")
    ap.add_argument("action", choices=ACTIONS, help=", ".join(f"{k}={v[0]}" for k, v in ACTIONS.items()))
    ap.add_argument("--edl", action="store_true", help="2세대 (Qualcomm EDL). 기본은 1/1.5세대 (Rockchip)")
    ap.add_argument("--loader", default="", help="MaskROM 로더(rk3399_loader_*.bin) / firehose 로더(.elf/.mbn)")
    ap.add_argument("--userdata", action="store_true", help="userdata도 백업 (느림)")
    ap.add_argument("--verity-off", action="store_true", help="vbmeta verity 끄기 (부트로더 언락 기기만 효과)")
    ap.add_argument("--image", default="", help="Armbian 이미지 (.img/.img.xz)")
    ap.add_argument("--dtb", default="", help="Armbian용 DTB (선택)")
    ap.add_argument("-y", "--yes", action="store_true", help="확인 질문 생략")
    o = ap.parse_args(argv)
    _, fn, confirm = ACTIONS[o.action]
    if confirm and not o.yes and input(f"{confirm} [y/N] ").strip().lower() != "y":
        return 1
    try:
        fn(o)
    except Fail as e:
        print(f"✘ {e}")
        return 1
    print("✔ 성공")
    return 0


# ---------- GUI ----------

def gui():
    import argparse
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
    armbian, dtb = tk.StringVar(), tk.StringVar()
    for label, var in (("Armbian 이미지:", armbian), ("DTB (선택):", dtb)):
        r = tk.Frame(root)
        r.pack(fill="x", padx=8)
        tk.Label(r, text=label, width=14, anchor="w").pack(side="left")
        tk.Entry(r, textvariable=var, width=40).pack(side="left", fill="x", expand=True)
        tk.Button(r, text="찾기", command=lambda v=var: v.set(
            filedialog.askopenfilename() or v.get())).pack(side="left")
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
        o = argparse.Namespace(edl=gen.get() == "edl", loader=loader.get(), userdata=userdata.get(),
                               verity_off=verity.get(), image=armbian.get(), dtb=dtb.get())

        def job():
            try:
                fn(o)
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

    for text, fn, confirm in ACTIONS.values():
        b = tk.Button(btns, text=text, command=lambda f=fn, c=confirm: start(f, c))
        b.pack(side="left", padx=2)
        buttons.append(b)

    poll()
    root.mainloop()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    gui()
