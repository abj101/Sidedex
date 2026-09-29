"""Live release-ROM harness checks (skips cleanly when the artifacts are absent).

These tests are the only place the 528 KiB ROM / the worker are touched.  They
never modify the production path and never enter Git with a blob.
"""

from __future__ import annotations

import base64
import hashlib
import json
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


def _client(rom=ROM, expected_artifact=None):
    import rom_bridge as rb
    return rb.RomWorkerClient(
        rom_path=rom, expected_artifact_sha256=expected_artifact or r.PINNED_ROM_SHA256,
        worker_path=WORKER, expected_worker_sha256=r.PINNED_WORKER_SHA256, timeout=180.0)


def test_release_artifacts_are_pinned():
    assert r.sha256_file(ROM).upper() == r.PINNED_ROM_SHA256
    assert r.sha256_file(WORKER).upper() == r.PINNED_WORKER_SHA256
    if ELF.is_file():
        assert r.sha256_file(ELF).upper() == r.PINNED_ELF_SHA256


def test_h4_h5_ram_patch_and_iwram_write():
    """H4: patch EWRAM+IWRAM inside a savestate, reload, read back.  H5: IWRAM writes."""
    with _client() as client:
        client.run_frames(600)
        slot = client.save_checkpoint("release-h4")
        raw = base64.b64decode(client.export_state(slot["handle"])["blob"], validate=True)
        header, payload = r.header_payload(raw)
        e_addr, e_val = 0x02030000, 0xABCD1234
        main_rng = (0x55667788, 0x01020304, 0xAABBCCDD, 0x11223344)
        secondary_rng = (0xDEADBEEF, 0x10203040, 0x55667799, 0x88776655)
        i_addr, i_addr2 = r.RELEASE_MAP["gRngValue"], r.RELEASE_MAP["gRng2Value"]
        writes = {e_addr: e_val}
        writes.update({i_addr + 4 * i: value for i, value in enumerate(main_rng)})
        writes.update({i_addr2 + 4 * i: value for i, value in enumerate(secondary_rng)})
        blob = r.make_blob(raw, r.patch_payload(payload, writes))
        imported = client.import_state(base64.b64encode(blob).decode(), name="release-h4-patched")
        client.restore_state(imported["handle"])
        assert int.from_bytes(client.read_memory_block(e_addr, 4), "little") == e_val
        assert client.read_memory_block(i_addr, 16) == b"".join(
            value.to_bytes(4, "little") for value in main_rng)
        assert client.read_memory_block(i_addr2, 16) == b"".join(
            value.to_bytes(4, "little") for value in secondary_rng)
        # H5: direct IWRAM write through the worker op.
        direct_main, direct_secondary = bytes(range(16)), bytes(range(16, 32))
        client.write_memory(i_addr, direct_main)
        client.write_memory(i_addr2, direct_secondary)
        assert client.read_memory_block(i_addr, 16) == direct_main
        assert client.read_memory_block(i_addr2, 16) == direct_secondary
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


def _script_path() -> Path:
    return Path(r.__file__).with_name("release_input_script.json")


