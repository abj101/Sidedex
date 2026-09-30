"""Live behavior fixtures for the pinned dynamic ROM oracle."""
import copy
import dataclasses
import hashlib
import json
import struct
import time
from pathlib import Path

import pytest

import rom_bridge
import rom_query
from tools.rom_oracle import run_dynamic


ROOT = Path(__file__).parent
ELF = ROOT / "native/rom_worker/build/mingw/pokeemerald-test-headless.elf"
V8_ELF = ROOT / "native/rom_worker/build/mingw/pokeemerald-test-v8.elf"
V8B_ELF = ROOT / "native/rom_worker/build/mingw/pokeemerald-test-v8b.elf"
BASE = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
DATA = json.loads((ROOT / "data/game_data.json").read_text(encoding="utf-8"))


def _staged_or_pinned_v8():
    """The v8 ELF under test: the pin once it flips, else the newest staged artifact."""
    if run_dynamic.PINNED_PROTOCOL_VERSION >= 8:
        return ELF, run_dynamic.PIN["dynamic_oracle"]["headless_elf_sha256"], run_dynamic.PINNED_PROTOCOL_VERSION
    for staged in (V8B_ELF, V8_ELF):
        if staged.is_file():
            return staged, hashlib.sha256(staged.read_bytes()).hexdigest(), 8
    return None


def _v8_request(moves=None, pp=None, opponent_hp=None):
    request = copy.deepcopy(BASE)
    if moves is not None:
        request["player_party"][0]["moves"] = moves
        request["player_party"][0]["pp"] = pp
    if opponent_hp is not None:
        request["opponent_party"][0]["hp"] = opponent_hp
    return request


class _QuerySession:
    """One worker booted to the pre-choice checkpoint, exposing a token."""

    def __init__(self, elf, sha, version):
        self.version = version
        self.client = rom_bridge.RomWorkerClient(
            rom_path=elf, expected_artifact_sha256=sha, timeout=60.0)
        self.client.__enter__()
        trace = []
        self.mailbox = None
        for _ in range(400):
            trace.extend(run_dynamic._trace(self.client.run_frames(60)))
            ready = [line for line in trace if line.startswith("ROMTRACE|ready|")]
            if ready:
                self.mailbox = rom_bridge.parse_v9_ready(ready)  # the ROM-announced buffers
                break
        if self.mailbox is None:
            self.client.__exit__(None, None, None)
            raise RuntimeError("ELF never announced its mailbox")

    def token(self, request):
        slot = self.client.save_checkpoint()
        identity = {"test_elf_sha256": self.client.rom_sha256,
                    "worker_sha256": self.client.worker_sha256,
                    "rom_hook_protocol": self.version}
        return rom_query.token_from_checkpoint(slot, request, identity=identity)

    def close(self):
        self.client.__exit__(None, None, None)


def _query(edit=None):
    if not ELF.is_file() or not rom_bridge.DEFAULT_WORKER.is_file():
        pytest.skip("pinned ROM test ELF or mGBA worker is not installed")
    request = copy.deepcopy(BASE)
    if edit is not None:
        edit(request)
    request["query"] = True
    trace = run_dynamic.run_turn(
        ELF, run_dynamic.PIN["dynamic_oracle"]["headless_elf_sha256"], request
    )
    assert "ROMTRACE|critical-policy|item-ability-only|battle-script-filtered" in trace
    return trace


def _damage(trace, side, move_id):
    rows = {}
    for event in trace:
        cells = event.split("|")
        if cells[:3] == ["ROMTRACE", "query", "damage"]:
            rows[(int(cells[3]), int(cells[4]))] = {
                "min": int(cells[8]),
                "max": int(cells[9]),
                "crit_denominator": int(cells[11]),
            }
    return rows[(side, run_dynamic.numeric_id("moves", move_id))]


def test_live_held_item_toggle_is_authoritative_for_both_sides():
    baseline = _query()

    def player_item(enabled):
        def edit(request):
            mon = request["player_party"][0]
            mon.update(item="ITEM_LIFE_ORB", held_item_enabled=enabled)
        return edit

    def enemy_item(enabled):
        def edit(request):
            mon = request["opponent_party"][0]
            mon.update(item="ITEM_LIFE_ORB", held_item_enabled=enabled)
        return edit

    player_on = _query(player_item(True))
    player_off = _query(player_item(False))
    enemy_on = _query(enemy_item(True))
    enemy_off = _query(enemy_item(False))

    base_player = _damage(baseline, 0, "MOVE_TACKLE")
    assert _damage(player_on, 0, "MOVE_TACKLE")["min"] > base_player["min"]
    assert _damage(player_off, 0, "MOVE_TACKLE") == base_player
    base_enemy = _damage(baseline, 1, "MOVE_TACKLE")
    assert _damage(enemy_on, 1, "MOVE_TACKLE")["min"] > base_enemy["min"]
    assert _damage(enemy_off, 1, "MOVE_TACKLE") == base_enemy


