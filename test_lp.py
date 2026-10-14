"""합성 super.img로 parse_lp/patch 왕복 검증 (ext4, ext4 shared_blocks, EROFS). 실행: python3 test_lp.py"""
import os
import struct
import subprocess
import tempfile
from pathlib import Path

import tf_flasher as tf

E2 = "/opt/homebrew/opt/e2fsprogs/sbin/"
KEEP = "/system/app/Keep/Keep.apk"


def make_system(tmp, fs):
    root = tmp / "root"
    for apk in tf.TOSS_APKS + [KEEP]:
        p = root / apk.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(5000))
    # 디렉터리가 여러 블록에 걸치게
    for i in range(300):
        (root / "system/app" / f"Filler{i:03d}").mkdir()
    img = tmp / "system.img"
    if fs == "erofs":
        subprocess.run(["mkfs.erofs", "-zlz4", img, root], check=True, capture_output=True)
        with open(img, "ab") as f:  # 파티션처럼 여유 공간
            f.write(b"\0" * (1 << 20))
    else:
        opts = ["-O", "^metadata_csum"] if fs == "ext4-shared" else []
        subprocess.run([E2 + "mke2fs", "-q", "-t", "ext4", "-b", "4096", *opts, "-d", root, img, "8M"],
                       check=True)
        if fs == "ext4-shared":
            with open(img, "r+b") as f:
                f.seek(1024 + 0x64)
                v, = struct.unpack("<I", f.read(4))
                f.seek(1024 + 0x64)
                f.write(struct.pack("<I", v | 0x4000))
    return img


