"""Golden SDR2 vectors are current and self-consistent (orchestrator-owned)."""
import struct
import zlib
from pathlib import Path

from tools.rom_oracle import make_sdr2_vectors as m


def test_vectors_current_and_crc_valid():
    good, bad = m.vectors()
    for n, b in {**good, **bad}.items():
        assert (m.OUT / f"{n}.bin").read_bytes() == b, f"{n} stale: rerun make_sdr2_vectors.py"
    for n, b in good.items():
        buf = bytearray(b)
        crc = struct.unpack_from("<I", buf, 12)[0]
        struct.pack_into("<I", buf, 12, 0)
        assert struct.unpack_from("<I", buf, 8)[0] == len(b) and crc == zlib.crc32(bytes(buf[12:])), n
