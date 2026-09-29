"""Compiled-engine Calc evaluator: request encoding, snapshot preparation, and the planner-backed
recommendation card on a toy rollout (no emulator; the live rollout is covered by test_rom_rollout.py)."""

import copy
import json
import struct
import time
import unittest
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


















if __name__ == "__main__":
    unittest.main()


class Root:
    legal = [(0, run_dynamic.numeric_id("moves", "MOVE_THUNDER_SHOCK")),
             (0, run_dynamic.numeric_id("moves", "MOVE_GROWL")), (1, 1)]


THUNDER, GROWL, SWITCH = Root.legal
SEEN = []


def toy_rollout(root, items):
    """Thunder Shock wins 80% of seeds, Growl and the switch 20%; the win draw is the item's rng1."""
    SEEN.extend(items)
    return [{"outcome": "win" if item["rng1"][0] % 100 < (80 if item["prefix"][0] == THUNDER else 20) else "loss",
             "turns": 3, "prefix_consumed": 1, "legal_at_prefix_end": [],
             "leaf": {"p_alive": 0.0, "o_alive": 0.0, "p_hp": 0.0, "o_hp": 0.0}} for item in items]


QUERY = {kind: {"status": "engine-exact", "moves": [], "actions": []}
         for kind in ("player_moves", "enemy_moves", "ai_actions")}


def evaluate(snapshot, *, query=QUERY, rollout=toy_rollout, published=None, cancelled=None):
    """rom_eval.evaluate with the query worker and the rollout pool replaced (exceptions are raised)."""
    SEEN.clear()

    def open_rollout(request, deadline):
        if isinstance(rollout, Exception):
            raise rollout
        return Root, rollout

    def run_query(*args):
        if isinstance(query, Exception):
            raise query
        return copy.deepcopy(query)

    with mock.patch.object(rom_eval, "_query", run_query), mock.patch.object(rom_eval, "_open_rollout", open_rollout):
        return rom_eval.evaluate(snapshot, DATA, time.monotonic() + 12.0, cancelled=cancelled,
                                 publish=published.append if published is not None else None)


