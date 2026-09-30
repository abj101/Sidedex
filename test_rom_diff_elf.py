"""Phase-4 ELF-pair differential matrix (package E).

Runs every green fixture from ``tools/rom_oracle/fixtures/`` under
``mode=normal`` and ``mode=logic-only`` from the same saved decision node and
requires the contract section 2.2 projection, mechanical event list,
boundary/terminal record, both RNG streams and the raw game-state RAM to be
identical.  The matrix is pinned to the wave-2 v8c ELF
(``pokeemerald-test-headless.elf``); the older v8 / v8b ELFs are historical and
are only exercised with ``SIDEDEX_DIFF_HISTORICAL=1``.  The non-fixture checks
(AI leakage, restore determinism, state-token identity rejection, query
immutability) run against the pinned ELF.

Generated binaries and savestate blobs never enter Git.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.rom_oracle import rom_diff_harness as h  # noqa: E402
from tools.rom_oracle.rom_diff_harness import DiffSession, HarnessError  # noqa: E402


def _iter_elfs():
    """The pinned v8c ELF (wave-2). v8/v8b are historical and opt-in only."""
    elfs = [h.DEFAULT_V8_ELF] if h.DEFAULT_V8_ELF.is_file() else []
    if os.environ.get("SIDEDEX_DIFF_HISTORICAL") == "1":
        elfs.extend(p for p in (h.HISTORICAL_V8_ELF, h.HISTORICAL_V8B_ELF) if p.is_file())
    if not elfs:
        pytest.skip("pinned differential ELF artifact is missing", allow_module_level=True)
    if not h.DEFAULT_WORKER.is_file():
        pytest.skip("rom_worker.exe is missing", allow_module_level=True)
    return elfs


ELFS = _iter_elfs()
ELF_IDS = [path.stem for path in ELFS]
ALL_FIXTURES = h.load_fixtures()
GREEN = sorted((data for _, data in ALL_FIXTURES if data.get("status") == "green-target"),
               key=lambda data: int(data["row"][1:]))
UNSUPPORTED = [data for _, data in ALL_FIXTURES if data.get("status") == "unsupported"]


def _root_request(moves=("MOVE_TACKLE", "MOVE_GROWL", "MOVE_HYDRO_PUMP", "MOVE_SLASH"),
                  pp=(35, 40, 5, 20)):
    sample = json.loads((h.TOOLS / "dynamic_sample.json").read_text(encoding="utf-8"))
    request = copy.deepcopy(sample)
    request["player_party"][0]["moves"] = list(moves)
    request["player_party"][0]["pp"] = list(pp)
    request["candidate_action"] = {"kind": "move", "move_id": moves[0]}
    return request


def _seed_pair(seed):
    return ([seed * 2654435761 % 2**32, 0x1122, 0x3344, 7],
            [seed, seed * 7 + 1, seed * 13 + 2, seed * 17 + 3])


def _hp_delta(records, battler):
    return [int(e.split("|")[4]) - int(e.split("|")[5]) for e in records
            if e.startswith("ROMTRACE|hp|") and e.split("|")[3] == battler]


def _trainer_action(records, turn=0):
    return [e for e in records if e.startswith(f"ROMTRACE|action|{turn}|1|")]


def _check_expect(fixture, result):
    expect = result.get("expected") or {}
    stop = result["normal"]["stop"]
    if expect.get("stop") == "boundary":
        assert stop is not None and stop.startswith("ROMTRACE|boundary|"), (fixture["id"], stop)
        if expect.get("boundary_kind"):
            assert stop.split("|")[2] == expect["boundary_kind"], stop
    if expect.get("stop") == "terminal":
        assert stop == f"ROMTRACE|terminal|{expect['result']}|{expect['reason']}", stop
    if expect.get("target_hp_unchanged"):
        assert not _hp_delta(result["normal"]["records"], "1"), result["normal"]["records"]
    if expect.get("crit"):
        damage = _hp_delta(result["normal"]["records"], "1")
        assert damage and damage[0] >= 17, (fixture["id"], damage)
    if expect.get("faint"):
        assert any(e.startswith("ROMTRACE|faint|") for e in result["normal"]["records"])
    if expect.get("no_faint"):
        assert not any(e.startswith("ROMTRACE|faint|") for e in result["normal"]["records"])
    if expect.get("target_damage_min") is not None:
        damage = _hp_delta(result["normal"]["records"], "1")
        assert sum(damage) >= expect["target_damage_min"], (fixture["id"], damage)
    if expect.get("target_damage_max") is not None:
        damage = _hp_delta(result["normal"]["records"], "1")
        assert damage and sum(damage) <= expect["target_damage_max"], (fixture["id"], damage)
    if expect.get("target_final_hp") is not None:
        finals = [int(e.split("|")[5]) for e in result["normal"]["records"]
                  if e.startswith("ROMTRACE|hp|0|1|")]
        assert finals and finals[-1] == expect["target_final_hp"], (fixture["id"], finals)
    if expect.get("status_record"):
        assert expect["status_record"] in result["normal"]["records"], result["normal"]["records"]
    if expect.get("ability_record") is not None:
        want = f"ROMTRACE|ability|0|1|{expect['ability_record']}"
        assert want in result["normal"]["records"], (fixture["id"], result["normal"]["records"])
    if expect.get("unrepresentable"):
        want = f"ROMTRACE|unrepresentable|{expect['unrepresentable']}"
        assert want in result["normal"]["records"], (fixture["id"], result["normal"]["records"])
    if expect.get("hits_between"):
        hits = len([e for e in result["normal"]["records"] if e.startswith("ROMTRACE|hp|0|1|")])
        lo, hi = expect["hits_between"]
        assert lo <= hits <= hi, (fixture["id"], hits)


@pytest.mark.parametrize("elf", ELFS, ids=ELF_IDS)
@pytest.mark.parametrize("fixture", GREEN, ids=[f["id"] for f in GREEN])
def test_matrix_row(elf, fixture):
    session = DiffSession(elf=elf)
    try:
        run = h.run_fixture(session, fixture)
    finally:
        session.close()
    for result in run["actions"]:
        assert result["diffs"] == [], result["diffs"]
        _check_expect(fixture, result)
    if fixture["id"] == "P5":
        records = run["actions"][0]["normal"]["records"]
        moves = [e.split("|")[5] for e in records if e.startswith("ROMTRACE|action|0|0|")]
        assert moves and moves[0] == "56", moves  # MOVE_HYDRO_PUMP was chosen
    if fixture["id"] == "P29":
        projections = ["\n".join(h.projection(r["normal"]["records"])) for r in run["actions"]]
        assert len(set(projections)) == len(projections), "branches must differ"


def test_every_matrix_row_is_named():
    """No row deleted, no row silently skipped: P1..P32 each green or unsupported."""
    seen = {}
    for _, data in ALL_FIXTURES:
        seen.setdefault(data["id"], []).append(data)
    for number in range(1, 33):
        row = f"P{number}"
        assert row in seen, f"{row} is missing from the fixture set"
        assert len(seen[row]) == 1, f"{row} is duplicated"
        data = seen[row][0]
        assert data["status"] in ("green-target", "unsupported"), (row, data["status"])
        if data["status"] == "unsupported":
            assert data.get("reason"), row
            if data["reason"] == "unsupported-mechanic":
                assert data.get("rom_record"), f"{row} must name the ROM record"


@pytest.mark.parametrize("elf", ELFS, ids=ELF_IDS)
def test_ai_leakage(elf):
    """Same pre-choice token + identical RNG: the trainer's move choice must not depend
    on the player's unrevealed move."""
    session = DiffSession(elf=elf)
    try:
        root = _root_request()
        session.build_root(root)
        injected = _seed_pair(21)
        choices = {}
        for move in ("MOVE_TACKLE", "MOVE_GROWL", "MOVE_SLASH"):
            branch = copy.deepcopy(root)
            branch["candidate_action"] = {"kind": "move", "move_id": move}
            result = session.transition(branch, mode="normal", policy="normal",
                                        inject_rng=injected)
            choices[move] = tuple(_trainer_action(result["records"]))
        assert all(choices.values()), choices
        assert len(set(choices.values())) == 1, choices
    finally:
        session.close()


