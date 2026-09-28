"""Focused Gate 4 checks for the compiled-engine Calc evaluator.

Most checks drive rom_eval with fixture traces so they do not need the
emulator. The pinned-engine check is guarded by skipUnless, exactly like
test_rom_worker_smoke.py.
"""

import copy
import json
import os
import struct
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import battle_engine as be
import planner_ui
import rom_bridge
import rom_eval
import rom_query
from tools.rom_oracle import run_dynamic

ROOT = Path(__file__).parent
DATA = json.loads((ROOT / "data" / "game_data.json").read_text(encoding="utf-8"))
STAT_KEYS = ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")
PLAYER_MOVES = ("MOVE_THUNDER_SHOCK", "MOVE_QUICK_ATTACK", "MOVE_GROWL")
KNOWN_MAIN = [0x12345678, 0x9ABCDEF0, 0x0BADF00D, 1]
KNOWN_SECONDARY = [1, 2, 3, 4]
ELF = Path(os.environ["SIDEDEX_ENGINE_ELF_PATH"]) if os.environ.get(
    "SIDEDEX_ENGINE_ELF_PATH") else ROOT / "native" / "rom_worker" / "build" / "mingw" / "pokeemerald-test-headless.elf"
WORKER = Path(os.environ.get(
    "SIDEDEX_ROM_WORKER_EXE",
    ROOT / "native" / "rom_worker" / "build" / "mingw" / "rom_worker.exe",
))


def _trainer(trainer_id):
    return next(record for record in DATA["trainers"] if record["id"] == trainer_id)


def _player_row(row_id=1):
    return {"id": row_id, "species": "SPECIES_PIKACHU", "level": 50, "ability": "ABILITY_NONE",
            "item": "ITEM_NONE", "nature": "NATURE_HARDY",
            "ivs": json.dumps({key: 31 for key in STAT_KEYS}),
            "evs": json.dumps({key: 0 for key in STAT_KEYS}),
            "moves": json.dumps(list(PLAYER_MOVES))}


def _max_hp(species, level, nature, ivs, evs):
    return be.calc_mon_stats(DATA["species"][species]["stats"], level, nature, ivs, evs)["hp"]


def snapshot_v2(trainer_id, *, rng=None, players=1, planned="MOVE_THUNDER_SHOCK", origin="battle-start",
                enemy_items=None):
    """Schema-v2 battle-start snapshot through the UI builder, every member at full HP."""
    record = _trainer(trainer_id)
    party = record["party"]
    rows = [_player_row(row_id) for row_id in range(1, players + 1)]
    player_max = _max_hp("SPECIES_PIKACHU", 100, "NATURE_HARDY",
                         {key: 31 for key in STAT_KEYS}, {key: 0 for key in STAT_KEYS})
    state = {
        "player_active_slot": 0,
        "enemy_active_index": 0,
        "progress_flags": {},
        "rng": rng,
        "planned_action": {"kind": "move", "move_id": planned, "target_index": 0},
        "field": {},
        "history": {"turns": []},
        "state_origin": origin,
        "player": {str(row["id"]): {"hp": player_max, "max_hp": player_max, "status": "none",
                                    "pp": {move: 20 for move in PLAYER_MOVES}} for row in rows},
        "enemy": {},
    }
    for index, member in enumerate(party):
        maximum = _max_hp(member["species"], member["level"], member["nature"],
                          member["ivs"], member["evs"])
        state["enemy"][str(index)] = {"hp": maximum, "max_hp": maximum, "status": "none", "pp": {}}
    return planner_ui.build_calc_snapshot(
        DATA, rows, party, state, trainer_id=trainer_id, difficulty="Normal",
        level_cap_id="CAP_MAX", enemy_items=enemy_items, ai_flags=record.get("ai_flags"),
        enemy_levels=[member.get("level") for member in party])


def _party_hps(snapshot, side):
    return [mon["max_hp"] for mon in snapshot[side]["party"]]


def _trace(outcome, turn=1):
    """One completed turn trace: choices on turn-1, resulting state on turn."""
    lines = [f"ROMTRACE|phase|{turn}|after-turn"]
    action = outcome["action"]
    if action["kind"] == "move":
        lines.append(f"ROMTRACE|action|{turn - 1}|0|move|{action['move_id']}|1")
    else:
        lines.append(f"ROMTRACE|action|{turn - 1}|0|switch|{action['slot']}")
    enemy = outcome.get("enemy_action") or {"kind": "move", "move_id": 33}
    if enemy["kind"] == "move":
        lines.append(f"ROMTRACE|action|{turn - 1}|1|move|{enemy['move_id']}|0")
    else:
        lines.append(f"ROMTRACE|action|{turn - 1}|1|switch|{enemy['slot']}")
    lines.append(f"ROMTRACE|active|{turn}|0|0|25|{outcome['player_hp'][0]}")
    lines.append(f"ROMTRACE|rng|{turn}|1|2|3|4|5|6|7|8")
    for side, hps in ((0, outcome["player_hp"]), (1, outcome["enemy_hp"])):
        for slot, hp in enumerate(hps):
            lines.append(f"ROMTRACE|party|{turn}|{side}|{slot}|1|{hp}")
    lines.append("ROMTRACE|critical-policy|item-ability-only|battle-script-filtered")
    lines.append(f"ROMTRACE|complete|{turn}")
    return lines


def _decode(payload):
    kind, move, slot, _replay = struct.unpack_from("<4H", payload, 16)
    return {"kind": "move" if kind == 0 else "switch", "move_id": move, "slot": slot}