class Recommendation(unittest.TestCase):
    def _base(self, **kw):
        return snapshot_v2("TRAINER_HALEY_1", **kw)

    def test_streams_snapshots_then_a_final_p_win_card_with_the_leader_first(self):
        published = []
        result = evaluate(self._base(), published=published)
        card = result["recommendation"]
        self.assertEqual(result["status"], "estimated")
        self.assertEqual((card["critical_policy"], card["method"]), ("normal", "open-loop-uct"))
        self.assertEqual(card["actions"][0]["id"], "MOVE_THUNDER_SHOCK")
        self.assertEqual(card["actions"][-1]["kind"], "switch")
        self.assertTrue(card["resolved"] and card["final"] and not card["ranking_uncertain"])
        for row in card["actions"]:
            self.assertEqual(row["samples"], card["samples"])
            self.assertGreater(row["half_width"], 0)
        self.assertEqual([row["top_option"] for row in card["actions"]], [True, False, False])
        self.assertTrue(card["actions"][0]["uniquely_recommended"])
        self.assertGreater(card["actions"][0]["p_win"], card["actions"][1]["p_win"] + 0.3)
        # The first publish is the pending card next to the query cards; later ones stream snapshots.
        self.assertEqual(published[0]["recommendation"], {"status": "pending"})
        self.assertEqual(published[0]["player_moves"]["status"], "engine-exact")
        streamed = [p["recommendation"]["samples"] for p in published[1:]]
        self.assertEqual(streamed, sorted(set(streamed)))
        self.assertTrue(all(p["_meta"]["refining"] and p["status"] == "refining" for p in published))

    def test_a_tie_reports_the_leading_set(self):
        flat = lambda root, items: toy_rollout(root, [{**item, "prefix": [GROWL]} for item in items])  # noqa: E731
        card = evaluate(self._base(), rollout=flat)["recommendation"]
        self.assertTrue(card["ranking_uncertain"])
        self.assertFalse(card["resolved"])
        self.assertEqual([row["top_option"] for row in card["actions"]], [True] * 3)
        self.assertFalse(any(row["uniquely_recommended"] for row in card["actions"]))

    def test_known_rng_pins_every_seed_and_labels_the_card_fixed_seed(self):
        snapshot = self._base(rng={"main": KNOWN_MAIN, "secondary": KNOWN_SECONDARY})
        result = evaluate(snapshot)
        self.assertEqual(result["status"], "engine-fixed-seed")
        self.assertTrue(result["recommendation"]["deterministic"])
        self.assertTrue(SEEN and all(item["rng1"] == KNOWN_MAIN and item["rng2"] == KNOWN_SECONDARY
                                     for item in SEEN))
        self.assertLessEqual(result["recommendation"]["samples"], 8)
        evaluate(self._base())  # negative control: unknown RNG draws distinct seeds
        self.assertGreater(len({item["rng1"] for item in SEEN}), 8)

    def test_trainer_items_and_engine_failures_fail_closed_per_card(self):
        result = evaluate(self._base(enemy_items={"ITEM_POTION": 2}))
        self.assertEqual(result["recommendation"]["reason"], "unsupported-mechanic")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["player_moves"]["status"], "engine-exact")
        cases = [(FileNotFoundError("missing-artifact: no elf"), "missing-artifact"),
                 (rom_bridge.RomIdentityError("artifact SHA-256 00 does not match"), "artifact-mismatch"),
                 (rom_bridge.WorkerTimeoutError("timed out"), "timeout"),
                 (rom_bridge.WorkerExitedError("exited"), "engine-error")]
        for error, reason in cases:
            result = evaluate(self._base(), rollout=error)
            self.assertEqual(result["recommendation"]["reason"], reason, error)
            self.assertEqual(result["player_moves"]["status"], "engine-exact")  # query cards survive
            result = evaluate(self._base(), query=error)
            self.assertEqual(result["player_moves"]["reason"], reason, error)
            self.assertEqual(result["recommendation"]["status"], "estimated")  # and vice versa

    def test_invalid_snapshots_name_exact_reason_codes(self):
        for reason, mutate in (
                ("incomplete-state", lambda snap: snap["player"].__setitem__("active_index", 1)),
                ("invalid-id", lambda snap: snap["player"]["party"][0].__setitem__("status", "STATUS1_BOGUS")),
                ("source-mismatch", lambda snap: snap.__setitem__("source_sha", "0" * 40))):
            snapshot = self._base()
            mutate(snapshot)
            result = evaluate(snapshot)
            self.assertEqual(result["status"], "unavailable")
            for kind in rom_eval._CARDS:
                self.assertEqual(result[kind]["reason"], reason, (kind, result[kind]))

    def test_cancelled_calculation_stops_without_a_card(self):
        result = evaluate(self._base(), cancelled=lambda: True)
        self.assertEqual(result["recommendation"]["reason"], "timeout")


class Snapshots(unittest.TestCase):
    def test_move_alias_from_game_data_is_canonicalized_for_mailbox(self):
        snapshot = snapshot_v2('TRAINER_HALEY_1', rng={'main': KNOWN_MAIN, 'secondary': KNOWN_SECONDARY})
        mon = snapshot["player"]["party"][0]
        mon["moves"][0] = "MOVE_VICE_GRIP"
        mon["pp"] = {"MOVE_VICE_GRIP": 20}
        snapshot["planned_action"]["move_id"] = "MOVE_VICE_GRIP"
        prepared = rom_eval._canonical_moves(rom_eval._prepare_snapshot(snapshot), DATA)
        self.assertEqual(prepared["player"]["party"][0]["moves"][0], "MOVE_VISE_GRIP")
        self.assertEqual(prepared["planned_action"]["move_id"], "MOVE_VISE_GRIP")
        self.assertEqual(prepared["player"]["party"][0]["pp"]["MOVE_VISE_GRIP"], 20)

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


if __name__ == "__main__":
    unittest.main()