def test_live_ability_and_active_stage_effects_change_damage():
    def azumarill(ability):
        def edit(request):
            request["player_party"][0].update(
                species="SPECIES_AZUMARILL", ability=ability
            )
        return edit

    thick_fat = _query(azumarill("ABILITY_THICK_FAT"))
    huge_power = _query(azumarill("ABILITY_HUGE_POWER"))
    assert _damage(huge_power, 0, "MOVE_TACKLE")["min"] > _damage(
        thick_fat, 0, "MOVE_TACKLE"
    )["min"]

    neutral = _query()

    def attack_stage(request):
        request["player_party"][0]["stat_stages"] = [8, 6, 6, 6, 6, 6, 6]

    def defense_stage(request):
        request["player_party"][0]["stat_stages"] = [6, 8, 6, 6, 6, 6, 6]

    attack_up = _query(attack_stage)
    defense_up = _query(defense_stage)
    assert _damage(attack_up, 0, "MOVE_TACKLE")["min"] > _damage(
        neutral, 0, "MOVE_TACKLE"
    )["min"]
    assert _damage(defense_up, 1, "MOVE_TACKLE")["max"] < _damage(
        neutral, 1, "MOVE_TACKLE"
    )["max"]
    stages = [line.split("|") for line in attack_up
              if line.startswith("ROMTRACE|query|active|")]
    # +2 Attack, then the opposing Gyarados's switch-in Intimidate takes one stage.
    assert [int(value) for value in stages[0][8:15]] == [7, 6, 6, 6, 6, 6, 6]


def test_live_crit_policy_omits_ordinary_crits_and_keeps_item_or_ability_changes():
    def night_slash(request):
        mon = request["player_party"][0]
        mon["moves"] = ["MOVE_TACKLE", "MOVE_NIGHT_SLASH", "MOVE_NONE", "MOVE_NONE"]
        mon["pp"] = [35, 15, 0, 0]

    ordinary = _query(night_slash)

    def scope_lens(enabled):
        def edit(request):
            night_slash(request)
            request["player_party"][0].update(
                item="ITEM_SCOPE_LENS", held_item_enabled=enabled
            )
        return edit

    lens_on = _query(scope_lens(True))
    lens_off = _query(scope_lens(False))

    def super_luck(request):
        night_slash(request)
        request["player_party"][0].update(
            species="SPECIES_ABSOL", ability="ABILITY_SUPER_LUCK"
        )

    ability_crit = _query(super_luck)
    assert _damage(ordinary, 0, "MOVE_NIGHT_SLASH")["crit_denominator"] == 1
    assert _damage(lens_on, 0, "MOVE_NIGHT_SLASH")["crit_denominator"] == 2
    assert _damage(lens_off, 0, "MOVE_NIGHT_SLASH")["crit_denominator"] == 1
    assert _damage(ability_crit, 0, "MOVE_NIGHT_SLASH")["crit_denominator"] == 2


def test_live_switch_entry_damage_is_present_in_post_switch_query():
    def edit(request):
        request["candidate_action"] = {"kind": "move", "move_id": "MOVE_TACKLE"}
        request["replay_turns"] = [{
            "player_action": {"kind": "switch", "slot": 1},
            "enemy_action": {"kind": "move", "move_id": "MOVE_TACKLE"},
        }]
        request["post_switch_query"] = True

    trace = _query(edit)
    active = [line.split("|") for line in trace
              if line.startswith("ROMTRACE|query|active|0|")][0]
    assert active[5] == str(run_dynamic.numeric_id("species", "SPECIES_CHARIZARD"))
    assert active[6:8] == ["113", "138"]
    assert "ROMTRACE|query|context|post-switch" in trace
    assert "ROMTRACE|phase|0|switch-settled" in trace


def test_live_v8_query_is_stable_and_leaves_the_token_byte_identical():
    target = _staged_or_pinned_v8()
    if target is None or not rom_bridge.DEFAULT_WORKER.is_file():
        pytest.skip("staged v8 test ELF or mGBA worker is not installed")
    elf, sha, version = target
    session = _QuerySession(elf, sha, version)
    try:
        request = _v8_request(["MOVE_TACKLE", "MOVE_BULLET_SEED", "MOVE_NONE", "MOVE_NONE"],
                              [35, 25, 0, 0])
        token = session.token(request)
        fingerprint = token.fingerprint()
        damage_a = rom_query.query_damage(session.client, session.mailbox, token,
                                          game_data=DATA, protocol_version=version)
        damage_b = rom_query.query_damage(session.client, session.mailbox, token,
                                          game_data=DATA, protocol_version=version)
        assert damage_a["player_moves"] == damage_b["player_moves"]
        assert damage_a["enemy_moves"] == damage_b["enemy_moves"]
        assert damage_a["query_schema"] == "sidedex-query-damage-v1"
        assert damage_a["decode_schema"] == "decode-v1"
        assert damage_a["rng"] == token.rng
        ai_a = rom_query.query_ai_action(session.client, session.mailbox, token,
                                         game_data=DATA, protocol_version=version)
        ai_b = rom_query.query_ai_action(session.client, session.mailbox, token,
                                         game_data=DATA, protocol_version=version)
        assert ai_a["ai_actions"] == ai_b["ai_actions"]
        assert ai_a["ai_actions"]["label"] == "deterministic"
        assert ai_a["rng"] == token.rng
        # Token immutability: decoded state, RNG and bytes are unchanged.
        assert token.fingerprint() == fingerprint
        assert rom_query._slot_sha(session.client, token.handle) == token.sha256
    finally:
        session.close()