def test_release_cold_boot_route_captures_a_battle_root(tmp_path):
    """Route to the H3 choice boundary, then exercise projection + injection."""
    script = r.load_input_script()
    with _client() as client:
        try:
            out = r.run_route(client, script, milestones_dir=tmp_path, script_path=_script_path())
        except (r.ReleaseRootUnavailable, r.RouteStepError) as exc:
            pytest.skip(f"release root capture blocked: {exc}")
        assert out["frames"] > 0
        assert out["state"]["battle_type_flags"] != 0

        # C2.1: the root is the release-side choice boundary (kind 1).
        report = r.boundary_report(client)
        assert report["kind"] == r.BOUNDARY_CHOICE, report
        assert report["gBattleMainFunc"] == r.RELEASE_FUNCS["HandleTurnActionSelectionState"]
        assert report["gBattlerControllerFuncs"][0] == r.RELEASE_FUNCS["HandleInputChooseAction"]
        assert report["gBattleOutcome"] == 0

        # Task 3: read, write back and re-read gBattleMons, party and both RNG streams.
        roundtrip = r.verify_root_projection(client)
        assert all(v["write_landed"] and v["restored"] for v in roundtrip.values()), roundtrip
        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["root"]["sha256"] == out["root_sha256"]

        # C2.3: the projection uses the tables.json vocabulary and is self-consistent.
        tables = r.load_tables()
        first = r.release_projection(client)
        assert len(first["active"]) == 2 and len(first["party"]) == 12
        assert first["active"][0]["hp"] > 0 and first["active"][0]["species"] != 0
        for group in r.PROJECTION_GROUPS:
            read = {name for record in first[group] for name in record}
            assert read <= set(tables["fields"][group])
        comparison = r.compare_projections(first, r.release_projection(client))
        assert comparison["diffs"] == []
        assert comparison["excluded"] and all(e["reason"] for e in comparison["excluded"])

        slot = client.save_checkpoint("c2-self-comparison")
        try:
            client.run_frames(4)
            client.restore_state(slot["handle"])
            assert r.compare_projections(first, r.release_projection(client))["diffs"] == []
        finally:
            if hasattr(client, "drop_state"):
                client.drop_state(slot["handle"])

        # C2.2: a scenario's party/active/rng state round-trips; the rest is refused.
        scenario = {"root": {"player_party": [{"species": 25, "level": 42, "hp": 111,
                                               "max_hp": 130, "status1": 0, "ability": 9,
                                               "item": 13, "moves": [85, 98, 87, 86],
                                               "pp": [15, 20, 10, 20],
                                               "nature": "NATURE_JOLLY",
                                               "evs": [0, 252, 0, 0, 4, 0]}],
                                "opponent_party": [{"species": 143, "level": 40, "hp": 160,
                                                    "max_hp": 200, "status1": 0,
                                                    "ability": 17, "item": 0,
                                                    "moves": [33, 0, 0, 0],
                                                    "pp": [35, 0, 0, 0]}],
                                "rng_main": [1, 2, 3, 4], "rng_secondary": [5, 6, 7, 8],
                                "candidate_action": {"kind": "move"}}}
        injected = r.inject_scenario(client, scenario)
        assert injected["verified"] is True, injected
        assert set(injected["refused"]) == {"nature", "evs", "candidate_action"}
        assert injected["readback"]["active"][0]["species"] == 25
        assert injected["readback"]["rng"][0]["main_ctr"] == 4


def test_release_boundary_reaches_a_terminal(tmp_path):
    """C2.1: a real boundary change, choice -> terminal, on the release ROM.

    The captured wild battle cannot produce a forced switch (the player party is a
    single mon), so boundary 2 is covered by unit test only; a win is enough for
    the terminal boundary.
    """
    script = r.load_input_script()
    with _client() as client:
        out = r.run_route(client, script, milestones_dir=tmp_path, script_path=_script_path())
        before = r.boundary_report(client)
        assert before["kind"] == r.BOUNDARY_CHOICE, before
        # negative control: the terminal predicate is not trivially true
        assert before["gBattleOutcome"] == 0
        reached = None
        for tap in range(200):
            if r.detect_boundary_kind(client) == r.BOUNDARY_TERMINAL:
                reached = tap
                break
            client.set_keys(1)
            client.run_frames(8)
            client.set_keys(0)
            client.run_frames(16)
        client.set_keys(0)
        client.run_frames(120)
        after = r.boundary_report(client)
        assert reached is not None, f"no terminal after {out['frames']} frames: {after}"
        assert after["kind"] == r.BOUNDARY_TERMINAL, after
        assert after["gBattleOutcome"] in (1, 2, 3, 4, 5, 6, 7), after
        assert r.terminal_report(client)["opponent_faints"] >= 1, after


def test_release_route_root_is_reproducible(tmp_path):
    """Acceptance: two fresh cold-boot runs on the pinned .gba agree on the root SHA."""
    script = r.load_input_script()
    roots = []
    for i in range(2):
        with _client() as client:
            out = r.run_route(client, script, milestones_dir=tmp_path / f"run{i}",
                              script_path=_script_path())
            roots.append(out["root_sha256"])
            if i == 0:
                assert out["state"]["battle_type_flags"] != 0
    assert roots[0] == roots[1], roots
    if script.get("root_sha256"):
        assert roots[0].upper() == script["root_sha256"].upper()


def test_empty_valid_save_loads_but_is_not_a_playable_release_root(tmp_path):
    """Exercise the external-save loader without treating empty blocks as a root."""
    save_worker_value = os.environ.get("SIDEDEX_RELEASE_SAVE_WORKER")
    if not save_worker_value:
        pytest.skip("set SIDEDEX_RELEASE_SAVE_WORKER to a worker built with --save support")
    save_worker = Path(save_worker_value)
    if not save_worker.is_file():
        pytest.skip("SIDEDEX_RELEASE_SAVE_WORKER does not exist")
    save = tmp_path / "release-empty-valid.sav"
    fixture = r.write_empty_valid_save(save)
    import rom_bridge as rb
    with rb.RomWorkerClient(
        rom_path=ROM, worker_path=save_worker,
        expected_artifact_sha256=r.PINNED_ROM_SHA256,
        expected_worker_sha256=rb.sha256_file(save_worker),
        worker_args=["--save", str(save)], timeout=180.0,
    ) as client:
        assert client.status["save_bytes"] == fixture["bytes"]
        assert client.status["save_sha256"] == fixture["sha256"]
        client.run_frames(1200)
        assert client.read_memory(0x030062C0, 2) == 1  # SAVE_STATUS_OK
        assert client.read_memory(r.RELEASE_MAP["gPlayerPartyCount"], 1) == 0
        assert client.read_memory(r.RELEASE_MAP["gBattleTypeFlags"], 2) == 0


