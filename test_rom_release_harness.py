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
    assert script["root_settle"]["until"] == {"kind": "boundary", "value": r.BOUNDARY_CHOICE}
    assert script["root_settle"]["keys"]
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


# ---------------------------------------------------------------- C2: H3 + projection

class ProjectionClient:
    """Byte-addressable RAM double: projection and injection round-trip offline."""

    def __init__(self):
        self.ram = {}
        self.frames = 0
        self.keys = 0

    def write_memory(self, address, data):
        for offset, value in enumerate(bytes(data)):
            self.ram[address + offset] = value

    def read_memory_block(self, address, length):
        return bytes(self.ram.get(address + i, 0) for i in range(length))

    def read_memory(self, address, width):
        return int.from_bytes(self.read_memory_block(address, width), "little")

    def set_keys(self, keys):
        self.keys = keys

    def run_frames(self, frames, keys=None):
        self.frames += frames
        return {"trace_events": []}


def _boundary_client(outcome=0, main="DoBattleIntro", ctrl="Controller_WaitForString", mon_hp=20):
    client = ProjectionClient()
    client.write_memory(r.RELEASE_MAP["gBattleOutcome"], int(outcome).to_bytes(2, "little"))
    client.write_memory(r.RELEASE_MAP["gBattleMainFunc"], (r.RELEASE_FUNCS[main] | 1).to_bytes(4, "little"))
    client.write_memory(r.RELEASE_MAP["gBattlerControllerFuncs"],
                        (r.RELEASE_FUNCS[ctrl] | 1).to_bytes(4, "little"))
    client.write_memory(r.RELEASE_MAP["gBattleMons"] + r.BATTLEMON["hp"], int(mon_hp).to_bytes(2, "little"))
    return client


def test_identity_is_read_from_rom_identity():
    import rom_identity as ri
    assert r.PINNED_ROM_SHA256 == ri.ROM_SHA256
    assert r.PINNED_ELF_SHA256 == ri.ENGINE_ELF_SHA256
    assert r.PINNED_WORKER_SHA256 == ri.WORKER_SHA256
    assert r.PINNED_SOURCE_SHA == ri.SOURCE_SHA
    script_raw = r.load_input_script_raw()
    scenarios_raw = r.load_scenarios_raw()
    for digest in (ri.ROM_SHA256, ri.ENGINE_ELF_SHA256, ri.WORKER_SHA256):
        assert digest.lower() not in json.dumps(script_raw).lower()
        assert digest.lower() not in json.dumps(scenarios_raw).lower()
    assert "<<rom_identity.ROM_SHA256>>" in json.dumps(script_raw)
    script = r.load_input_script()
    assert script["rom_sha256"] == ri.ROM_SHA256
    assert script["worker_sha256"] == ri.WORKER_SHA256
    scenarios = r.load_scenarios()
    assert scenarios["release_rom_sha256"] == ri.ROM_SHA256
    assert scenarios["worker_sha256"] == ri.WORKER_SHA256
    assert scenarios["elf_sha256"] == ri.ENGINE_ELF_SHA256


def test_unknown_identity_placeholder_fails_closed():
    with pytest.raises(r.ReleaseHarnessError):
        r.resolve_identity("<<rom_identity.NOT_A_REAL_PIN>>")
    assert r.resolve_identity({"a": ["<<rom_identity.ROM_SHA256>>"]})["a"] == [r.PINNED_ROM_SHA256]


def test_boundary_detection_kinds():
    assert r.detect_boundary_kind(_boundary_client()) == r.BOUNDARY_NONE
    assert r.detect_boundary_kind(_boundary_client(
        main="HandleTurnActionSelectionState", ctrl="HandleInputChooseAction")) == r.BOUNDARY_CHOICE
    assert r.detect_boundary_kind(_boundary_client(main="HandleFaintedMonActions")) == r.BOUNDARY_FORCED_SWITCH
    assert r.detect_boundary_kind(_boundary_client(
        ctrl="PlayerHandleChoosePokemon", mon_hp=0)) == r.BOUNDARY_FORCED_SWITCH
    assert r.detect_boundary_kind(_boundary_client(outcome=1)) == r.BOUNDARY_TERMINAL
    # negative control: the party menu on a live mon is not a forced switch
    assert r.detect_boundary_kind(_boundary_client(
        ctrl="PlayerHandleChoosePokemon", mon_hp=20)) == r.BOUNDARY_NONE
    report = r.boundary_report(_boundary_client(
        main="HandleTurnActionSelectionState", ctrl="HandleInputChooseAction"))
    assert report["name"] == "choice" and "HandleInputChooseAction" in report["reason"]
    assert r.eval_condition(_boundary_client(
        main="HandleTurnActionSelectionState", ctrl="HandleInputChooseAction"),
        {"kind": "boundary", "value": 1}, {}) is True


