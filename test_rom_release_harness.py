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
    assert script["worker_sha256"].upper() == r.PINNED_WORKER_SHA256
    assert script["status"] in ("captured", "incomplete")
    if script["status"] == "captured":
        assert len(script["root_sha256"]) == 64
        assert script["root_frame"] > 0
    else:
        assert script.get("blocker"), "an incomplete script must name its blocker"
    flat = r._flatten_steps(script["steps"])
    assert flat, "the script needs at least one step"
    for step in flat:
        assert step["frames"] > 0
        spec = step["keys"]
        if isinstance(spec, list):
            assert spec and all(isinstance(k, int) for k in spec)
        else:
            assert isinstance(spec, int)
    # Every hop after the fixed intro movie must be condition-driven.
    driven = [s for s in flat if s.get("until")]
    assert len(driven) >= 10, len(driven)
    assert flat[-1]["name"] == "grass-down"
    assert script["root_settle"]["symbol"] == "gBattleMons"
    assert int(script["detect"]["gBattleTypeFlags"], 16) == r.RELEASE_MAP["gBattleTypeFlags"]


def test_scenarios_are_pinned_and_narrow():
    data = r.load_scenarios()
    assert data["release_rom_sha256"].upper() == r.PINNED_ROM_SHA256
    assert data["elf_sha256"] == r.PINNED_ELF_SHA256
    assert data["status"]["root_capture"] == "captured"
    assert len(data["status"]["root_sha256"]) == 64
    assert data["status"]["h2"] == "blocked"  # the R-P* scenario matrix is not enumerated yet
    assert len(data["scenarios"]) >= 10
    assert "query mode" in data["not_claimed"]
    for scenario in data["scenarios"]:
        assert scenario["release_ready"] is False
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


def test_empty_valid_save_uses_release_sector_layout_but_has_no_game_state(tmp_path):
    path = tmp_path / "empty-valid.sav"
    result = r.write_empty_valid_save(path)
    image = path.read_bytes()
    assert len(image) == 32 * 4096
    assert result["bytes"] == len(image)
    assert result["sha256"] == hashlib.sha256(image).hexdigest()
    for sector_id in range(28):
        start = sector_id * r.SAVE_SECTOR_BYTES
        data = image[start:start + r.SAVE_SECTOR_DATA_BYTES]
        footer = image[start + r.SAVE_SECTOR_DATA_BYTES:start + r.SAVE_SECTOR_BYTES]
        assert int.from_bytes(footer[0:2], "little") == sector_id
        assert int.from_bytes(footer[2:4], "little") == r._save_sector_checksum(data)
        assert int.from_bytes(footer[4:8], "little") == r.SAVE_SECTOR_SIGNATURE
    assert image[28 * r.SAVE_SECTOR_BYTES:] == b"\xff" * (4 * r.SAVE_SECTOR_BYTES)
    assert image[4 * r.SAVE_SECTOR_BYTES:4 * r.SAVE_SECTOR_BYTES +
                 r.SAVE_SECTOR_DATA_BYTES] == bytes(r.SAVE_SECTOR_DATA_BYTES)


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
        self.key_calls = []

    def set_keys(self, keys):
        self.keys = keys
        self.key_calls.append(keys)

    def run_frames(self, frames, keys=None):
        self.frames += frames
        if keys is not None:
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

    def read_memory_block(self, address, length):
        if address == r.RELEASE_MAP["gSaveBlock1Ptr"]:
            return (0x02000000).to_bytes(4, "little")
        if address == 0x02000000:
            return b"\x02\x00\x02\x00\x19\x28\xff\x00\xff\xff"[:length]
        return bytes(length)


def test_run_input_script_returns_on_battle_entry():
    script = r.load_input_script()
    client = FakeClient(battle_at=704)
    frames = r.run_input_script(client, script)
    assert 700 <= frames <= 716, frames
    assert client.keys == 8  # still holding START when the battle was detected
    assert client.key_calls[:2] == [0, 8]


def test_run_input_script_fails_closed_with_the_last_state():
    # Keep this check independent of the real route: a tiny fixed script proves the
    # wrapper reports the last machine state when no battle ever appears.
    minimal = {"name": "minimal", "steps": [{"keys": 0, "frames": 4},
                                             {"keys": 8, "frames": 4}],
               "detect": {"gBattleTypeFlags": "0x020002C0"}}
    with pytest.raises(r.ReleaseRootUnavailable) as excinfo:
        r.run_input_script(FakeClient(), minimal, progress=lambda *a: None)
    message = str(excinfo.value)
    assert "callback2" in message
    assert "party_count" in message
    assert "player_x" in message
    assert "map_group" in message
    assert "root-capture-unavailable" not in message  # the error names the script instead
    assert excinfo.value.frames == 8


