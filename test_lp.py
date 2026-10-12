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


def case(fs):
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
        if fs == "erofs":
            assert exists(check, fs, "/system/app/TossPlace/TossPlace.ap~")
            r = subprocess.run(["fsck.erofs", check], capture_output=True, text=True)
        else:
            r = subprocess.run([E2 + "e2fsck", "-fn", check], capture_output=True, text=True)
        # shared 는 unlink라 고아 inode 남는 게 정상
        assert fs == "ext4-shared" or r.returncode == 0, r.stdout + r.stderr
        assert tf.parse_lp(sup) == {"system_a": expected}  # 원본 그대로
    print(f"OK {fs}")


if __name__ == "__main__":
    for fs in ("ext4", "ext4-shared", "erofs"):
        case(fs)
