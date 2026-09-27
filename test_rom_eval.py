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
from tools.rom_oracle import run_dynamic

ROOT = Path(__file__).parent
DATA = json.loads((ROOT / "data" / "game_data.json").read_text(encoding="utf-8"))
STAT_KEYS = ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")
PLAYER_MOVES = ("MOVE_THUNDER_SHOCK", "MOVE_QUICK_ATTACK", "MOVE_GROWL")
KNOWN_MAIN = [0x12345678, 0x9ABCDEF0, 0x0BADF00D, 1]
KNOWN_SECONDARY = [1, 2, 3, 4]
ELF = Path(os.environ["SIDEDEX_ENGINE_ELF_PATH"]) if os.environ.get("SIDEDEX_ENGINE_ELF_PATH") else None
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


def snapshot_v2(trainer_id, *, rng=None, players=1, planned="MOVE_THUNDER_SHOCK", origin="battle-start"):
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
        level_cap_id="CAP_MAX", enemy_items=None, ai_flags=record.get("ai_flags"),
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
    lines.append(f"ROMTRACE|complete|{turn}")
    return lines


def _decode(payload):
    kind, move, slot, _replay = struct.unpack_from("<4H", payload, 16)
    return {"kind": "move" if kind == 0 else "switch", "move_id": move, "slot": slot}


def _decode_second(payload):
    """Second candidate turn lives in the four halfwords after the replay slot."""
    kind, move, slot, present = struct.unpack_from("<4H", payload, run_dynamic.MAILBOX_BYTES - 8)
    return bool(present), {"kind": "move" if kind == 0 else "switch", "move_id": move, "slot": slot}


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
        self.assertEqual(result["recommendation"]["scored_turns"], 2)
        self.assertEqual(result["recommendation"]["label"], "two-turn lookahead")
        self.assertEqual(result["player_moves"]["moves"][0]["method"], "known-seed")


class TwoTurnLookahead(unittest.TestCase):
    def test_recommendation_scores_the_best_second_turn(self):
        """A two-turn payload is scored from its second-turn state, not turn one."""
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
                    present, _second = _decode_second(payload)
                    asked_for_second.append(present)
                    # Only the second turn removes the enemy active.
                    hp = [0, *enemy_hps[1:]] if present else list(enemy_hps)
                    turns.append({"complete": True, "events": _trace(
                        {"action": _decode(payload), "player_hp": player_hps, "enemy_hp": hp,
                         "enemy_action": {"kind": "move", "move_id": absorb}},
                        turn=2 if present else 1)})
                return {"turns": turns}

        result = _run(snapshot, Engine())
        self.assertIn(True, asked_for_second, "no two-turn request was issued")
        self.assertEqual(result["recommendation"]["scored_turns"], 2)
        self.assertEqual(result["recommendation"]["status"], "engine-fixed-seed")
        one_turn = {"parties": {0: {0: {"hp": player_hps[0]}},
                               1: {i: {"hp": hp} for i, hp in enumerate(enemy_hps)}}}
        self.assertGreater(result["recommendation"]["actions"][0]["score"]["mean"],
                           rom_eval._score(one_turn, player_party, enemy_party))


class SampledEvaluation(unittest.TestCase):
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
            self.assertGreaterEqual(card["effective_samples"], 2)
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
        self.assertEqual({row["id"] for row in actions}, {"MOVE_ABSORB", "MOVE_BUBBLE"})


class SwitchRecommendation(unittest.TestCase):
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


class FailClosedEvaluation(unittest.TestCase):
    def _base(self):
        return snapshot_v2("TRAINER_HALEY_1",
                           rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})

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
    def test_known_seed_snapshot_fills_all_four_cards(self):
        snapshot = snapshot_v2("TRAINER_HALEY_1",
                               rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})
        started = time.monotonic()
        result = rom_eval.evaluate(snapshot, DATA, deadline=started + 10.0)
        elapsed = time.monotonic() - started
        for kind in rom_eval._CARDS:
            self.assertEqual(result[kind]["status"], "engine-fixed-seed", (kind, result[kind]))
        self.assertLess(elapsed, 10.0)
        self.assertTrue(_no_exact(result))
        self.assertEqual(result["recommendation"]["scored_turns"], 2)
        self.assertEqual(result["player_moves"]["moves"][0]["method"], "known-seed")


if __name__ == "__main__":
    unittest.main()