def test_live_v8_queries_do_not_resolve_a_turn():
    target = _staged_or_pinned_v8()
    if target is None:
        pytest.skip("staged v8 test ELF is not installed")
    elf, sha, version = target
    request = _v8_request(["MOVE_TACKLE", "MOVE_BULLET_SEED", "MOVE_NONE", "MOVE_NONE"],
                          [35, 25, 0, 0], opponent_hp=30)
    request["query"] = True
    trace = run_dynamic.run_turn(elf, sha, request, protocol_version=version)
    assert "ROMTRACE|query|complete" in trace
    assert not any(line.startswith(("ROMTRACE|boundary|", "ROMTRACE|action|",
                                    "ROMTRACE|hp|")) for line in trace)
    cards = rom_query.parse_damage(trace, rom_query.snapshot_from_request(request), DATA)
    row = next(item for item in cards["player_moves"]["moves"]
               if item["move_id"] == "MOVE_BULLET_SEED")
    assert row["status"] == "engine-exact"
    assert row["damage_range"]["max"] >= row["damage_range"]["min"] > 0


def test_live_v8_unsupported_mechanic_fails_closed():
    target = _staged_or_pinned_v8()
    if target is None:
        pytest.skip("staged v8 test ELF is not installed")
    elf, sha, version = target
    # Counter's damage depends on the hit taken this turn, so the query cannot price it.
    request = _v8_request(["MOVE_COUNTER", "MOVE_TACKLE", "MOVE_NONE", "MOVE_NONE"],
                          [20, 35, 0, 0])
    request["query"] = True
    trace = run_dynamic.run_turn(elf, sha, request, protocol_version=version)
    cards = rom_query.parse_damage(trace, rom_query.snapshot_from_request(request), DATA)
    row = cards["player_moves"]["moves"][0]
    assert row["move_id"] == "MOVE_COUNTER"
    assert row["status"] == "unavailable"
    assert row["reason"] == "unsupported-mechanic"
    assert row["detail"]
    assert cards["unsupported"][0]["move_id"] == "MOVE_COUNTER"


def test_live_v8_item_query_is_labeled_and_leaves_the_token_unchanged():
    target = _staged_or_pinned_v8()
    if target is None or not rom_bridge.DEFAULT_WORKER.is_file():
        pytest.skip("staged v8 test ELF or mGBA worker is not installed")
    elf, sha, version = target
    session = _QuerySession(elf, sha, version)
    try:
        request = _v8_request(["MOVE_TACKLE", "MOVE_NONE", "MOVE_NONE", "MOVE_NONE"],
                              [35, 0, 0, 0], opponent_hp=1)
        request["trainer_items"] = {"ITEM_HYPER_POTION": 2}
        token = session.token(request)
        fingerprint = token.fingerprint()
        first = rom_query.query_ai_action(session.client, session.mailbox, token,
                                          game_data=DATA, protocol_version=version)
        second = rom_query.query_ai_action(session.client, session.mailbox, token,
                                           game_data=DATA, protocol_version=version)
        card = first["ai_actions"]
        assert card["predicted_action"]["kind"] == "item"
        assert card["predicted_action"]["id"] == "ITEM_HYPER_POTION"
        assert card["label"] == "deterministic" and card["distribution"] is False
        # The item decision is not consumed or repeated: the immutable token
        # restores to the same state and predicts the same item again.
        assert second["ai_actions"] == card
        assert token.fingerprint() == fingerprint
        assert rom_query._slot_sha(session.client, token.handle) == token.sha256
    finally:
        session.close()


