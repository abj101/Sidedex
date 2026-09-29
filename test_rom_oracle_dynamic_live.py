"""Live behavior fixtures for the pinned dynamic ROM oracle."""
import copy
import json
from pathlib import Path

import pytest

import rom_bridge
from tools.rom_oracle import run_dynamic


ROOT = Path(__file__).parent
ELF = ROOT / "native/rom_worker/build/mingw/pokeemerald-test-headless.elf"
BASE = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))


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
    assert [int(value) for value in stages[0][8:15]] == [8, 6, 6, 6, 6, 6, 6]


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