def test_projection_reads_every_tables_json_field_or_excludes_it():
    tables = r.load_tables()
    assert set(r.PROJECTION_GROUPS) <= set(tables["fields"])
    projection = r.release_projection(ProjectionClient())
    for group in r.PROJECTION_GROUPS:
        expected = set(tables["fields"][group])
        read = {name for record in projection[group] for name in record}
        excluded = set(projection["excluded"][group])
        assert read <= expected and excluded <= expected
        assert read | excluded == expected, (group, sorted(expected - read - excluded))
        assert read & excluded == set(), group
        assert all(reason for reason in projection["excluded"][group].values())
    assert len(projection["active"]) == 2 and len(projection["side"]) == 2
    assert len(projection["party"]) == 12 and len(projection["rng"]) == 1


def test_compare_projections_detects_a_field_difference_on_the_golden_vector():
    vector = json.loads((r.SDR2_VECTORS / "choice_p1.json").read_text(encoding="utf-8"))
    normal = vector["records"]
    release = {group: normal[group] for group in r.PROJECTION_GROUPS}
    release["excluded"] = {}
    assert r.compare_projections(release, normal)["diffs"] == []
    changed = dict(release)
    changed["active"] = [dict(normal["active"][0], hp=999), normal["active"][1]]
    assert r.compare_projections(changed, normal)["diffs"] == [
        {"group": "active", "index": 0, "field": "hp", "release": 999, "normal": 100}]
    changed = dict(release)
    changed["rng"] = [dict(normal["rng"][0], main_ctr=7)]
    diffs = r.compare_projections(changed, normal)["diffs"]
    assert len(diffs) == 1 and diffs[0]["field"] == "main_ctr", diffs


def test_projection_self_comparison_is_identical_and_exclusions_carry_reasons():
    client = ProjectionClient()
    result = r.compare_projections(r.release_projection(client), r.release_projection(client))
    assert result["diffs"] == []
    assert result["excluded"], "an all-readable projection would hide unreadable fields"
    assert all(entry["reason"] for entry in result["excluded"])
    for entry in result["excluded"]:
        assert entry["group"] in r.PROJECTION_GROUPS
        assert entry["field"] in r.load_tables()["fields"][entry["group"]]


def _scenario_row():
    return {"player_party": [{"species": 25, "level": 42, "hp": 111, "max_hp": 130, "status1": 0,
                              "ability": 9, "item": 13, "moves": [85, 98, 87, 86],
                              "pp": [15, 20, 10, 20], "nature": "NATURE_JOLLY",
                              "evs": [0, 252, 0, 0, 4, 0], "ivs": [31] * 6}],
            "opponent_party": [{"species": 143, "level": 40, "hp": 160, "max_hp": 200,
                                "status1": 0, "ability": 17, "item": 0, "moves": [33, 0, 0, 0],
                                "pp": [35, 0, 0, 0]}],
            "rng_main": [1, 2, 3, 4], "rng_secondary": [5, 6, 7, 8],
            "candidate_action": {"kind": "move"}}


def test_inject_scenario_round_trips_and_refuses_unrepresentable_fields():
    result = r.inject_scenario(ProjectionClient(), {"root": _scenario_row()})
    assert result["verified"] is True
    assert set(result["refused"]) == {"nature", "evs", "ivs", "candidate_action"}
    assert all(reason for reason in result["refused"].values())
    got = result["readback"]["active"][0]
    assert got["species"] == 25 and got["hp"] == 111 and got["max_hp"] == 130
    assert [got[f"move{i + 1}"] for i in range(4)] == [85, 98, 87, 86]
    assert [got[f"pp{i + 1}"] for i in range(4)] == [15, 20, 10, 20]
    assert result["readback"]["rng"][0] == {"main_a": 1, "main_b": 2, "main_c": 3, "main_ctr": 4,
                                            "sec_a": 5, "sec_b": 6, "sec_c": 7, "sec_ctr": 8}
    # a scenario without a field refuses it by name instead of writing a zero
    sparse = {"root": {"player_party": [{"species": 1}], "rng_main": [1, 2, 3]}}
    refused = r.inject_scenario(ProjectionClient(), sparse)["refused"]
    assert "rng_main" in refused and "expected 4 rng words" in refused["rng_main"]