def _decode_second(payload):
    """Second candidate turn lives in the four halfwords after the replay slot."""
    kind, move, slot, present = struct.unpack_from("<4H", payload, run_dynamic.SECOND_ACTION_OFFSET)
    return present, {"kind": "move" if kind == 0 else "switch", "move_id": move, "slot": slot}


def _engine(outcomes, *, complete=True):
    class Engine:
        batches = 0

        def __call__(self, deadline):
            return self

        def __enter__(self):
            return self, 0x02000000

        def __exit__(self, *exc):
            return False

        def batch_turns(self, address, payloads, **kwargs):
            turns = [{"complete": complete, "events": _trace(outcomes(_decode(payload), self.batches))}
                     for payload in payloads]
            self.batches += 1
            return {"turns": turns}

    return Engine()


class _RaisingEngine:
    def __init__(self, error):
        self.error = error

    def __call__(self, deadline):
        raise self.error


def _run(snapshot, engine):
    with mock.patch.object(rom_eval, "_engine_session", engine):
        return rom_eval.evaluate(snapshot, DATA, time.monotonic() + 10.0)


def _no_exact(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("status", "method") and value == "exact":
                return False
            if not _no_exact(value):
                return False
    elif isinstance(node, list):
        return all(_no_exact(child) for child in node)
    return True


def _enemy_move(name):
    return run_dynamic.numeric_id("moves", name)


class RequestEncoding(unittest.TestCase):
    def test_trainer_items_fill_the_four_slots_at_the_mailbox_tail(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY},
                               enemy_items={"ITEM_HYPER_POTION": 2, "ITEM_REVIVE": 1})
        request = rom_eval._request(snapshot, DATA,
                                    {"kind": "move", "move_id": "MOVE_THUNDER_SHOCK"},
                                    (KNOWN_MAIN, KNOWN_SECONDARY))
        payload = run_dynamic.encode_request(request)
        self.assertEqual(len(payload), run_dynamic.MAILBOX_BYTES)
        self.assertEqual(struct.unpack_from("<H", payload, 4)[0], run_dynamic.VERSION)
        self.assertEqual(struct.unpack_from("<4H", payload, run_dynamic.SECOND_ACTION_OFFSET),
                         (0, 0, 0, 0))
        self.assertEqual(
            struct.unpack_from("<4H", payload, run_dynamic.SECOND_ACTION_OFFSET + 8),
            (run_dynamic.numeric_id("items", "ITEM_HYPER_POTION"),
             run_dynamic.numeric_id("items", "ITEM_HYPER_POTION"),
             run_dynamic.numeric_id("items", "ITEM_REVIVE"), 0))
        # Counts expand into one slot per item (2 Hyper Potions + 1 Revive = 3).
        self.assertEqual(struct.unpack_from("<H", payload, run_dynamic.SECOND_ACTION_OFFSET + 16)[0], 3)
        request["query"] = True
        query_payload = run_dynamic.encode_request(request)
        self.assertEqual(
            struct.unpack_from("<H", query_payload, run_dynamic.SECOND_PRESENT_OFFSET)[0], 2)

    def test_held_item_toggle_and_all_stat_stages_reach_the_pinned_record(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1", rng=None)
        member = snapshot["player"]["party"][0]
        member["held_item_enabled"] = False
        member["stat_stages"] = {"attack": 2, "speed": -1, "accuracy": 1}
        encoded = run_dynamic._encode_mon(rom_eval._mon(member, DATA, snapshot["state_origin"]))
        self.assertEqual(struct.unpack_from("<B7B", encoded, 56),
                         (0, 8, 6, 6, 6, 5, 7, 6))

    def test_more_than_four_trainer_items_are_incomplete_state(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY},
                               enemy_items={"ITEM_POTION": 5})
        request = rom_eval._request(snapshot, DATA,
                                    {"kind": "move", "move_id": "MOVE_THUNDER_SHOCK"},
                                    (KNOWN_MAIN, KNOWN_SECONDARY))
        with self.assertRaisesRegex(ValueError, "four item slots"):
            run_dynamic.encode_request(request)


class TurnParsing(unittest.TestCase):
    def test_resolved_actions_use_the_previous_turn_label(self):
        """The after-turn snapshot is labelled N; its choices are labelled N-1."""
        events = [
            "ROMTRACE|action|0|0|move|98|1",
            "ROMTRACE|action|0|1|move|71|0",
            "ROMTRACE|action|1|1|move|589|0",
            "ROMTRACE|party|1|0|0|25|211",
            "ROMTRACE|party|1|1|0|270|0",
            "ROMTRACE|active|1|0|0|25|211",
            "ROMTRACE|rng|1|1|2|3|4|5|6|7|8",
            "ROMTRACE|complete|1",
        ]
        parsed = rom_eval._parse_turn(events, (1, 1))
        self.assertEqual(parsed["turn"], 1)
        self.assertEqual([entry["id"] for entry in parsed["actions"] if entry["side"] == 1], [71])
        self.assertEqual([entry["id"] for entry in parsed["actions"] if entry["side"] == 0], [98])
        self.assertEqual(parsed["parties"][1][0]["hp"], 0)

    def test_first_turn_ko_ready_is_a_complete_turn(self):
        events = [
            "ROMTRACE|action|0|0|move|84|1",
            "ROMTRACE|action|0|1|move|741|0",
            "ROMTRACE|phase|0|after-turn",
            "ROMTRACE|party|0|0|0|25|0",
            "ROMTRACE|party|0|1|0|270|80",
            "ROMTRACE|active|0|0|0|25|0",
            "ROMTRACE|rng|0|1|2|3|4|5|6|7|8",
            "ROMTRACE|ready|33815612|760",
        ]
        parsed = rom_eval._parse_turn(events, (1, 1))
        self.assertEqual(parsed["turn"], 0)
        self.assertEqual([entry["id"] for entry in parsed["actions"] if entry["side"] == 1], [741])
        self.assertEqual(parsed["parties"][0][0]["hp"], 0)

    def test_partial_trace_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "incomplete-state"):
            rom_eval._parse_turn(["ROMTRACE|party|1|0|0|25|211", "ROMTRACE|complete|1"], (1, 1))

    def test_missing_party_slot_is_rejected(self):
        events = [
            "ROMTRACE|action|0|0|move|98|1",
            "ROMTRACE|party|1|0|0|25|211",
            "ROMTRACE|party|1|1|1|270|0",
            "ROMTRACE|active|1|0|0|25|211",
            "ROMTRACE|rng|1|1|2|3|4|5|6|7|8",
            "ROMTRACE|complete|1",
        ]
        with self.assertRaisesRegex(ValueError, "incomplete-state"):
            rom_eval._parse_turn(events, (2, 2))