def make_super(sysimg, out):
    data = sysimg.read_bytes()
    half = len(data) // 2 // 4096 * 4096
    # system을 두 extent로 쪼개 배치 (사이에 1MiB 빈 공간)
    ext = [(1 << 20, data[:half]), ((2 << 20) + half, data[half:])]
    buf = bytearray(ext[1][0] + len(ext[1][1]) + (1 << 20))
    struct.pack_into("<I", buf, 4096, tf.LP_GEOMETRY_MAGIC)
    # A/B: system_a에 extent, system_b는 빈 파티션 (virtual A/B 흉내)
    parts = struct.pack("<36s4I", b"vendor", 0, 0, 0, 0) + \
        struct.pack("<36s4I", b"system_a", 0, 0, 2, 0) + \
        struct.pack("<36s4I", b"system_b", 0, 2, 0, 0)
    exts = b"".join(struct.pack("<QIQI", len(d) // 512, 0, off // 512, 0) for off, d in ext)
    header_size = 256
    hdr = struct.pack("<IHHI32sI32s", tf.LP_HEADER_MAGIC, 10, 2, header_size, b"", 0, b"")
    hdr += struct.pack("<6I", 0, 3, 52, len(parts), 2, 24)
    m = tf.LP_METADATA_OFFSET
    buf[m:m + len(hdr)] = hdr
    buf[m + header_size:m + header_size + len(parts) + len(exts)] = parts + exts
    for off, d in ext:
        buf[off:off + len(d)] = d
    out.write_bytes(buf)
    return [(off, len(d)) for off, d in ext]


def exists(img, fs, path):
    if fs == "erofs":
        r = subprocess.run(["dump.erofs", f"--path={path}", img], capture_output=True, text=True)
        return r.returncode == 0 and "Path :" in r.stdout
    r = subprocess.run([tf.find_debugfs(), "-R", f"stat {path}", img], capture_output=True, text=True)
    return "Inode:" in r.stdout


def case(fs, no_debugfs=False):
    real = tf.find_debugfs
    if no_debugfs:  # Windows 흉내: 패치는 debugfs 없이 (검증만 debugfs 사용)
        tf.find_debugfs = lambda required=True: real() if required else None
    try:
        _case(fs, no_debugfs)
    finally:
        tf.find_debugfs = real
    print(f"OK {fs}{' (debugfs 없음)' if no_debugfs else ''}")


def _case(fs, hide):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        sysimg = make_system(tmp, fs)
        bk = tmp / "backup"
        bk.mkdir()
        sup = bk / "super.bin"
        expected = make_super(sysimg, sup)
        assert tf.parse_lp(sup) == {"system_a": expected}, tf.parse_lp(sup)
        vb = bytearray(4096)
        vb[:4] = b"AVB0"
        (bk / "vbmeta_a.bin").write_bytes(vb)

        out = tmp / "work" / "flash"
        assert tf.patch(sup, out, verity_off=True) == 4
        assert (out / "super.img").stat().st_size == sup.stat().st_size
        assert struct.unpack_from(">I", (out / "vbmeta_a.img").read_bytes(), 120)[0] == 3

        # 패치된 super에서 system 재추출 후 확인
        check = tmp / "check.img"
        with open(out / "super.img", "rb") as f, open(check, "wb") as d:
            for off, ln in expected:
                f.seek(off)
                d.write(f.read(ln))
        for apk in tf.TOSS_APKS:
            assert not exists(check, fs, apk), apk
        assert exists(check, fs, KEEP)
        assert exists(check, fs, "/system/app/Filler299")
        if fs == "erofs" or hide:
            assert exists(check, fs, "/system/app/TossPlace/TossPlace.ap~")
        if fs == "erofs":
            r = subprocess.run(["fsck.erofs", check], capture_output=True, text=True)
        else:
            r = subprocess.run([E2 + "e2fsck", "-fn", check], capture_output=True, text=True)
        # shared 는 unlink라 고아 inode 남는 게 정상. 이름변경 방식은 체크섬까지 깨끗해야 함
        assert (fs == "ext4-shared" and not hide) or r.returncode == 0, r.stdout + r.stderr
        assert tf.parse_lp(sup) == {"system_a": expected}  # 원본 그대로


def make_disk(tmp, table, boot_part):
    """Armbian 흉내: 1MiB 뒤 ext4 파티션들. boot_part면 /boot가 별도 파티션"""
    parts = []
    for i, files in enumerate([{"boot": True}, {}] if boot_part else [{"boot": True}]):
        root = tmp / f"p{i}"
        b = root / ("" if boot_part and i == 0 else "boot")
        if files:
            (b / "dtb-6.6/rockchip").mkdir(parents=True)
            os.symlink("dtb-6.6", b / "dtb")
            (b / "armbianEnv.txt").write_text("verbosity=1\nfdtfile=rockchip/rk3399-rock-pi-4b.dtb\n")
        else:
            (root / "etc").mkdir(parents=True)
        img = tmp / f"p{i}.img"
        subprocess.run([E2 + "mke2fs", "-q", "-t", "ext4", "-d", root, img, "8M"], check=True)
        parts.append(img.read_bytes())
    lba = [2048 + i * 16384 for i in range(len(parts))]
    disk = bytearray(lba[-1] * 512 + len(parts[-1]))
    for l, d in zip(lba, parts):
        disk[l * 512:l * 512 + len(d)] = d
    if table == "mbr":
        for i, l in enumerate(lba):
            struct.pack_into("<B3xB3xII", disk, 446 + i * 16, 0, 0x83, l, 16384)
    else:
        struct.pack_into("<B3xB3xII", disk, 446, 0, 0xEE, 1, 0xFFFFFFFF)
        disk[512:512 + 92] = b"EFI PART" + bytes(64) + struct.pack("<QII", 2, 128, 128) + bytes(4)
        for i, l in enumerate(lba):
            struct.pack_into("<16s16sQQ", disk, 1024 + i * 128, b"\x01" * 16, b"\x02" * 16, l, l + 16383)
    disk[510:512] = b"\x55\xaa"
    out = tmp / "armbian.img.xz"
    import lzma
    out.write_bytes(lzma.compress(bytes(disk)))
    return out, lba[0] * 512, "" if boot_part else "/boot"


def armbian_case(table, boot_part):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tf.WORK = tmp / "w ork"  # 공백 경로
        tf.ARMBIAN = tf.WORK / "armbian.img"
        src, off, boot = make_disk(tmp, table, boot_part)
        dtb = tmp / "my board.dtb"
        dtb.write_bytes(b"\xd0\x0d\xfe\xed" + os.urandom(100))
        img = tf.prepare_armbian(src, dtb)
        dev = f"{img}?offset={off}"
        dfs = tf.find_debugfs()
        r = subprocess.run([dfs, "-R", f"cat {boot}/armbianEnv.txt", dev], capture_output=True, text=True)
        assert r.stdout == "verbosity=1\nfdtfile=rockchip/my_board.dtb\n", r.stdout
        r = subprocess.run([dfs, "-R", f"cat {boot}/dtb-6.6/rockchip/my_board.dtb", dev], capture_output=True)
        assert r.stdout == dtb.read_bytes()
        p = tmp / "part.img"
        with open(img, "rb") as f:
            f.seek(off)
            p.write_bytes(f.read(8 << 20))
        r = subprocess.run([E2 + "e2fsck", "-fn", p], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
    print(f"OK armbian {table}{' +boot' if boot_part else ''}")


def dtc(tmp, name, src):
    (tmp / f"{name}.dts").write_text(src)
    subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", tmp / f"{name}.dtb", tmp / f"{name}.dts"],
                   check=True)
    return (tmp / f"{name}.dtb").read_bytes()


def dtb_case():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        board = dtc(tmp, "board", '/dts-v1/;\n/ { model = "Toss Front"; compatible = "toss,front", '
                    '"rockchip,rk3399"; #address-cells = <2>; cpus { }; };')
        other = dtc(tmp, "other", '/dts-v1/;\n/ { model = "Other"; compatible = "x,y"; };')
        overlay = dtc(tmp, "ovl", '/dts-v1/;\n/plugin/;\n&foo { status = "okay"; };')
        bk = tmp / "backup"
        bk.mkdir()
        # resource.img: RSCE 헤더 + 엔트리 2개(logo, rk-kernel.dtb)
        res = bytearray(512 * 4)
        res[:4] = b"RSCE"
        struct.pack_into("<HHBBBI", res, 4, 0, 0, 1, 1, 1, 2)
        logo = b"BM" + bytes(510)
        for i, (name, blob) in enumerate(((b"logo.bmp", logo), (b"rk-kernel.dtb", board))):
            off = len(res) // 512
            res[512 * (1 + i):512 * (1 + i) + 268] = b"ENTR" + name.ljust(256, b"\0") + struct.pack("<II", off, len(blob))
            res += blob + bytes(-len(blob) % 512)
        (bk / "resource.img").write_bytes(res)
        # boot.img: 쓰레기 + 같은 DTB(중복) + 다른 DTB + 오버레이 + 가짜 magic
        (bk / "boot.img").write_bytes(b"ANDROID!" + os.urandom(3000) + tf.FDT_MAGIC + b"\xff" * 20 +
                                      board + bytes(100) + other + overlay + os.urandom(1000))
        (bk / "super.img").write_bytes(other)  # 대상 아님
        out = tf.extract_dtbs(bk, tmp / "dtb")
        assert [d.name for d in out] == ["boot_" + f"{3008 + 24 + len(board) + 100:x}.dtb", "rk-kernel.dtb"], out
        assert (tmp / "dtb/rk-kernel.dtb").read_bytes() == board
        assert tf.fdt_root_props(board) == {"model": "Toss Front", "compatible": "toss,front, rockchip,rk3399"}
        assert "Toss Front" in (tmp / "dtb/rk-kernel.dts").read_text()
    print("OK dtb")


if __name__ == "__main__":
    dtb_case()
    for fs in ("ext4", "ext4-shared", "erofs"):
        case(fs)
    for fs in ("ext4", "ext4-shared"):
        case(fs, no_debugfs=True)
    for table, boot_part in (("mbr", False), ("gpt", False), ("mbr", True)):
        armbian_case(table, boot_part)