# ------------------------------------------------------------ C3: party decrypt + timers

def _hand_party_mon(personality, ot_id, *, species=4, item=13, moves=(10, 45, 206, 0),
                    pps=(35, 40, 40, 0), evs=(1, 2, 3, 4, 5, 6), ivs=(31, 30, 29, 28, 27, 26),
                    hidden_nature=0):
    """Independent encoder: packs the four plaintext substructs, rotates and encrypts them."""
    sub0 = bytearray(12)
    sub0[0:2] = species.to_bytes(2, "little")            # species:11, teraType:5
    sub0[2:4] = item.to_bytes(2, "little")               # heldItem:10
    sub1 = bytearray(12)
    for i, move in enumerate(moves):
        sub1[2 * i:2 * i + 2] = move.to_bytes(2, "little")
        sub1[8 + i] = pps[i]
    sub2 = bytearray(12)
    sub2[0:6] = bytes(evs)
    sub3 = bytearray(12)
    packed = sum(iv << (5 * i) for i, iv in enumerate(ivs))
    sub3[4:8] = packed.to_bytes(4, "little")
    order = {  # personality % 24 == 7 -> slots (1, 0, 3, 2); == 0 -> (0, 1, 2, 3)
        0: (0, 1, 2, 3), 7: (1, 0, 3, 2), 23: (3, 2, 1, 0)}[personality % 24]
    plain = bytearray(48)
    for kind, sub in enumerate((sub0, sub1, sub2, sub3)):
        plain[order[kind] * 12:(order[kind] + 1) * 12] = sub
    checksum = sum(int.from_bytes(plain[i:i + 2], "little") for i in range(0, 48, 2)) & 0xFFFF
    key = personality ^ ot_id
    secure = b"".join((int.from_bytes(plain[i:i + 4], "little") ^ key).to_bytes(4, "little")
                      for i in range(0, 48, 4))
    raw = bytearray(0x64)
    raw[0:4] = personality.to_bytes(4, "little")
    raw[4:8] = ot_id.to_bytes(4, "little")
    raw[0x12] = hidden_nature << 3
    raw[0x13] = 0x02                                       # hasSpecies
    raw[0x1C:0x1E] = checksum.to_bytes(2, "little")
    raw[0x20:0x50] = secure
    raw[0x54], raw[0x56], raw[0x58] = 5, 20, 20
    return bytes(raw)


@pytest.mark.parametrize("personality", [0x12345600, 0x12345607 + 24 * 3, 0x1234560B + 12])
def test_party_decode_matches_hand_encrypted_struct(personality):
    ot_id = 0xA1B2C3D4
    assert personality % 24 in (0, 7, 23), personality % 24
    raw = _hand_party_mon(personality, ot_id, hidden_nature=2)
    got = r.decode_party_mon(raw)
    assert got["checksum_ok"] is True
    assert (got["species"], got["item"]) == (4, 13)
    assert [got[f"move{i}"] for i in range(1, 5)] == [10, 45, 206, 0]
    assert [got[f"pp{i}"] for i in range(1, 5)] == [35, 40, 40, 0]
    assert [got[f"ev_{n}"] for n in r.IV_ORDER] == [1, 2, 3, 4, 5, 6]
    assert [got[f"iv_{n}"] for n in r.IV_ORDER] == [31, 30, 29, 28, 27, 26]
    assert got["nature"] == (personality % 25) ^ 2