def test_release_state_identity_is_rejected_by_test_elf():
    """A release save blob must fail closed when presented to the test ELF."""
    if not ELF.is_file():
        pytest.skip("test ELF is missing")
    import rom_bridge as rb
    with _client(ROM) as release:
        release.run_frames(600)
        slot = release.save_checkpoint("release-identity")
        blob = release.export_state(slot["handle"])["blob"]
    with _client(ELF, r.PINNED_ELF_SHA256) as test_elf:
        with pytest.raises((rb.WorkerProtocolError, rb.WorkerResponseError), match="identity"):
            test_elf.import_state(blob, name="reject-release-identity")


def test_release_party_decrypt_and_reencrypt_round_trip(tmp_path):
    """C3: decode the root's party, write a re-encrypted edit back, re-read, restore."""
    with _client() as client:
        try:
            r.run_route(client, r.load_input_script(), milestones_dir=tmp_path,
                        script_path=_script_path())
        except (r.ReleaseRootUnavailable, r.RouteStepError) as exc:
            pytest.skip(f"release root capture blocked: {exc}")
        base = r.RELEASE_MAP["gPlayerParty"]
        original = client.read_memory_block(base, r.POKEMON_SIZE)
        mon = r.decode_party_mon(original)
        assert mon["checksum_ok"] is True
        # starter plausibility, cross-checked against the independent gBattleMons copy
        battle = client.read_memory_block(r.RELEASE_MAP["gBattleMons"], r.BATTLEMON_SIZE)
        assert mon["species"] == int.from_bytes(battle[:2], "little") != 0
        assert [mon[f"move{i}"] for i in range(1, 5)] == [
            int.from_bytes(battle[r.BATTLEMON["moves"] + 2 * i:][:2], "little") for i in range(4)]
        assert [mon[f"pp{i}"] for i in range(1, 5)] == list(
            battle[r.BATTLEMON["pp"]:r.BATTLEMON["pp"] + 4])
        assert original[r.POKEMON_LEVEL] == int.from_bytes(battle[r.BATTLEMON["level"]:][:1], "little")

        # decrypt -> edit pp1 in the plaintext substruct 1 -> fix checksum -> re-encrypt -> write
        personality = int.from_bytes(original[:4], "little")
        key = personality ^ int.from_bytes(original[4:8], "little")
        plain = bytearray(r._secure_xor(original[0x20:0x50], key))
        assert bytes(r._secure_xor(bytes(plain), key)) == original[0x20:0x50]   # symmetric cipher
        pp_at = r.SUBSTRUCT_SLOTS[personality % 24][1] * r.SUBSTRUCT_BYTES + 8
        assert plain[pp_at] == mon["pp1"]
        plain[pp_at] = mon["pp1"] - 1
        edited = bytearray(original)
        edited[0x20:0x50] = r._secure_xor(bytes(plain), key)
        edited[r.POKEMON_CHECKSUM:r.POKEMON_CHECKSUM + 2] = (
            (int.from_bytes(original[r.POKEMON_CHECKSUM:r.POKEMON_CHECKSUM + 2], "little") - 1)
            & 0xFFFF).to_bytes(2, "little")
        try:
            client.write_memory(base, bytes(edited))
            assert client.read_memory_block(base, r.POKEMON_SIZE) == bytes(edited)
            after = r.release_projection(client)["party"][0]
            assert after["pp1"] == mon["pp1"] - 1
            assert {k: v for k, v in after.items() if k not in ("pp1", "side", "slot", "level", "hp",
                                                                   "max_hp", "status1", "present")} == {
                k: v for k, v in mon.items() if k not in ("pp1", "checksum_ok")}
        finally:
            client.write_memory(base, original)
        assert client.read_memory_block(base, r.POKEMON_SIZE) == original
        assert r.decode_party_mon(client.read_memory_block(base, r.POKEMON_SIZE)) == mon

        proj = r.release_projection(client)
        assert proj["party"][0]["species"] == mon["species"]
        assert set(proj["excluded"]["party"]) == {"ability", "form"}
        # no battle timers are running at the root; all zero by construction of the battle start
        assert all(proj["side"][s][k] == 0 for s in (0, 1) for k in r.SIDE_TIMER)
        assert proj["field"][0]["weather_timer"] == 0 and proj["battle"][0]["ai_flags"] == 0