class KnownSeedEvaluation(unittest.TestCase):
    def test_four_cards_are_engine_fixed_seed_and_never_exact(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})
        player_hps = _party_hps(snapshot, "player")
        enemy_hps = _party_hps(snapshot, "enemy")
        absorb = _enemy_move("MOVE_ABSORB")

        def outcomes(action, batch):
            return {"action": action, "player_hp": player_hps,
                    "enemy_hp": [0, *enemy_hps[1:]],
                    "enemy_action": {"kind": "move", "move_id": absorb}}

        result = _run(snapshot, _engine(outcomes))
        self.assertEqual(result["status"], "engine-fixed-seed")
        for kind in rom_eval._CARDS:
            self.assertEqual(result[kind]["status"], "engine-fixed-seed", result[kind])
        self.assertTrue(_no_exact(result))
        self.assertEqual(result["recommendation"]["scored_turns"], 1)
        self.assertEqual(result["recommendation"]["label"],
                         "one-turn risk ranking; follow-up estimate unavailable")
        self.assertEqual(result["player_moves"]["moves"][0]["method"], "known-seed")


class OneTurnLookahead(unittest.TestCase):
    def test_recommendation_uses_query_without_simulating_a_second_action(self):
        """Follow-up queries inspect the next state without executing another turn."""
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})
        player_party = snapshot["player"]["party"]
        enemy_party = snapshot["enemy"]["party"]
        player_hps = _party_hps(snapshot, "player")
        enemy_hps = _party_hps(snapshot, "enemy")
        absorb = _enemy_move("MOVE_ABSORB")
        asked_for_second = []
        class Engine:
            def __call__(self, deadline):
                return self

            def __enter__(self):
                return self, 0x02000000

            def __exit__(self, *exc):
                return False

            def batch_turns(self, address, payloads, **kwargs):
                turns = []
                for payload in payloads:
                    asked_for_second.append(_decode_second(payload)[0])
                    # The first turn removes the enemy active.
                    hp = [0, *enemy_hps[1:]]
                    turns.append({"complete": True, "events": _trace(
                        {"action": _decode(payload), "player_hp": player_hps, "enemy_hp": hp,
                         "enemy_action": {"kind": "move", "move_id": absorb}},
                        turn=1)})
                return {"turns": turns}

        result = _run(snapshot, Engine())
        self.assertNotIn(1, asked_for_second)
        self.assertEqual(result["recommendation"]["scored_turns"], 1)
        self.assertEqual(result["recommendation"]["status"], "engine-fixed-seed")
        self.assertEqual(result["recommendation"]["actions"][0]["score"]["mean"],
                         rom_eval._score(
                             {"parties": {0: {0: {"hp": player_hps[0]}},
                                          1: {i: {"hp": hp} for i, hp in enumerate([0, *enemy_hps[1:]])}}},
                             player_party, enemy_party))