def test_party_decode_negative_controls():
    personality, ot_id = 0x12345607 + 24 * 3, 0xA1B2C3D4
    raw = bytearray(_hand_party_mon(personality, ot_id))
    # wrong key (otId only, the vanilla-checksum style mistake): must not decode
    wrong = bytearray(raw)
    wrong[0:4] = (personality ^ 0xFFFF).to_bytes(4, "little")
    bad = r.decode_party_mon(bytes(wrong))
    assert bad["checksum_ok"] is False and (bad["species"], bad["move1"]) != (4, 10)
    # right key but wrong rotation (personality % 24 differs): fields land in the wrong slots
    rotated = bytearray(raw)
    key = personality ^ ot_id
    rotated[0:4] = (personality + 1).to_bytes(4, "little")
    rotated[0x20:0x50] = b"".join(
        ((int.from_bytes(raw[0x20 + i:0x24 + i], "little") ^ key ^ (personality + 1) ^ ot_id)
         ).to_bytes(4, "little") for i in range(0, 48, 4))
    wrong_order = r.decode_party_mon(bytes(rotated))
    assert (wrong_order["species"], wrong_order["move1"]) != (4, 10)
    # a flipped ciphertext bit is caught by the checksum
    raw[0x22] ^= 0x01
    assert r.decode_party_mon(bytes(raw))["checksum_ok"] is False


def test_projection_reads_party_timers_and_ai_flags():
    client = ProjectionClient()
    client.write_memory(r.RELEASE_MAP["gPlayerPartyCount"], b"\x01")
    client.write_memory(r.RELEASE_MAP["gPlayerParty"], _hand_party_mon(0x12345607, 0x11223344))
    for off, name in ((0, "reflect_timer"), (2, "light_screen_timer"), (4, "mist_timer"),
                      (6, "safeguard_timer"), (14, "aurora_veil_timer"), (16, "tailwind_timer"),
                      (21, "follow_me_timer")):
        client.write_memory(r.RELEASE_MAP["gSideTimers"] + 32 + off, bytes([off + 1]))
    client.write_memory(r.RELEASE_MAP["gFieldTimers"] + 4, b"\x03")
    client.write_memory(r.RELEASE_MAP["gFieldTimers"] + 5, b"\x04")
    client.write_memory(r.RELEASE_MAP["gWishFutureKnock"] + 28, b"\x05")
    # gBattleResources -> ai -> aiFlags[1]
    client.write_memory(r.RELEASE_MAP["gBattleResources"], (0x02010000).to_bytes(4, "little"))
    client.write_memory(0x02010000 + 0x14, (0x02020000).to_bytes(4, "little"))
    client.write_memory(0x02020000 + 0x18 + 4, (0x20000000).to_bytes(4, "little"))
    proj = r.release_projection(client)
    mon = proj["party"][0]
    assert (mon["present"], mon["species"], mon["move3"], mon["iv_spd"]) == (1, 4, 206, 26)
    assert proj["party"][1]["present"] == 0 and proj["party"][1]["slot"] == 1
    assert {"ability", "form"} == set(proj["excluded"]["party"])
    assert [proj["field"][0][k] for k in ("weather_timer", "terrain_timer", "trick_room_timer")] == [5, 4, 3]
    side1 = proj["side"][1]
    assert [side1[k] for k in ("reflect_timer", "light_screen_timer", "mist_timer", "safeguard_timer",
                               "aurora_veil_timer", "tailwind_timer", "follow_me_timer")] == [1, 3, 5, 7, 15, 17, 22]
    assert all(proj["side"][0][k] == 0 for k in r.SIDE_TIMER)
    assert proj["battle"][0]["ai_flags"] == 0x20000000
    assert "ai_flags" not in proj["excluded"]["battle"]
    # negative control: no AI struct allocated -> excluded with a reason, never 0
    empty = r.release_projection(ProjectionClient())
    assert "ai_flags" not in empty["battle"][0] and empty["excluded"]["battle"]["ai_flags"]


def test_projection_refuses_a_corrupt_party_mon():
    client = ProjectionClient()
    raw = bytearray(_hand_party_mon(0x12345607, 0x11223344))
    raw[0x30] ^= 0xFF
    client.write_memory(r.RELEASE_MAP["gPlayerPartyCount"], b"\x01")
    client.write_memory(r.RELEASE_MAP["gPlayerParty"], bytes(raw))
    with pytest.raises(r.ReleaseHarnessError, match="checksum"):
        r.release_projection(client)