@pytest.mark.parametrize("elf", ELFS, ids=ELF_IDS)
def test_restore_determinism(elf):
    """Restoring the same token twice, and after another branch, reproduces the decoded
    projection, both RNG streams and the game-state RAM byte-for-byte."""
    session = DiffSession(elf=elf)
    try:
        root = _root_request()
        session.build_root(root)
        root_blob = session.client.export_state(session.root_slot["handle"])["blob"]
        tackle = {**root, "candidate_action": {"kind": "move", "move_id": "MOVE_TACKLE"}}
        growl = {**root, "candidate_action": {"kind": "move", "move_id": "MOVE_GROWL"}}

        first = session.transition(tackle, mode="normal", policy="normal")
        first["state"] = session.slot_digest()
        session.transition(growl, mode="normal", policy="normal")
        second = session.transition(tackle, mode="normal", policy="normal")
        second["state"] = session.slot_digest()

        assert h.projection(first["records"]) == h.projection(second["records"])
        assert first["rng"] == second["rng"]
        assert first["state"]["ram_windows_sha256"] == second["state"]["ram_windows_sha256"]
        # the source token itself never changes
        assert session.client.export_state(session.root_slot["handle"])["blob"] == root_blob
    finally:
        session.close()


@pytest.mark.parametrize("elf", ELFS, ids=ELF_IDS)
def test_query_immutability(elf):
    """QUERY_DAMAGE / QUERY_AI_ACTION leave the source token untouched and resolve no turn."""
    session = DiffSession(elf=elf)
    try:
        root = _root_request()
        session.build_root(root)
        handle = session.root_slot["handle"]
        blob_before = session.client.export_state(handle)["blob"]
        control = session.transition({**root, "candidate_action":
                                      {"kind": "move", "move_id": "MOVE_TACKLE"}},
                                     mode="normal", policy="normal")
        for operation in ("query-damage", "query-ai-action"):
            session.client.restore_state(handle)
            records, frames, _ = session._submit(
                root, operation=operation, mode="normal", policy="normal",
                want=["ROMTRACE|query|complete"], final="ROMTRACE|complete|0")
            assert "ROMTRACE|query|complete" in records, (operation, records)
            assert "ROMTRACE|complete|0" in records, (operation, records)
            assert not any(e.startswith(("ROMTRACE|hp|", "ROMTRACE|faint|", "ROMTRACE|boundary|"))
                           for e in records), (operation, records)
        assert session.client.export_state(handle)["blob"] == blob_before
        replay = session.transition({**root, "candidate_action":
                                     {"kind": "move", "move_id": "MOVE_TACKLE"}},
                                    mode="normal", policy="normal")
        assert h.projection(replay["records"]) == h.projection(control["records"])
    finally:
        session.close()