class SampledEvaluation(unittest.TestCase):
    def test_ai_prediction_uses_recommended_action_without_a_planned_action(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1", rng={"main": KNOWN_MAIN,
                                                        "secondary": KNOWN_SECONDARY})
        snapshot.pop("planned_action")
        actions = rom_eval._actions(snapshot)
        preferred = actions[1]
        absorb, bubble = _enemy_move("MOVE_ABSORB"), _enemy_move("MOVE_BUBBLE")
        outcomes = {}
        scores = {}
        for action in actions:
            key = rom_eval._action_key(action)
            selected = action == preferred
            enemy_move = bubble if selected else absorb
            outcomes[key] = [{"actions": [{"side": 1, "kind": "move", "id": enemy_move}],
                              "parties": {0: {0: {"hp": 20}},
                                          1: {0: {"hp": 0 if selected else 20}}}}]
            scores[key] = [20 if action == preferred else 10]
        result = rom_eval._summarize(snapshot, DATA, actions, [], outcomes, scores, True)
        self.assertEqual(result["ai_actions"]["conditioned_on"],
                         f"recommended action: {rom_eval._label(preferred, DATA, snapshot)}")
        self.assertEqual(result["ai_actions"]["actions"][0]["id"], "MOVE_BUBBLE")

    def test_worker_deadline_keeps_three_completed_samples(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1", rng=None)
        player_hps = _party_hps(snapshot, "player")
        enemy_hps = _party_hps(snapshot, "enemy")
        absorb = _enemy_move("MOVE_ABSORB")
        engine = _engine(lambda action, batch: {
            "action": action, "player_hp": player_hps, "enemy_hp": enemy_hps,
            "enemy_action": {"kind": "move", "move_id": absorb}})
        original = engine.batch_turns

        def limited(*args, **kwargs):
            if engine.batches == 3:
                raise rom_bridge.WorkerResponseError("deadline_exceeded")
            return original(*args, **kwargs)

        engine.batch_turns = limited
        result = _run(snapshot, engine)
        self.assertEqual(result["recommendation"]["status"], "estimated", result)
        self.assertEqual(result["recommendation"]["effective_samples"], 3)

    def test_enemy_ko_samples_use_trajectories_for_the_planned_move(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})
        player_hps = _party_hps(snapshot, "player")
        enemy_hps = _party_hps(snapshot, "enemy")
        absorb, bubble = _enemy_move("MOVE_ABSORB"), _enemy_move("MOVE_BUBBLE")

        def outcomes(action, _batch):
            enemy_move = absorb if action == run_dynamic.numeric_id(
                "moves", "MOVE_THUNDER_SHOCK") else bubble
            enemy_hp = [0, *enemy_hps[1:]] if enemy_move == absorb else enemy_hps
            return {"action": action, "player_hp": player_hps, "enemy_hp": enemy_hp,
                    "enemy_action": {"kind": "move", "move_id": enemy_move}}

        engine = _engine(lambda _action, _batch: {})

        def batch_turns(address, payloads, **kwargs):
            turns = []
            for payload in payloads:
                _kind, move, _slot, _replay = struct.unpack_from("<4H", payload, 12)
                action = {"kind": "move", "move_id": move}
                turns.append({"complete": True, "events": _trace(
                    outcomes(move, 0) | {"action": action})})
            return {"turns": turns}

        engine.batch_turns = batch_turns
        result = _run(snapshot, engine)
        self.assertEqual(result["ai_actions"]["conditioned_on"], "the planned player action")
        self.assertEqual(result["ai_actions"]["actions"][0]["id"], "MOVE_ABSORB")
        rows = {row["move_id"]: row for row in result["enemy_moves"]["moves"]}
        self.assertEqual(rows["MOVE_ABSORB"]["num"], 0)
        self.assertEqual(rows["MOVE_ABSORB"]["den"], 1)
        self.assertEqual(rows["MOVE_BUBBLE"]["status"], "unavailable")

    def test_item_ai_action_is_labelled_from_the_item_id(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})
        item = run_dynamic.numeric_id("items", "ITEM_HYPER_POTION")
        turn = rom_eval._parse_turn([
            "ROMTRACE|action|0|0|move|84|1",
            f"ROMTRACE|action|0|1|item|{item}",
            "ROMTRACE|party|1|0|0|25|211",
            "ROMTRACE|party|1|1|0|270|80",
            "ROMTRACE|active|1|0|0|25|211",
            "ROMTRACE|rng|1|1|2|3|4|5|6|7|8",
            "ROMTRACE|complete|1",
        ], (1, 1))
        action = {"kind": "move", "move_id": "MOVE_THUNDER_SHOCK"}
        key = rom_eval._action_key(action)
        summary = rom_eval._summarize(snapshot, DATA, [action], [], {key: [turn]}, {key: [1]}, True)
        row = summary["ai_actions"]["actions"][0]
        self.assertEqual((row["kind"], row["id"], row["label"]),
                         ("item", "ITEM_HYPER_POTION",
                          rom_query.item_label(DATA, "ITEM_HYPER_POTION")))

    def test_candidates_share_each_seed_pair_and_publish_after_sample_floor(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1", rng=None)
        player_hps = _party_hps(snapshot, "player")
        enemy_hps = _party_hps(snapshot, "enemy")
        absorb = _enemy_move("MOVE_ABSORB")
        published = []

        class Engine:
            batches = []

            def __call__(self, deadline):
                return self

            def __enter__(self):
                return self, 0x02000000

            def __exit__(self, *exc):
                return False

            def batch_turns(self, address, payloads, **kwargs):
                self.batches.append(payloads)
                return {"turns": [{"complete": True, "events": _trace({
                    "action": _decode(payload), "player_hp": player_hps,
                    "enemy_hp": enemy_hps,
                    "enemy_action": {"kind": "move", "move_id": absorb}})}
                    for payload in payloads]}

        engine = Engine()
        with mock.patch.object(rom_eval, "_engine_session", lambda deadline: engine):
            result = rom_eval.evaluate(snapshot, DATA, time.monotonic() + 10.0,
                                       publish=published.append)

        self.assertEqual(result["status"], "estimated")
        self.assertTrue(engine.batches)
        candidate_batches = [payloads for payloads in engine.batches
                             if all(_decode_second(payload)[0] != 2 for payload in payloads)]
        seed_pairs = []
        for payloads in candidate_batches:
            rng_values = [struct.unpack_from("<8I", payload, 20) for payload in payloads]
            self.assertTrue(rng_values)
            self.assertEqual(len(set(rng_values)), 1, "all candidates in a sample must share both RNG streams")
            seed_pairs.append(rng_values[0])
        self.assertEqual(len(seed_pairs), len(set(seed_pairs)))
        self.assertEqual([row["progress"]["completed_samples"] for row in published],
                         list(range(rom_eval.MIN_ESTIMATE_SAMPLES, len(candidate_batches) + 1)))
        target_samples = published[0]["progress"]["target_samples"]
        self.assertTrue(all(row["progress"]["target_samples"] == target_samples
                            for row in published))
        self.assertEqual(published[-1]["recommendation"]["status"], "estimated")

    def test_unknown_seed_reports_samples_sampler_and_interval(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1", rng=None)
        player_hps = _party_hps(snapshot, "player")
        enemy_hps = _party_hps(snapshot, "enemy")
        absorb, bubble = _enemy_move("MOVE_ABSORB"), _enemy_move("MOVE_BUBBLE")

        def outcomes(action, batch):
            enemy_move = absorb if batch % 2 == 0 else bubble
            return {"action": action, "player_hp": player_hps, "enemy_hp": enemy_hps,
                    "enemy_action": {"kind": "move", "move_id": enemy_move}}

        result = _run(snapshot, _engine(outcomes))
        for kind in rom_eval._CARDS:
            card = result[kind]
            self.assertEqual(card["status"], "estimated", card)
            self.assertGreaterEqual(card["effective_samples"], rom_eval.MIN_ESTIMATE_SAMPLES)
            self.assertEqual(card["sampler_id"], "paired-sfc32-v1")
        row = result["player_moves"]["moves"][0]
        self.assertLessEqual(row["interval_95"]["low"], row["interval_95"]["high"])
        self.assertGreaterEqual(row["interval_95"]["low"], 0.0)
        self.assertLessEqual(row["interval_95"]["high"], 1.0)
        self.assertIsInstance(result["recommendation"]["ranking_uncertain"], bool)

    def test_ai_action_frequencies_sum_to_one(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1", rng=None)
        player_hps = _party_hps(snapshot, "player")
        enemy_hps = _party_hps(snapshot, "enemy")
        absorb, bubble = _enemy_move("MOVE_ABSORB"), _enemy_move("MOVE_BUBBLE")

        def outcomes(action, batch):
            enemy_move = absorb if batch % 2 == 0 else bubble
            return {"action": action, "player_hp": player_hps, "enemy_hp": enemy_hps,
                    "enemy_action": {"kind": "move", "move_id": enemy_move}}

        result = _run(snapshot, _engine(outcomes))
        actions = result["ai_actions"]["actions"]
        self.assertTrue(actions)
        total = sum(Fraction(row["probability"]["num"], row["probability"]["den"]) for row in actions)
        self.assertEqual(total, 1)
        self.assertIn("conditioned_on", result["ai_actions"])


class SwitchRecommendation(unittest.TestCase):
    def test_ranking_risk_combines_conditional_followup_without_fake_ci(self):
        first_turn = {"num": 1, "den": 4,
                      "interval_95": {"low": 0.1, "high": 0.5}}
        followup = {"estimate": 0.2, "range": {"low": 0.1, "high": 0.4},
                    "basis": "measured test model"}
        combined = rom_eval._ranking_death_risk(first_turn, followup, False)
        self.assertAlmostEqual(combined["estimate"], 0.4)
        self.assertEqual(combined["range"], {"low": 0.19, "high": 0.7})
        self.assertIn("conditional modeled follow-up risk", combined["basis"])

        unresolved = rom_eval._ranking_death_risk(first_turn, None, False)
        self.assertIsNone(unresolved["estimate"])
        self.assertEqual(unresolved["range"], {"low": 0.1, "high": 1.0})
        self.assertTrue(rom_eval._intervals_overlap(
            {"range": {"low": 0.1, "high": 1.0}},
            {"range": {"low": 0.75, "high": 0.9}}))

    def test_followup_estimate_accepts_a_survivable_two_hit_ko_path(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1", rng=None)
        player = snapshot["player"]["party"][0]
        enemy = snapshot["enemy"]["party"][0]
        player["hp"] = player["max_hp"]
        player["moves"] = ["MOVE_QUICK_ATTACK"]
        cards = {
            "critical_policy_verified": True,
            "active": {
                0: {"party_slot": 0, "hp": player["hp"], "stat_stages": [6] * 7},
                1: {"party_slot": 0, "hp": 50, "stat_stages": [6] * 7},
            },
            "ai_actions": {"predicted_action": {"kind": "move", "id": "MOVE_TACKLE"}},
            "player_moves": {"moves": [{
                "move_id": "MOVE_QUICK_ATTACK", "status": "engine-exact",
                "probability": {"num": 0, "den": 1},
                "damage_range": {"min": 25, "max": 30}, "accuracy_percent": 100,
            }]},
            "enemy_moves": {"moves": [{
                "move_id": "MOVE_TACKLE", "status": "engine-exact",
                "probability": {"num": 0, "den": 1},
                "damage_range": {"min": 1, "max": 2}, "accuracy_percent": 100,
            }]},
        }
        estimate = rom_eval._followup_estimate(snapshot, DATA,
                                                {"kind": "switch", "slot": 0}, cards)
        self.assertEqual(estimate["turns_to_ko"]["estimate"], 2)
        self.assertEqual(estimate["followup_viability"], 1.0)
        self.assertEqual(estimate["matchup_estimate"], "estimated-stable")

    def test_switch_is_unique_only_with_lower_risk_and_stable_ko_path(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1", rng=None, players=2)
        actions = [{"kind": "move", "move_id": "MOVE_THUNDER_SHOCK"},
                   {"kind": "switch", "slot": 1}]
        player_hps = _party_hps(snapshot, "player")
        enemy_hps = _party_hps(snapshot, "enemy")
        outcomes, scores, post_queries = {}, {}, {}
        for action in actions:
            key = rom_eval._action_key(action)
            trajectories = []
            for _ in range(16):
                fainted = action["kind"] == "move"
                p_hp = [0 if fainted else player_hps[0], player_hps[1]]
                trajectories.append({
                    "actions": [{"side": 0, "kind": action["kind"], "id": 0},
                                {"side": 1, "kind": "move", "id": _enemy_move("MOVE_TACKLE")}],
                    "parties": {0: {i: {"hp": hp} for i, hp in enumerate(p_hp)},
                                1: {i: {"hp": hp} for i, hp in enumerate(enemy_hps)}},
                })
            outcomes[key], scores[key] = trajectories, [0.0] * len(trajectories)
            slot = action.get("slot", 0)
            player = snapshot["player"]["party"][slot]
            post_queries[key] = [{
                "critical_policy_verified": True,
                "active": {0: {"party_slot": slot, "hp": player["max_hp"],
                               "stat_stages": [6] * 7},
                           1: {"party_slot": 0, "hp": 50, "stat_stages": [6] * 7}},
                "ai_actions": {"predicted_action": {"kind": "move", "id": "MOVE_TACKLE"}},
                "player_moves": {"moves": [{
                    "move_id": "MOVE_QUICK_ATTACK", "status": "engine-exact",
                    "probability": {"num": 0, "den": 1},
                    "damage_range": {"min": 25, "max": 30}, "accuracy_percent": 100}]},
                "enemy_moves": {"moves": [{
                    "move_id": "MOVE_TACKLE", "status": "engine-exact",
                    "probability": {"num": 0, "den": 1},
                    "damage_range": {"min": 1, "max": 2}, "accuracy_percent": 100}]},
            }]
        result = rom_eval._summarize(snapshot, DATA, actions, [], outcomes, scores,
                                    False, post_queries)
        recommendation = result["recommendation"]
        self.assertEqual(recommendation["actions"][0]["kind"], "switch")
        self.assertTrue(recommendation["actions"][0]["uniquely_recommended"])

    def test_followup_estimate_uses_post_action_damage_and_priority(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1", rng={"main": KNOWN_MAIN,
                                                        "secondary": KNOWN_SECONDARY})
        player_moves = [
            {"move_id": "MOVE_THUNDER_SHOCK", "status": "engine-exact",
             "probability": {"num": 0, "den": 1}, "damage_range": {"min": 1, "max": 2},
             "accuracy_percent": 100},
            {"move_id": "MOVE_QUICK_ATTACK", "status": "engine-exact",
             "probability": {"num": 1, "den": 1}, "damage_range": {"min": 30, "max": 40},
             "accuracy_percent": 100},
        ]
        cards = {
            "critical_policy_verified": True,
            "active": {
                0: {"party_slot": 0, "hp": 200, "max_hp": 200},
                1: {"party_slot": 0, "hp": 20, "max_hp": 20},
            },
            "player_moves": {"moves": player_moves},
            "enemy_moves": {"moves": [
                {"move_id": "MOVE_SCRATCH", "probability": {"num": 0, "den": 1}},
            ]},
            "ai_actions": {"predicted_action": {"kind": "move", "id": "MOVE_SCRATCH"}},
        }
        estimate = rom_eval._followup_estimate(
            snapshot, DATA, {"kind": "move", "move_id": "MOVE_THUNDER_SHOCK"}, cards)
        self.assertEqual(estimate["followup_action"]["move_id"], "MOVE_QUICK_ATTACK")
        self.assertEqual(estimate["turns_to_ko"]["estimate"], 1)
        self.assertEqual(estimate["matchup_estimate"], "estimated-stable")

    def test_a_switch_can_outrank_every_move(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY}, players=2)
        player_hps = _party_hps(snapshot, "player")
        enemy_hps = _party_hps(snapshot, "enemy")
        absorb = _enemy_move("MOVE_ABSORB")

        def outcomes(action, batch):
            if action["kind"] == "switch":
                player_hp = list(player_hps)
                enemy_hp = [0, *enemy_hps[1:]]
            else:
                player_hp = [0, *player_hps[1:]]
                enemy_hp = list(enemy_hps)
            return {"action": action, "player_hp": player_hp, "enemy_hp": enemy_hp,
                    "enemy_action": {"kind": "move", "move_id": absorb}}

        result = _run(snapshot, _engine(outcomes))
        ranking = result["recommendation"]["actions"]
        self.assertEqual(ranking[0]["kind"], "switch")
        self.assertTrue(all(row["score"]["mean"] < ranking[0]["score"]["mean"]
                            for row in ranking[1:]), ranking)

    def test_unknown_seed_does_not_call_a_switch_safe_from_three_samples(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1", rng=None, players=2)
        actions = [
            {"kind": "move", "move_id": "MOVE_THUNDER_SHOCK"},
            {"kind": "switch", "slot": 1},
        ]
        player_hp = _party_hps(snapshot, "player")
        enemy_hp = _party_hps(snapshot, "enemy")
        outcomes, scores = {}, {}
        for action in actions:
            key = rom_eval._action_key(action)
            trajectories = []
            for _ in range(rom_eval.MIN_ESTIMATE_SAMPLES):
                safe = action["kind"] == "switch"
                trajectories.append({
                    "actions": [{"side": 0, "kind": action["kind"], "id": 0},
                                {"side": 1, "kind": "move", "id": _enemy_move("MOVE_ABSORB")}],
                    "parties": {
                        0: {index: {"hp": hp if safe else 0 if index == 0 else hp}
                            for index, hp in enumerate(player_hp)},
                        1: {index: {"hp": 0 if safe and index == 0 else hp}
                            for index, hp in enumerate(enemy_hp)},
                    },
                })
            outcomes[key], scores[key] = trajectories, [0.0] * len(trajectories)
        result = rom_eval._summarize(snapshot, DATA, actions, [], outcomes, scores, False)
        recommendation = result["recommendation"]
        self.assertTrue(recommendation["ranking_uncertain"])
        self.assertFalse(recommendation["actions"][0]["uniquely_recommended"])
        self.assertEqual(recommendation["actions"][0]["death_risk"]["den"], 3)

    def test_known_seed_risk_is_labeled_deterministic(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})
        action = {"kind": "move", "move_id": "MOVE_THUNDER_SHOCK"}
        key = rom_eval._action_key(action)
        hp = _party_hps(snapshot, "player")
        enemy = _party_hps(snapshot, "enemy")
        outcome = {"actions": [{"side": 0, "kind": "move", "id": 0},
                               {"side": 1, "kind": "move", "id": 1}],
                   "parties": {0: {i: {"hp": value} for i, value in enumerate(hp)},
                               1: {i: {"hp": value} for i, value in enumerate(enemy)}}}
        result = rom_eval._summarize(snapshot, DATA, [action], [], {key: [outcome]}, {key: [0]}, True)
        row = result["recommendation"]["actions"][0]
        self.assertTrue(row["deterministic"])
        self.assertIsNone(row["death_risk"]["interval_95"])
        self.assertEqual(result["recommendation"]["critical_policy"], "item-ability-only")


class FailClosedEvaluation(unittest.TestCase):
    def _base(self):
        return snapshot_v2("TRAINER_HALEY_1",
                           rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})

    def test_move_alias_from_game_data_is_canonicalized_for_mailbox(self):
        snapshot = self._base()
        mon = snapshot["player"]["party"][0]
        mon["moves"][0] = "MOVE_VICE_GRIP"
        mon["pp"] = {"MOVE_VICE_GRIP": 20}
        snapshot["planned_action"]["move_id"] = "MOVE_VICE_GRIP"
        prepared = rom_eval._canonical_moves(rom_eval._prepare_snapshot(snapshot), DATA)
        self.assertEqual(prepared["player"]["party"][0]["moves"][0], "MOVE_VISE_GRIP")
        self.assertEqual(prepared["planned_action"]["move_id"], "MOVE_VISE_GRIP")
        self.assertEqual(prepared["player"]["party"][0]["pp"]["MOVE_VISE_GRIP"], 20)

    def _idle_engine(self, snapshot):
        player_hps = _party_hps(snapshot, "player")
        enemy_hps = _party_hps(snapshot, "enemy")
        absorb = _enemy_move("MOVE_ABSORB")
        return _engine(lambda action, batch: {
            "action": action, "player_hp": player_hps, "enemy_hp": enemy_hps,
            "enemy_action": {"kind": "move", "move_id": absorb}})

    def test_unsupported_and_artifact_failures_name_exact_reason_codes(self):
        mismatch = rom_bridge.RomIdentityError("artifact SHA-256 00 does not match pinned artifact")
        cases = [
            ("incomplete-state", lambda snap: snap["player"].__setitem__("active_index", 1), None),
            ("invalid-id", lambda snap: snap["player"]["party"][0].__setitem__("status", "STATUS1_BOGUS"), None),
            ("source-mismatch", lambda snap: snap.__setitem__("source_sha", "0" * 40), None),
            ("missing-artifact",
             None, _RaisingEngine(FileNotFoundError("missing-artifact: set SIDEDEX_ENGINE_ELF_PATH"))),
            ("artifact-mismatch", None, _RaisingEngine(mismatch)),
        ]
        for reason, mutate, engine in cases:
            snapshot = self._base()
            if mutate is not None:
                mutate(snapshot)
            result = _run(snapshot, engine or self._idle_engine(snapshot))
            self.assertEqual(result["status"], "unavailable", (reason, result))
            for kind in rom_eval._CARDS:
                self.assertEqual(result[kind]["reason"], reason, (kind, result[kind]))

    def test_incomplete_trajectory_is_never_confident(self):
        snapshot = self._base()
        player_hps = _party_hps(snapshot, "player")
        enemy_hps = _party_hps(snapshot, "enemy")
        absorb = _enemy_move("MOVE_ABSORB")
        engine = _engine(lambda action, batch: {
            "action": action, "player_hp": player_hps, "enemy_hp": enemy_hps,
            "enemy_action": {"kind": "move", "move_id": absorb}}, complete=False)
        result = _run(snapshot, engine)
        self.assertEqual(result["status"], "unavailable")
        for kind in rom_eval._CARDS:
            self.assertEqual(result[kind]["status"], "unavailable")
            self.assertEqual(result[kind]["reason"], "insufficient-samples")

    def test_manual_edited_start_is_reconstructable(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY},
                               origin="manual-scenario")
        snapshot["player"]["party"][0]["hp"] -= 10
        result = _run(snapshot, self._idle_engine(snapshot))
        self.assertEqual(result["status"], "engine-fixed-seed")
        self.assertEqual(result["scenario"], "edited-start")
        self.assertNotEqual(result["player_moves"]["status"], "unavailable")

    def test_focused_nonzero_active_is_assumed_lead(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY},
                               players=2)
        snapshot["player"]["active_index"] = 1
        snapshot["enemy"]["active_index"] = 0
        result = _run(snapshot, self._idle_engine(snapshot))
        self.assertEqual(result["status"], "engine-fixed-seed")
        self.assertTrue(result["assumed_lead"])

    def test_assumed_lead_remaps_history_switch_slots(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY},
                               players=2)
        snapshot["player"]["active_index"] = 1
        snapshot["history"] = {"turns": [{
            "player_action": {"kind": "switch", "slot": 0},
            "enemy_action": {"kind": "move", "move_id": "MOVE_ABSORB"},
        }]}
        prepared = rom_eval._prepare_snapshot(snapshot)
        self.assertEqual(prepared["player"]["active_index"], 0)
        self.assertEqual(prepared["player"]["party"][0]["species"],
                         snapshot["player"]["party"][1]["species"])
        self.assertEqual(prepared["history"]["turns"][0]["player_action"]["slot"], 1)
        self.assertEqual(prepared["_player_order"], [1, 0])

    def test_timeout_and_worker_exit_are_unavailable(self):
        for reason, error in (("timeout", rom_bridge.WorkerTimeoutError("mGBA worker response timed out")),
                              ("engine-error", rom_bridge.WorkerExitedError("mGBA worker exited before replying"))):
            result = _run(self._base(), _RaisingEngine(error))
            self.assertEqual(result["status"], "unavailable", error)
            for kind in rom_eval._CARDS:
                self.assertEqual(result[kind]["reason"], reason, (kind, result[kind]))
                self.assertNotIn(result[kind]["status"], ("exact", "estimated", "engine-fixed-seed"))