def _transition_frames(elf, sha, version, mode):
    """Run one v8 SIMULATE_TURN to its boundary and report the frames it took."""
    client = rom_bridge.RomWorkerClient(
        rom_path=elf, expected_artifact_sha256=sha, timeout=60.0)
    with client:
        trace = []
        address = None
        for _ in range(400):
            trace.extend(run_dynamic._trace(client.run_frames(60)))
            ready = [line for line in trace if line.startswith("ROMTRACE|ready|")]
            if ready:
                announced = rom_bridge.parse_v9_ready(ready)
                address = announced["address"]
                break
        assert address is not None
        client.save_state()
        request = _v8_request(["MOVE_TACKLE", "MOVE_NONE", "MOVE_NONE", "MOVE_NONE"],
                              [35, 0, 0, 0])
        request["operation"] = "simulate-turn"
        request["request_id"] = 1
        request["mode"] = mode
        payload = run_dynamic.encode_request(request, protocol_version=version, result_buffer=announced)
        assert struct.unpack_from("<H", payload, run_dynamic.V8_EXTENSION_OFFSET + 2)[0] == (
            run_dynamic.MODE_LOGIC_ONLY if mode == "logic-only" else run_dynamic.MODE_NORMAL)
        result = client.batch_turns(
            address, [payload], mailbox_bytes=run_dynamic.mailbox_bytes(version),
            engine_elf_sha256=sha, deadline_ms=int(time.monotonic() * 1000) + 30_000,
            request_id="logic-only-probe", max_frames=24_000)
        turn = result["turns"][0]
        return turn["frames"], turn["events"]


def test_live_v8_simulate_turn_exports_the_complete_boundary_state():
    target = _staged_or_pinned_v8()
    if target is None:
        pytest.skip("staged v8 test ELF is not installed")
    elf, sha, version = target
    if elf == V8_ELF:
        pytest.skip("resolved v8 artifact predates the boundary-order fix")
    request = _v8_request(["MOVE_TACKLE", "MOVE_NONE", "MOVE_NONE", "MOVE_NONE"],
                          [35, 0, 0, 0])
    request["operation"] = "simulate-turn"
    request["request_id"] = 1
    trace = run_dynamic.run_turn(elf, sha, request, protocol_version=version)
    states = [line for line in trace if "|state|" in line]
    assert any(line.startswith("ROMTRACE|boundary|") for line in trace)
    # The boundary marker now follows the export, so the whole state survives the
    # worker's batch stop (25 state records at the first boundary).
    assert len(states) >= 20
    assert any("|state|party|" in line for line in states)
    assert any("|state|active|" in line for line in states)
    assert not any(line.startswith("ROMTRACE|action|") for line in trace)


def test_live_v8_logic_only_mode_is_honoured():
    target = _staged_or_pinned_v8()
    if target is None or not rom_bridge.DEFAULT_WORKER.is_file():
        pytest.skip("staged v8 test ELF or mGBA worker is not installed")
    elf, sha, version = target
    normal_frames, normal_events = _transition_frames(elf, sha, version, "normal")
    logic_frames, logic_events = _transition_frames(elf, sha, version, "logic-only")
    for events in (normal_events, logic_events):
        assert any(line.startswith("ROMTRACE|boundary|") for line in events)
        assert any("|state|party|" in line for line in events)
    # The logic-only pump runs up to four synchronous advances per frame, so the
    # same fixture reaches the boundary in fewer frames. Parity is not asserted.
    assert normal_frames != logic_frames


def test_live_v8_normal_critical_policy_is_labelled_and_changes_the_odds():
    target = _staged_or_pinned_v8()
    if target is None or not rom_bridge.DEFAULT_WORKER.is_file():
        pytest.skip("staged v8 test ELF or mGBA worker is not installed")
    elf, sha, version = target
    session = _QuerySession(elf, sha, version)
    try:
        request = _v8_request(["MOVE_TACKLE", "MOVE_NONE", "MOVE_NONE", "MOVE_NONE"],
                              [35, 0, 0, 0])
        token = session.token(request)
        rows = {}
        for policy in ("normal", "item-ability-only"):
            variant = dataclasses.replace(
                token, request={**token.request, "critical_policy": policy})
            damage = rom_query.query_damage(session.client, session.mailbox, variant,
                                            game_data=DATA, protocol_version=version)
            ai = rom_query.query_ai_action(session.client, session.mailbox, variant,
                                           game_data=DATA, protocol_version=version)
            assert damage["critical_policy"] == policy
            assert damage["critical_policy_verified"]
            assert damage["player_moves"]["critical_policy"] == policy
            assert ai["critical_policy"] == policy
            assert ai["ai_actions"]["label"] == "deterministic"
            rows[policy] = damage["player_moves"]["moves"][0]
        # Observed difference: `normal` keeps the real crit rolls, so the crit
        # denominator is the engine crit chance and the range includes crits,
        # while item-ability-only filters ordinary crits out.
        assert (rows["normal"]["crit_denominator"]
                > rows["item-ability-only"]["crit_denominator"])
        assert (rows["normal"]["damage_range"]["max"]
                > rows["item-ability-only"]["damage_range"]["max"])
    finally:
        session.close()