def test_route_step_error_reports_the_blocked_hop(tmp_path):
    script = {"name": "t", "steps": [
        {"name": "blocked-hop", "keys": 16, "frames": 8, "until": {"kind": "pos", "x": 999}}]}
    with pytest.raises(r.RouteStepError) as excinfo:
        r.run_route(FakeClient(), script, milestones_dir=tmp_path)
    assert excinfo.value.step["name"] == "blocked-hop"
    assert "blocked-hop" in str(excinfo.value)
    assert "map=" in str(excinfo.value) and "pos=" in str(excinfo.value)


def test_multi_mask_step_cycles_the_key_sequence():
    script = {"name": "seq", "steps": [
        {"name": "up-then-a", "keys": [64, 1], "frames": 8, "until": {"kind": "pos", "x": 999}}]}
    client = FakeClient()
    with pytest.raises(r.RouteStepError):
        r.run_input_script(client, script)
    # poll_every is 4, so masks alternate per chunk: 64 then 1.
    assert client.key_calls[:2] == [64, 1]


class MilestoneClient(FakeClient):
    """Client double that keeps exported savestate blobs in memory."""

    def __init__(self):
        super().__init__()
        self.slots = {}

    def save_checkpoint(self, name):
        raw = ("state:" + name).encode()
        self.slots[name] = base64.b64encode(raw).decode()
        return {"handle": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    def export_state(self, handle):
        return {"blob": self.slots[handle]}

    def import_state(self, blob, name=None):
        self.slots[name] = blob
        return {"handle": name}

    def restore_state(self, handle):
        return None


def test_run_route_records_a_hashed_milestone_manifest(tmp_path):
    script = {"name": "m", "steps": [
        {"name": "hop", "keys": 0, "frames": 4, "until": {"kind": "pos", "x": 2},
         "milestone": "first"}]}
    with pytest.raises(r.ReleaseRootUnavailable):
        r.run_route(MilestoneClient(), script, milestones_dir=tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    blob_path = tmp_path / "first.sdxstate"
    assert blob_path.is_file()
    entry = manifest["milestones"]["first"]
    assert entry["sha256"] == hashlib.sha256(blob_path.read_bytes()).hexdigest()
    assert entry["bytes"] == blob_path.stat().st_size
    assert manifest["rom_sha256"] == r.PINNED_ROM_SHA256


class RamClient:
    """Client double with a byte-addressable RAM so write/read-back is provable."""

    def __init__(self):
        self.ram = {}

    def read_memory_block(self, address, length):
        if address not in self.ram:
            self.ram[address] = bytes(range(256))[0:1] * length
        return self.ram[address]

    def write_memory(self, address, data):
        self.ram[address] = bytes(data)


def test_verify_root_projection_writes_back_and_restores():
    result = r.verify_root_projection(RamClient())
    assert set(result) == {"gBattleMons", "gPlayerParty", "gRngValue", "gRng2Value"}
    assert all(v["write_landed"] and v["restored"] for v in result.values())


def test_verify_identity_refuses_a_wrong_artifact(tmp_path):
    rom = tmp_path / "rom.gba"
    rom.write_bytes(b"not the pinned rom")
    worker = tmp_path / "rom_worker.exe"
    worker.write_bytes(b"not the pinned worker")
    with pytest.raises(r.IdentityMismatch):
        r.verify_identity({}, rom, worker)


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


def test_mem_condition_reads_a_release_symbol():
    client = FakeClient()
    # FakeClient reports gBattleTypeFlags == 0 until a battle is scheduled.
    assert r.eval_condition(client, {"kind": "mem", "symbol": "gBattleTypeFlags", "value": 0}, {}) is True
    assert r.eval_condition(client, {"kind": "mem", "symbol": "gBattleTypeFlags", "nonzero": True}, {}) is False
    battle = FakeClient(battle_at=1)
    battle.frames = 10
    assert r.eval_condition(battle, {"kind": "mem", "symbol": "gBattleTypeFlags", "nonzero": True}, {}) is True


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