@unittest.skipUnless(ELF is not None and ELF.is_file(), "set SIDEDEX_ENGINE_ELF_PATH to the pinned test ELF")
@unittest.skipUnless(WORKER.is_file(), f"mGBA worker executable is missing: {WORKER}")
class PinnedEngineEvaluation(unittest.TestCase):
    def test_multi_hit_move_has_an_engine_exact_probability_and_range(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})
        snapshot["player"]["party"][0]["moves"][0] = "MOVE_BULLET_SEED"
        snapshot["planned_action"] = None
        result = rom_eval.evaluate(snapshot, DATA, deadline=time.monotonic() + 10.0)
        row = result["player_moves"]["moves"][0]
        self.assertEqual(row["move_id"], "MOVE_BULLET_SEED")
        self.assertEqual(row["status"], "engine-exact")
        self.assertEqual(row["method"], "engine-enumerated-multi-hit")
        self.assertGreater(row["probability"]["den"], 0)
        self.assertGreaterEqual(row["probability"]["den"], row["probability"]["num"])
        self.assertLess(0, row["damage_range"]["min"])
        self.assertGreater(row["damage_range"]["max"], row["damage_range"]["min"])

    def test_trainer_items_reach_the_engine_and_never_incomplete_state(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY},
                               enemy_items={"ITEM_POTION": 2})
        started = time.monotonic()
        result = rom_eval.evaluate(snapshot, DATA, deadline=started + 10.0)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(snapshot["enemy"]["items"], {"ITEM_POTION": 2})
        self.assertEqual(result["player_moves"]["status"], "engine-exact")
        for kind in rom_eval._CARDS:
            self.assertNotEqual(result[kind].get("reason"), "incomplete-state", kind)

    def test_low_hp_trainer_item_predicts_the_item_and_fails_recommendation_closed(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY},
                               enemy_items={"ITEM_HYPER_POTION": 2})
        snapshot["planned_action"] = None
        snapshot["enemy"]["party"][0]["hp"] = 1
        started = time.monotonic()
        result = rom_eval.evaluate(snapshot, DATA, deadline=started + 10.0)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(result["ai_actions"]["predicted_action"]["kind"], "item")
        self.assertEqual(result["player_moves"]["status"], "engine-exact")
        self.assertEqual(result["enemy_moves"]["status"], "engine-exact")
        self.assertEqual(result["recommendation"]["status"], "unavailable")
        self.assertEqual(result["recommendation"]["reason"], "unsupported-mechanic")

    def test_known_seed_snapshot_fills_all_four_cards(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})
        started = time.monotonic()
        result = rom_eval.evaluate(snapshot, DATA, deadline=started + 10.0)
        elapsed = time.monotonic() - started
        for kind in ("player_moves", "enemy_moves"):
            self.assertEqual(result[kind]["status"], "engine-exact", (kind, result[kind]))
        for kind in ("ai_actions", "recommendation"):
            self.assertEqual(result[kind]["status"], "engine-fixed-seed", (kind, result[kind]))
        self.assertLess(elapsed, 10.0)
        self.assertTrue(_no_exact(result))
        self.assertEqual(result["recommendation"]["scored_turns"], 1)
        self.assertEqual(result["player_moves"]["moves"][0]["method"], "engine-enumerated-single-hit")

    def test_matt_first_turn_ko_fills_all_four_cards(self):
        snapshot = snapshot_v2("TRAINER_MATT_SEAFLOOR_CAVERN",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})
        started = time.monotonic()
        result = rom_eval.evaluate(snapshot, DATA, deadline=started + 10.0)
        elapsed = time.monotonic() - started
        for kind in ("player_moves", "enemy_moves"):
            self.assertEqual(result[kind]["status"], "engine-exact", (kind, result[kind]))
        for kind in ("ai_actions", "recommendation"):
            self.assertEqual(result[kind]["status"], "engine-fixed-seed", (kind, result[kind]))
        self.assertLess(elapsed, 5.0)
        self.assertTrue(_no_exact(result))


if __name__ == "__main__":
    unittest.main()