@pytest.mark.parametrize("elf", ELFS, ids=ELF_IDS)
def test_state_token_identity_rejection(elf):
    """A blob minted under a different artifact / wire protocol / save format / worker is
    rejected with identity-mismatch; a dropped handle returns stale-handle."""
    artifact = h.sha256_bytes(Path(elf).read_bytes()).lower()
    with h.RawWorker(elf=elf) as worker:
        slot = worker.send(op="save_state", artifact_sha256=artifact)["slot"]
        handle = slot["handle"]
        wrong = worker.send(op="load_state", handle=handle, artifact_sha256="00" * 32)
        assert wrong.get("error") == "identity-mismatch", wrong
        exported = worker.send(op="export_state", handle=handle, artifact_sha256=artifact)
        raw = bytearray(base64.b64decode(exported["blob"]))
        for label, offset, value in (("wire_protocol", 12, 1), ("artifact", 24, 0xFF),
                                     ("save_format", 88, ord("X")),
                                     ("worker_version", 152, ord("X")),
                                     ("mgba_version", 184, ord("X"))):
            mutated = bytearray(raw)
            mutated[offset] = value
            reply = worker.send(op="import_state", name=f"mut-{label}", artifact_sha256=artifact,
                                blob=base64.b64encode(bytes(mutated)).decode())
            assert reply.get("error") == "identity-mismatch", (label, reply)
        malformed = bytearray(raw)
        malformed[0] ^= 0xFF  # header magic
        reply = worker.send(op="import_state", name="malformed", artifact_sha256=artifact,
                            blob=base64.b64encode(bytes(malformed)).decode())
        assert reply.get("error") == "incomplete-state", reply
        assert worker.send(protocol=1, op="status").get("error") == "invalid-request"
        dropped = worker.send(op="drop_state", handle=handle)
        assert dropped.get("dropped") is True, dropped
        stale = worker.send(op="load_state", handle=handle, artifact_sha256=artifact)
        assert stale.get("error") == "stale-handle", stale


def test_mode_and_policy_identity_rejection():
    """Contract section 1: mode and critical policy are part of the token identity."""
    identity = h.mint_token_identity(
        source_sha=h.ID_MAP["source_sha"], artifact_sha256=h.sha256_bytes(
            Path(ELFS[0]).read_bytes()).lower(), worker_sha256="0" * 64,
        wire_protocol=h.WIRE_PROTOCOL, rom_hook_protocol=h.ROM_HOOK_PROTOCOL,
        state_schema=h.STATE_SCHEMA,
        mode="normal", critical_policy="normal")
    h.check_token_identity(identity, mode="normal", critical_policy="normal")
    with pytest.raises(h.IdentityMismatch):
        h.check_token_identity(identity, mode="logic-only")
    with pytest.raises(h.IdentityMismatch):
        h.check_token_identity(identity, critical_policy="item-ability-only")
    with pytest.raises(h.IdentityMismatch):
        h.check_token_identity(identity, artifact_sha256="f" * 64)


def test_fixture_gate_identity_is_pinned():
    """Every fixture still carries the frozen wave-2 gate block it was recorded with.

    This is provenance, not the active pin: Gate V1 flipped the worker to wire 3 and
    the fixtures are evidence, so ``h.FIXTURE_GATE_IDENTITY`` is deliberately fixed.
    """
    for _, data in ALL_FIXTURES:
        gate = data.get("gate")
        assert gate, f"{data['id']} is missing its gate block"
        for field, expected in h.FIXTURE_GATE_IDENTITY.items():
            assert gate.get(field) == expected, (data["id"], field, gate.get(field))


def test_pinned_artifact_hashes():
    """Fail closed if the on-disk pinned artifacts drift from the recorded gate."""
    elf = h.DEFAULT_V8_ELF
    worker = h.DEFAULT_WORKER
    assert h.sha256_bytes(elf.read_bytes()) == h.ELF_SHA256, elf
    assert h.sha256_bytes(worker.read_bytes()).lower() == h.WORKER_SHA256.lower(), worker
