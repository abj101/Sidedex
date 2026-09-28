"""Offline tests for the release-ROM harness (no emulator, no artifacts needed)."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.rom_oracle import release_harness as r  # noqa: E402


def _blob(payload=b"\x00" * 64):
    header = bytearray(216)
    header[:8] = r.BLOB_MAGIC
    header[56:88] = hashlib.sha256(payload).digest()
    return bytes(header) + payload


def test_input_script_is_well_formed_and_honest():
    script = r.load_input_script()
    assert script["rom_sha256"].upper() == r.PINNED_ROM_SHA256
    assert script["status"] == "incomplete", "the capture script must not claim success"
    assert script["blocker"] == "root-capture-unavailable"
    assert script["steps"], "the script needs at least one step"
    flat = []
    for step in script["steps"]:
        flat.extend(step["steps"] * step["repeat"] if "repeat" in step else [step])
    assert all({"frames", "keys"} <= set(s) for s in flat)
    assert all(s["frames"] > 0 for s in flat)
    assert int(script["detect"]["gBattleTypeFlags"], 16) == r.RELEASE_MAP["gBattleTypeFlags"]


def test_scenarios_are_pinned_and_narrow():
    data = r.load_scenarios()
    assert data["release_rom_sha256"].upper() == r.PINNED_ROM_SHA256
    assert data["elf_sha256"] == r.PINNED_ELF_SHA256
    assert len(data["scenarios"]) >= 10
    assert "query mode" in data["not_claimed"]
    for scenario in data["scenarios"]:
        assert (ROOT / "tools" / "rom_oracle" / "fixtures").glob(f"*_{scenario['fixture'][1:].lower()}*")


def test_release_map_matches_the_documented_pins():
    assert r.RELEASE_MAP["gBattleMons"] == 0x02000360
    assert r.RELEASE_MAP["gBattleCommunication"] == 0x02000698
    assert r.RELEASE_MAP["gRngValue"] == 0x0300627C
    assert r.RELEASE_MAP["gRng2Value"] == 0x0300628C
    # IWRAM pins are build-specific and must never be copied from the test ELF.
    assert r.RELEASE_MAP["gMain"] == 0x03004210
    assert r.BATTLEMON["hp"] + 2 <= r.BATTLEMON_SIZE
    assert r.BATTLEMON["status1"] + 4 <= r.BATTLEMON_SIZE


def test_patch_payload_targets_ewram_and_iwram():
    payload = bytes(r.SAVESTATE_EWRAM_OFFSET + 0x40000)
    patched = r.patch_payload(payload, {0x02030000: 0xABCD1234, 0x0300627C: 0x55667788})
    e_off = r.SAVESTATE_EWRAM_OFFSET + (0x02030000 - r.EWRAM_BASE)
    i_off = r.SAVESTATE_IWRAM_OFFSET + (0x0300627C - r.IWRAM_BASE)
    assert patched[e_off:e_off + 4] == (0xABCD1234).to_bytes(4, "little")
    assert patched[i_off:i_off + 4] == (0x55667788).to_bytes(4, "little")
    assert patched[:0x1000] == payload[:0x1000]


def test_patch_payload_refuses_non_ram_addresses():
    with pytest.raises(r.ReleaseHarnessError):
        r.patch_payload(bytes(0x30000), {0x08000000: 1})
    with pytest.raises(r.ReleaseHarnessError):
        r.patch_payload(bytes(0x30000), {0x02030000: 1})


def test_make_blob_recomputes_the_payload_digest():
    raw = _blob(b"\x11" * 32)
    header, payload = r.header_payload(raw)
    patched = r.patch_payload(payload + bytes(0x22000), {})
    blob = r.make_blob(raw, patched)
    assert blob[:8] == r.BLOB_MAGIC
    assert blob[56:88] == hashlib.sha256(patched).digest()
    assert blob[216:] == patched
    assert blob[:56] + blob[88:216] == raw[:56] + raw[88:216]


class FakeClient:
    """Minimal client double: a battle appears only when ``battle_at`` is set."""

    def __init__(self, battle_at=None):
        self.frames = 0
        self.battle_at = battle_at
        self.keys = 0

    def run_frames(self, frames, keys=0):
        self.frames += frames
        self.keys = keys
        return {"trace_events": []}

    def read_memory(self, address, width):
        if address == r.RELEASE_MAP["gBattleTypeFlags"]:
            return 1 if self.battle_at and self.frames >= self.battle_at else 0
        if address == r.RELEASE_MAP["gMain"] + 4:
            return 0x08188E15
        if address == r.RELEASE_MAP["gBattleCommunication"]:
            return 0
        return 0


def test_run_input_script_returns_on_battle_entry():
    script = r.load_input_script()
    client = FakeClient(battle_at=704)
    frames = r.run_input_script(client, script)
    assert 700 <= frames <= 716, frames
    assert client.keys == 8  # still holding START when the battle was detected


def test_run_input_script_fails_closed_with_the_last_state():
    script = r.load_input_script()
    with pytest.raises(r.ReleaseRootUnavailable) as excinfo:
        r.run_input_script(FakeClient(), script, progress=lambda *a: None)
    message = str(excinfo.value)
    assert "0x8188e15" in message
    assert "root-capture-unavailable" not in message  # the error names the script instead
    assert excinfo.value.frames == sum(
        s["frames"] * s.get("repeat", 1) for s in script["steps"]
        if "frames" in s) + sum(
        inner["frames"] * s["repeat"] for s in script["steps"] if "repeat" in s
        for inner in s["steps"])


def _projection(**over):
    base = {"mons": [{"species": 1, "level": 50, "hp": 100, "max_hp": 105, "status1": 0,
                      "pp": [35, 40, 5, 20]},
                     {"species": 130, "level": 50, "hp": 155, "max_hp": 155, "status1": 0,
                      "pp": [35, 0, 0, 0]}],
            "outcome": 0, "rng_main": 0x11111111, "rng_secondary": 0x22222222,
            "battle_communication": 0}
    base.update(over)
    return base


def test_compare_h2_equal_and_mismatch():
    assert r.compare_h2(_projection(), _projection()) == []
    other = _projection()
    other["mons"] = [dict(other["mons"][0], hp=99), other["mons"][1]]
    diffs = r.compare_h2(_projection(), other)
    assert len(diffs) == 1 and "mon[0].hp" in diffs[0], diffs
    other = _projection(rng_main=0xDEADBEEF, outcome=1)
    diffs = r.compare_h2(_projection(), other)
    assert len(diffs) == 2 and any("outcome" in d for d in diffs) and \
        any("rng_main" in d for d in diffs), diffs


def test_identify_refuses_a_wrong_artifact(tmp_path):
    artifact = tmp_path / "rom.bin"
    artifact.write_bytes(b"not the pinned rom")
    with pytest.raises(r.ReleaseHarnessError):
        r.identify(artifact, r.PINNED_ROM_SHA256)
    assert r.identify(artifact, hashlib.sha256(b"not the pinned rom").hexdigest())


def test_exclude_list_names_presentation_only_exclusions():
    names = [name for name, _ in r.EXCLUDE_LIST]
    assert "requestId" in names
    assert all(reason for _, reason in r.EXCLUDE_LIST)
