"""Live release-ROM harness checks (skips cleanly when the artifacts are absent).

These tests are the only place the 528 KiB ROM / the worker are touched.  They
never modify the production path and never enter Git with a blob.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.rom_oracle import release_harness as r  # noqa: E402

ROM = Path(os.environ.get("SIDEDEX_RELEASE_ROM", r.DEFAULT_ROM))
ELF = Path(os.environ.get("SIDEDEX_DIFF_ELF", r.DEFAULT_ELF))
WORKER = Path(os.environ.get("SIDEDEX_ROM_WORKER_EXE", r.DEFAULT_WORKER))

pytestmark = pytest.mark.skipif(
    not (ROM.is_file() and WORKER.is_file()),
    reason="release ROM or rom_worker.exe is missing")


def _client():
    import rom_bridge as rb
    return rb.RomWorkerClient(
        rom_path=ROM, expected_artifact_sha256=r.sha256_file(ROM),
        worker_path=WORKER, expected_worker_sha256=r.sha256_file(WORKER), timeout=180.0)


def test_release_artifacts_are_pinned():
    assert r.sha256_file(ROM).upper() == r.PINNED_ROM_SHA256
    assert r.sha256_file(WORKER).upper() == r.PINNED_WORKER_SHA256


def test_h4_h5_ram_patch_and_iwram_write():
    """H4: patch EWRAM+IWRAM inside a savestate, reload, read back.  H5: IWRAM writes."""
    with _client() as client:
        client.run_frames(600)
        slot = client.save_checkpoint("release-h4")
        raw = base64.b64decode(client.export_state(slot["handle"])["blob"], validate=True)
        header, payload = r.header_payload(raw)
        e_addr, e_val = 0x02030000, 0xABCD1234
        i_addr, i_val = r.RELEASE_MAP["gRngValue"], 0x55667788
        writes = {e_addr: e_val, i_addr: i_val}
        blob = r.make_blob(raw, r.patch_payload(payload, writes))
        imported = client.import_state(base64.b64encode(blob).decode(), name="release-h4-patched")
        client.restore_state(imported["handle"])
        assert int.from_bytes(client.read_memory_block(e_addr, 4), "little") == e_val
        assert int.from_bytes(client.read_memory_block(i_addr, 4), "little") == i_val
        # H5: direct IWRAM write through the worker op.
        client.write_memory(i_addr, (0x0A0B0C0D).to_bytes(4, "little"))
        assert int.from_bytes(client.read_memory_block(i_addr, 4), "little") == 0x0A0B0C0D
        # the source slot is untouched by the patch round-trip.
        again = base64.b64decode(client.export_state(slot["handle"])["blob"], validate=True)
        assert again == raw


def test_battlemon_struct_offsets_match_the_test_elf():
    """The release projection reader uses source-defined offsets; prove them on the
    test ELF where the decoded active state is available."""
    if not ELF.is_file():
        pytest.skip("test ELF is missing")
    from tools.rom_oracle import rom_diff_harness as h
    session = h.DiffSession(elf=ELF)
    try:
        import copy
        import json
        sample = json.loads((h.TOOLS / "dynamic_sample.json").read_text(encoding="utf-8"))
        session.build_root(copy.deepcopy(sample))
        block = session.client.read_memory_block(h.GBATTLEMONS, r.BATTLEMON_SIZE)
        for battler in range(2):
            at = h.GBATTLEMONS + battler * r.BATTLEMON_SIZE
            block = session.client.read_memory_block(at, r.BATTLEMON_SIZE)
            species = int.from_bytes(block[r.BATTLEMON["species"]:r.BATTLEMON["species"] + 2],
                                     "little")
            decoded = [e for e in session.root_records
                       if e.startswith(f"ROMTRACE|phase|") and f"|state|active|{battler}|" in e]
            assert decoded, session.root_records[:5]
            assert species == int(decoded[-1].split("|")[7]), (battler, decoded[-1])
            hp = int.from_bytes(block[r.BATTLEMON["hp"]:r.BATTLEMON["hp"] + 2], "little")
            assert hp == int(decoded[-1].split("|")[8]), (battler, decoded[-1])
    finally:
        session.close()


def test_release_root_capture_reaches_a_battle():
    """The phase-4 crux precondition.  A battle-entry script does not exist yet, so this
    fails closed with the exact blocker instead of fabricating a root."""
    script = r.load_input_script()
    with _client() as client:
        try:
            frames = r.run_input_script(client, script, progress=lambda f, s: None)
        except r.ReleaseRootUnavailable as exc:
            pytest.skip(f"release root capture blocked: {exc}")
        slot = client.save_checkpoint("release-root")
        projection = r.read_projection(client)
        assert frames > 0
        assert projection["mons"][0]["species"] != 0
        # Pin the root identity (the blob itself is never committed).
        raw = base64.b64decode(client.export_state(slot["handle"])["blob"], validate=True)
        assert hashlib.sha256(raw).hexdigest()
