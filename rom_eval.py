"""Small, fail-closed evaluator for the local compiled battle test ELF."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import threading
import time
from collections import Counter
from math import ceil
from pathlib import Path

import battle_engine as be
import rom_bridge
import rom_pool
import rom_query
from tools.rom_oracle import run_dynamic


_CARDS = ("player_moves", "enemy_moves", "ai_actions", "recommendation")
_STATS = ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")
_STAGE_KEYS = ("attack", "defense", "sp_attack", "sp_defense", "speed", "accuracy", "evasion")
_MAX_BATCH = 8
_MAX_SAMPLES = 16
MIN_ESTIMATE_SAMPLES = 3
# Measured marginal cost of one batched engine turn on the pinned worker.
_TURN_MS = 240


def _unavailable(reason, detail, missing=()):
    return {"status": "unavailable", "reason": reason, "detail": detail,
            "missing": list(missing)}


def _result(source_sha, status, **cards):
    meta = cards.pop("_meta", {})
    return {"status": status, "source_sha": source_sha, "critical_policy": "item-ability-only",
            "_meta": meta, **meta,
               **{kind: cards.get(kind, _unavailable("incomplete-state", "No result was produced."))
                  for kind in _CARDS}}


def _stage_values(mon):
    stages = mon.get("stat_stages") or {}
    if isinstance(stages, dict):
        if any(type(value) is not int or not -6 <= value <= 6 for value in stages.values()):
            raise ValueError("incomplete-state: stat stages must be integers from -6 to 6")
        return [6 + stages.get(key, 0) for key in _STAGE_KEYS]
    if isinstance(stages, list) and len(stages) == len(_STAGE_KEYS) and all(
            type(value) is int and 0 <= value <= 12 for value in stages):
        return stages
    raise ValueError("incomplete-state: stat stages must contain seven valid engine stage values")


def _unsupported(snapshot, game_data):
    if snapshot.get("schema_version") != 2:
        return "incomplete-state", "The evaluator requires a schema-v2 battle snapshot.", []
    if snapshot.get("source_sha") != (game_data.get("source") or {}).get("commit"):
        return "source-mismatch", "The snapshot and game data use different pinned sources.", []
    if snapshot.get("difficulty") not in ("Easy", "Normal"):
        return "incomplete-state", "This compiled scenario supports Easy or Normal difficulty.", ["difficulty"]
    player = snapshot.get("player") or {}
    enemy = snapshot.get("enemy") or {}
    if not isinstance(player.get("active_index"), int) or not 0 <= player["active_index"] < len(player.get("party") or []):
        return "incomplete-state", "The active party index is out of range.", ["player.active_index"]
    if not isinstance(enemy.get("active_index"), int) or not 0 <= enemy["active_index"] < len(enemy.get("party") or []):
        return "incomplete-state", "The active party index is out of range.", ["enemy.active_index"]
    if not snapshot.get("progress", {}).get("milestone_id"):
        return "incomplete-state", "The selected numeric level cap is not a ROM progression milestone.", ["progress.milestone_id"]
    if snapshot.get("state_origin") not in ("battle-start", "manual-scenario"):
        return "incomplete-state", "The battle origin is unknown.", ["state_origin"]
    turns = (snapshot.get("history") or {}).get("turns", [])
    if not isinstance(turns, list) or len(turns) > 1:
        return "incomplete-state", "The current engine can replay at most one observed turn.", ["history.turns"]
    field = snapshot.get("field") or {}
    if any(field.get(key) not in (None, [], False) for key in
           ("weather", "terrain", "trick_room", "player_screens", "enemy_screens",
            "player_hazards", "enemy_hazards")):
        return "incomplete-state", "The current engine request cannot encode this field state.", ["field"]
    for side_name, side in (("player", player), ("enemy", enemy)):
        party = side.get("party")
        if not isinstance(party, list) or not 1 <= len(party) <= 6:
            return "incomplete-state", f"{side_name} party must contain one to six complete builds.", [f"{side_name}.party"]
        for index, mon in enumerate(party):
            item_enabled = mon.get("held_item_enabled", mon.get("item_enabled", True))
            if type(item_enabled) is not bool:
                return "incomplete-state", "Held item availability must be a boolean.", [f"{side_name}.party[{index}].held_item_enabled"]
            try:
                _stage_values(mon)
            except ValueError:
                return "incomplete-state", "Stat stages must use seven values from -6 to +6.", [f"{side_name}.party[{index}].stat_stages"]
            if mon.get("status") not in (None, "none") and mon.get("status") not in run_dynamic.ID_MAP["ids"]["statuses"]:
                return "invalid-id", f"Unknown status on {side_name} party slot {index}.", [f"{side_name}.party[{index}].status"]
            if type(mon.get("hp")) is not int or type(mon.get("max_hp")) is not int:
                return "incomplete-state", "Current and maximum HP are required by the engine request.", [f"{side_name}.party[{index}].hp", f"{side_name}.party[{index}].max_hp"]
            if not 0 <= mon["hp"] <= mon["max_hp"]:
                return "incomplete-state", "A party member has invalid current HP.", [f"{side_name}.party[{index}].hp"]
    if not isinstance(enemy.get("ai_flags"), list) or "AI_FLAG_UNKNOWN" in enemy["ai_flags"]:
        return "incomplete-state", "The trainer AI flags are not resolved.", ["enemy.ai_flags"]
    if len(turns) and any(not isinstance(turn.get(key), dict) for turn in turns
                           for key in ("player_action", "enemy_action")):
        return "incomplete-state", "A replay turn must contain both observed actions.", ["history.turns"]
    return None


def _field(snapshot):
    # Empty/unknown effects are the clear battle-start state represented by the hook.
    return {"weather": "WEATHER_NONE", "terrain": "TERRAIN_NONE", "trick_room": False,
            "player_screens": 0, "enemy_screens": 0,
            "player_hazards": 0, "enemy_hazards": 0}


def _mask(flags):
    mapping = run_dynamic.ID_MAP["ids"].get("ai_flags", {})
    if type(flags) is int and flags >= 0:
        return flags
    if not isinstance(flags, list) or any(flag not in mapping for flag in flags):
        raise ValueError("invalid-id: trainer AI flag is absent from the pinned ID map")
    result = 0
    for flag in flags:
        result |= mapping[flag]
    return result


def _mon(mon, game_data, origin):
    moves = list(mon.get("moves") or [])[:4]
    moves += ["MOVE_NONE"] * (4 - len(moves))
    pp_map = mon.get("pp") if isinstance(mon.get("pp"), dict) else {}
    pp = []
    for move_id in moves:
        if move_id == "MOVE_NONE":
            pp.append(0)
        elif move_id in pp_map:
            if type(pp_map[move_id]) is not int:
                raise ValueError("incomplete-state: an entered move PP value is unknown")
            pp.append(pp_map[move_id])
        elif origin in ("battle-start", "manual-scenario"):
            maximum = (game_data.get("moves", {}).get(move_id) or {}).get("pp")
            if type(maximum) is not int:
                raise ValueError(f"invalid-id: no PP data for {move_id}")
            pp.append(maximum)
        else:
            raise ValueError(f"incomplete-state: missing PP for {move_id}")
    status = mon.get("status")
    status1 = "STATUS1_NONE" if status in (None, "none") else status
    ivs, evs = mon.get("ivs") or {}, mon.get("evs") or {}
    return {"species": mon["species"], "level": mon["level"],
            "ability": mon["ability"], "item": mon.get("item") or "ITEM_NONE",
            "held_item_enabled": mon.get("held_item_enabled", mon.get("item_enabled", True)),
            "nature": mon["nature"], "hp": mon["hp"], "max_hp": mon["max_hp"],
            "status1": status1, "moves": moves, "pp": pp,
            "stat_stages": _stage_values(mon),
            "ivs": [ivs.get(stat, 0) for stat in _STATS],
            "evs": [evs.get(stat, 0) for stat in _STATS]}


def _request(snapshot, game_data, action, seed, *, second=None, replay_turns=None,
             query=False, post_switch_query=False):
    turns = ((snapshot.get("history") or {}).get("turns", [])
             if replay_turns is None else replay_turns)
    party = snapshot["player"]["party"]
    enemy = snapshot["enemy"]
    request = {"ai_flags": _mask(enemy["ai_flags"]), "candidate_action": action,
               "critical_policy": "item-ability-only",
               "replay_turns": turns,
               "player_active_index": 0,
               "opponent_active_index": 0,
               "trainer_items": enemy.get("items") or {},
               "rng_main": seed[0], "rng_secondary": seed[1], "field": _field(snapshot),
               "player_party": [_mon(mon, game_data, snapshot.get("state_origin")) for mon in party],
               "opponent_party": [_mon(mon, game_data, snapshot.get("state_origin"))
                                  for mon in enemy["party"]]}
    if second is not None:
        request["second_action"] = second
    if query:
        request["query"] = True
    if post_switch_query:
        request["post_switch_query"] = True
    return request


def _run_batch(client, mailbox, payloads, deadline, request_id):
    return client.batch_turns(
        mailbox, payloads, mailbox_bytes=run_dynamic.MAILBOX_BYTES,
        engine_elf_sha256=rom_bridge.ENGINE_ELF_SHA256,
        deadline_ms=int(deadline * 1000), request_id=request_id, max_frames=24_000)


def _query_cards(client, mailbox, snapshot, game_data, action, seed, deadline):
    request = _request(snapshot, game_data, action, seed)
    request["query"] = True
    payload = run_dynamic.encode_request(request)
    reply = _run_batch(client, mailbox, [payload], deadline, "calc-query")
    cards = rom_query.parse(reply["turns"][0]["events"], snapshot, game_data)
    if not cards.get("critical_policy_verified"):
        raise ValueError("unsupported-mechanic: query did not verify item-ability-only critical-hit policy")
    return cards


def _trace_action(record, game_data):
    kind = record.get("kind")
    if kind == "move":
        reverse_moves = {value: key for key, value in
                         run_dynamic.ID_MAP["ids"]["moves"].items()}
        move_id = reverse_moves.get(record["id"])
        if move_id is None or move_id not in game_data.get("moves", {}):
            return None
        return {"kind": "move", "move_id": move_id}
    if kind == "switch":
        return {"kind": "switch", "slot": record["id"]}
    return None


def _followup_query_payload(snapshot, game_data, action, outcome, seed):
    actions = {}
    for record in outcome.get("first_actions", []):
        actions.setdefault(record["side"], _trace_action(record, game_data))
    if set(actions) != {0, 1} or not actions[0] or not actions[1]:
        return None
    replay_turn = {"player_action": actions[0], "enemy_action": actions[1]}
    history = list((snapshot.get("history") or {}).get("turns") or [])
    if len(history) + 1 > 1:
        return None
    slot = action.get("slot", 0) if action["kind"] == "switch" else 0
    try:
        mon = snapshot["player"]["party"][slot]
        move_id = next(move for move in mon.get("moves") or [] if move != "MOVE_NONE")
    except (IndexError, StopIteration):
        return None
    return _request(snapshot, game_data, {"kind": "move", "move_id": move_id}, seed,
                    replay_turns=[*history, replay_turn], query=True,
                    post_switch_query=actions[0]["kind"] == "switch")


def _actions(snapshot):
    party = snapshot["player"]["party"]
    active = party[0]
    result = []
    for move_id in active.get("moves") or []:
        pp = active.get("pp") or {}
        if move_id != "MOVE_NONE" and (pp.get(move_id, 1) or 0) > 0:
            action = {"kind": "move", "move_id": move_id}
            if action not in result:
                result.append(action)
    for slot, mon in enumerate(party[1:], 1):
        if type(mon.get("hp")) is int and mon["hp"] > 0:
            result.append({"kind": "switch", "slot": slot})
    return result


def _remap_switch(action, order):
    if not isinstance(action, dict) or action.get("kind") != "switch":
        return action
    old_slot = action.get("slot")
    if not isinstance(old_slot, int) or old_slot not in order:
        return action
    remapped = dict(action)
    remapped["slot"] = order.index(old_slot)
    return remapped


def _prepare_snapshot(snapshot):
    """Move each focused battler to slot zero for the hook's lead contract."""
    import copy

    prepared = copy.deepcopy(snapshot)
    orders = {}
    for side_name in ("player", "enemy"):
        side = prepared.get(side_name) or {}
        party = side.get("party")
        active = side.get("active_index")
        if not isinstance(party, list) or not isinstance(active, int):
            continue
        if not 0 <= active < len(party):
            continue
        order = [active] + [index for index in range(len(party)) if index != active]
        side["party"] = [party[index] for index in order]
        side["active_index"] = 0
        orders[side_name] = order
        if side_name == "player":
            prepared["planned_action"] = _remap_switch(prepared.get("planned_action"), order)
    if orders:
        prepared["_player_order"] = orders.get("player")
        for turn in (prepared.get("history") or {}).get("turns") or []:
            if not isinstance(turn, dict):
                continue
            if "player" in orders:
                turn["player_action"] = _remap_switch(turn.get("player_action"), orders["player"])
            if "enemy" in orders:
                turn["enemy_action"] = _remap_switch(turn.get("enemy_action"), orders["enemy"])
    return prepared


def _canonical_moves(snapshot, game_data):
    aliases = {move_id: row["alias_of"] for move_id, row in game_data.get("moves", {}).items()
               if isinstance(row, dict) and row.get("alias_of")}
    if not aliases:
        return snapshot
    for side_name in ("player", "enemy"):
        for mon in (snapshot.get(side_name) or {}).get("party") or []:
            mon["moves"] = [aliases.get(move_id, move_id) for move_id in mon.get("moves") or []]
            if isinstance(mon.get("pp"), dict):
                mon["pp"] = {aliases.get(move_id, move_id): value for move_id, value in mon["pp"].items()}
    actions = [snapshot.get("planned_action")]
    for turn in (snapshot.get("history") or {}).get("turns") or []:
        if isinstance(turn, dict):
            actions.extend((turn.get("player_action"), turn.get("enemy_action")))
    for action in actions:
        if isinstance(action, dict) and action.get("kind") == "move":
            action["move_id"] = aliases.get(action.get("move_id"), action.get("move_id"))
    return snapshot


def _scenario(snapshot):
    return "edited-start" if snapshot.get("state_origin") == "manual-scenario" else "battle-start"


def _action_key(action):
    fields = ("kind", "move_id") if action.get("kind") == "move" else ("kind", "slot")
    return json.dumps({key: action[key] for key in fields}, sort_keys=True)


def _parse_turn(events, party_sizes):
    if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
        raise ValueError("engine-error: malformed turn events")
    done = [int(row.split("|")[2]) for row in events if row.startswith("ROMTRACE|complete|")]
    after = [int(row.split("|")[2]) for row in events
             if row.startswith("ROMTRACE|phase|") and row.endswith("|after-turn")]
    terminal_ready = any(row.startswith("ROMTRACE|ready|") for row in events)
    if done:
        turn = done[-1]
        action_turn = turn - 1 if turn >= 1 else 0
    elif after and terminal_ready:
        # First-turn KO: the hook snapshots after-turn at counter 0, then the
        # test runner announces ready instead of complete.
        turn = after[-1]
        action_turn = turn
    else:
        raise ValueError("incomplete-state: turn did not have exactly one complete marker")
    if turn < 0:
        raise ValueError("incomplete-state: turn counter did not advance")
    # The engine snapshots the resulting state as turn N and labels the choices
    # that produced it as turn N-1, so the resolved actions live on turn - 1.
    # Terminal KOs share the after-turn counter for both actions and snapshot.
    records = {}
    actions = []
    for line in events:
        cells = line.split("|")
        if len(cells) < 3 or cells[0] != "ROMTRACE":
            continue
        kind = cells[1]
        if kind == "action" and len(cells) >= 6 and int(cells[2]) == action_turn:
            actions.append({"side": int(cells[3]), "kind": cells[4], "id": int(cells[5]),
                            "target": int(cells[6]) if len(cells) > 6 else None})
        elif kind == "party" and len(cells) >= 7 and int(cells[2]) == turn:
            records.setdefault(int(cells[3]), {})[int(cells[4])] = {
                "species": int(cells[5]), "hp": int(cells[6])}
    actions_by_turn = {}
    for line in events:
        cells = line.split("|")
        if len(cells) >= 6 and cells[0] == "ROMTRACE" and cells[1] == "action":
            actions_by_turn.setdefault(int(cells[2]), []).append(
                {"side": int(cells[3]), "kind": cells[4], "id": int(cells[5]),
                 "target": int(cells[6]) if len(cells) > 6 else None})
    first_action_turn = min(actions_by_turn) if actions_by_turn else action_turn
    first_actions = actions_by_turn.get(first_action_turn, [])
    if not actions:
        if action_turn != turn:
            action_turn = turn
            for line in events:
                cells = line.split("|")
                if len(cells) >= 6 and cells[0] == "ROMTRACE" and cells[1] == "action" and int(cells[2]) == action_turn:
                    actions.append({"side": int(cells[3]), "kind": cells[4], "id": int(cells[5]),
                                    "target": int(cells[6]) if len(cells) > 6 else None})
        if not actions or len(records) < 2:
            raise ValueError("incomplete-state: outcome lacks actions or both resulting parties")
    elif len(records) < 2:
        raise ValueError("incomplete-state: outcome lacks actions or both resulting parties")
    for side, size in enumerate(party_sizes):
        party = records.get(side, {})
        if len(party) < size or sorted(party) != list(range(len(party))):
            raise ValueError("incomplete-state: outcome lacks a complete resulting party")
    if not any(line.startswith(f"ROMTRACE|active|{turn}|") for line in events):
        raise ValueError("incomplete-state: outcome lacks active battler state")
    if not any(line.startswith(f"ROMTRACE|rng|{turn}|") for line in events):
        raise ValueError("incomplete-state: outcome lacks resulting RNG state")
    policy_records = [line for line in events if line.startswith((
        "ROMTRACE|critical-policy|", "ROMTRACE|runner|critical-policy|"))]
    expected_policy = "ROMTRACE|critical-policy|item-ability-only|battle-script-filtered"
    expected_runner = ("ROMTRACE|runner|critical-policy|item-ability-only|verified-elf:" +
                       rom_bridge.ENGINE_ELF_SHA256)
    policy_verified = bool(policy_records) and all(
        line in (expected_policy, expected_runner) for line in policy_records)
    rng_values = []
    for line in events:
        cells = line.split("|")
        if len(cells) == 11 and cells[0] == "ROMTRACE" and cells[1] == "rng" and int(cells[2]) == turn:
            rng_values = [int(value) for value in cells[3:]]
    faints = []
    for line in events:
        cells = line.split("|")
        if len(cells) == 6 and cells[:2] == ["ROMTRACE", "faint"]:
            faints.append({"turn": int(cells[2]), "battler": int(cells[3]),
                           "party_slot": int(cells[4]), "species_id": int(cells[5])})
    return {"turn": turn, "actions": actions, "first_actions": first_actions,
            "parties": records, "faints": faints, "resulting_rng": rng_values,
            "critical_policy": "item-ability-only" if policy_verified else None,
            "critical_policy_verified": policy_verified}


def _wilson(successes, total):
    if not total:
        return {"low": 0.0, "high": 1.0}
    p = successes / total
    z2 = 1.96 ** 2
    den = 1 + z2 / total
    center = (p + z2 / (2 * total)) / den
    margin = 1.96 * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total) / den
    return {"low": max(0.0, center - margin), "high": min(1.0, center + margin)}


def _proportion(successes, total):
    return {"num": successes, "den": total, "effective_samples": total,
            "interval_95": _wilson(successes, total)}


def _observed_rate(successes, total, known_seed):
    """A known seed is a single deterministic observation, not a probability."""
    if known_seed:
        return {"num": successes, "den": total, "interval_95": None,
                "deterministic": True}
    return _proportion(successes, total)


def _party_fainted(initial, final):
    return any(mon["hp"] > 0 and final[index]["hp"] == 0
               for index, mon in enumerate(initial))


def _action_observations(action, trajectories, initial_player, initial_enemy, known_seed):
    total = len(trajectories)
    deaths = 0
    ko_before_faint = 0
    remaining = []
    enemy_kos = 0
    enemy_damage = []
    for sample in trajectories:
        players = sample["parties"][0]
        enemies = sample["parties"][1]
        fainted = _party_fainted(initial_player, players)
        deaths += fainted
        ko = initial_enemy[0]["hp"] > 0 and enemies.get(0, {}).get("hp", 1) == 0
        enemy_kos += ko
        ko_before_faint += ko and not fainted
        maximum_party_hp = sum(mon["max_hp"] for mon in initial_player)
        remaining.append(sum(players[index]["hp"] for index in range(len(initial_player))) /
                         maximum_party_hp if maximum_party_hp else 0.0)
        enemy_damage.append(max(0, initial_enemy[0]["hp"] - enemies.get(0, {}).get("hp", initial_enemy[0]["hp"])))
    return {
        "death_risk": _observed_rate(deaths, total, known_seed),
        "ko_before_faint": _observed_rate(ko_before_faint, total, known_seed),
        "ko": _observed_rate(enemy_kos, total, known_seed),
        "remaining_hp_fraction": sum(remaining) / total if total else 0.0,
        "mean_enemy_damage": sum(enemy_damage) / total if total else 0.0,
        "matchup_estimate": "unavailable",
    }


def _intervals_overlap(left, right):
    a = left.get("range") or left.get("interval_95")
    b = right.get("range") or right.get("interval_95")
    return a is None or b is None or (a["low"] <= b["high"] and b["low"] <= a["high"])


def _ranking_death_risk(first_turn, followup, known_seed):
    first_probability = _ratio(first_turn)
    first_interval = first_turn.get("interval_95")
    if first_interval is None:
        first_interval = {"low": first_probability, "high": first_probability}
    followup = followup or {}
    followup_probability = followup.get("estimate")
    followup_range = followup.get("range") or {"low": 0.0, "high": 1.0}
    if followup_probability is None:
        estimate = None
    else:
        estimate = first_probability + (1 - first_probability) * followup_probability
    risk_range = {
        "low": first_interval["low"] + (1 - first_interval["low"]) * followup_range["low"],
        "high": first_interval["high"] + (1 - first_interval["high"]) * followup_range["high"],
    }
    return {"estimate": estimate,
            "range": risk_range,
            "basis": ("first-turn deterministic result plus conditional modeled follow-up risk"
                      if known_seed else
                      "first-turn sampled risk plus conditional modeled follow-up risk")}


def _ratio(value):
    try:
        return value["num"] / value["den"] if value["den"] else 0.0
    except (KeyError, TypeError, ZeroDivisionError):
        return 0.0


def _speed(mon, game_data):
    """Return a stable calculated speed, or None for speed-changing effects."""
    ability = mon.get("ability")
    item = mon.get("item")
    if ability in {"ABILITY_SWIFT_SWIM", "ABILITY_CHLOROPHYLL", "ABILITY_SAND_RUSH",
                   "ABILITY_SLUSH_RUSH", "ABILITY_UNBURDEN", "ABILITY_QUICK_FEET",
                   "ABILITY_SURGE_SURFER", "ABILITY_SLOW_START"}:
        return None
    if mon.get("held_item_enabled", mon.get("item_enabled", True)) and item in {
            "ITEM_CHOICE_SCARF", "ITEM_IRON_BALL", "ITEM_MACHO_BRACE",
            "ITEM_POWER_WEIGHT", "ITEM_POWER_BRACER", "ITEM_POWER_BELT",
            "ITEM_POWER_LENS", "ITEM_POWER_BAND", "ITEM_POWER_ANKLET"}:
        return None
    if mon.get("status") not in (None, "none"):
        return None
    try:
        stats = be.calc_mon_stats(
            game_data["species"][mon["species"]]["stats"], mon["level"],
            mon["nature"], {key: (mon.get("ivs") or {}).get(key, 0) for key in _STATS},
            {key: (mon.get("evs") or {}).get(key, 0) for key in _STATS})
        stage = _stage_values(mon)[4] - 6
    except (KeyError, TypeError, ValueError):
        return None
    value = stats["speed"] * ((2 + stage) / 2 if stage >= 0 else 2 / (2 - stage))
    return int(value)


def _followup_estimate(snapshot, game_data, first_action, cards):
    """Choose the best measured post-action attack and estimate the stable matchup."""
    if not cards or not cards.get("critical_policy_verified"):
        return None
    active = cards.get("active") or {}
    player_state = active.get(0) or {}
    enemy_state = active.get(1) or {}
    player_slot = player_state.get("party_slot", first_action.get("slot", 0))
    enemy_slot = enemy_state.get("party_slot", 0)
    try:
        player = dict(snapshot["player"]["party"][player_slot])
        enemy = snapshot["enemy"]["party"][enemy_slot]
    except (IndexError, TypeError):
        return None
    if isinstance(player_state.get("stat_stages"), list):
        player["stat_stages"] = player_state["stat_stages"]
    stable_effects = True
    if first_action["kind"] == "move":
        first_move = game_data.get("moves", {}).get(first_action["move_id"], {})
        stable_effects = first_move.get("effect") in (None, "EFFECT_HIT") or str(
            first_move.get("effect", "")).startswith(("EFFECT_ATTACK_", "EFFECT_DEFENSE_",
                "EFFECT_SPECIAL_ATTACK_", "EFFECT_SPECIAL_DEFENSE_", "EFFECT_SPEED_", "EFFECT_DRAGON_DANCE",
                "EFFECT_CALM_MIND", "EFFECT_BULK_UP"))
        speed_changed = first_move.get("effect") in {
            "EFFECT_SPEED_UP_2", "EFFECT_DRAGON_DANCE", "EFFECT_AGILITY",
            "EFFECT_ATTACK_ACCURACY_UP", "EFFECT_TIDY_UP", "EFFECT_ORDER_UP"}
    else:
        speed_changed = False
    enemy_action = (cards.get("ai_actions") or {}).get("predicted_action") or {}
    enemy_move_id = enemy_action.get("id") if enemy_action.get("kind") == "move" else None
    enemy_move_data = game_data.get("moves", {}).get(enemy_move_id, {})
    if enemy_move_id and enemy_move_data.get("effect") not in (None, "EFFECT_HIT"):
        stable_effects = False
    player_speed, enemy_speed = _speed(player, game_data), _speed(enemy, game_data)
    if speed_changed:
        player_speed = None
    player_rows = (cards.get("player_moves") or {}).get("moves") or []
    candidates = []
    for row in player_rows:
        data = game_data.get("moves", {}).get(row.get("move_id"), {})
        damage = row.get("damage_range") or {}
        accuracy = row.get("accuracy_percent")
        if (row.get("status") != "engine-exact" or data.get("category") not in ("physical", "special") or
                type(data.get("power")) is not int or data["power"] <= 0 or
                type(accuracy) is not int or accuracy <= 0 or
                type(damage.get("min")) is not int or type(damage.get("max")) is not int):
            continue
        mean_damage = (damage["min"] + damage["max"]) / 2
        damage_per_turn = mean_damage * accuracy / 100
        if damage_per_turn <= 0:
            continue
        target_hp = enemy_state.get("hp", row.get("target_hp", 0))
        turns = max(1, ceil(target_hp / damage_per_turn))
        player_priority = int(data.get("priority", 0))
        enemy_priority = int(enemy_move_data.get("priority", 0))
        if player_priority != enemy_priority:
            player_first = player_priority > enemy_priority
        elif player_speed is None or enemy_speed is None or player_speed == enemy_speed:
            player_first = None
        else:
            player_first = player_speed > enemy_speed
        enemy_row = next((candidate for candidate in
                          ((cards.get("enemy_moves") or {}).get("moves") or [])
                          if candidate.get("move_id") == enemy_move_id), None)
        enemy_ko_chance = _ratio((enemy_row or {}).get("probability") or {})
        player_ko_chance = _ratio(row.get("probability") or {})
        survival = None
        if player_first is not None:
            enemy_actions = max(0, turns - 1) if player_first else turns
            enemy_damage = (enemy_row or {}).get("damage_range") or {}
            enemy_accuracy = (enemy_row or {}).get("accuracy_percent")
            if enemy_actions == 0:
                survival = 1.0
            elif enemy_actions == 1 and enemy_row and enemy_row.get("probability"):
                survival = 1.0 - enemy_ko_chance
            elif (type(enemy_damage.get("max")) is int and
                  type(enemy_accuracy) is int and 0 <= enemy_accuracy <= 100):
                # The maximum measured enemy hit bounds survival across the
                # expected number of actions before the player's estimated KO.
                survival = (1.0 if enemy_damage["max"] * enemy_actions < player_state.get("hp", 0)
                            else None)
        if turns == 1:
            viable = None if survival is None else player_ko_chance * survival
        else:
            minimum_path_damage = damage["min"] * turns
            maximum_path_damage = damage["max"] * turns
            if survival is None or player_first is None:
                viable = None
            elif minimum_path_damage >= target_hp:
                accuracy_probability = (accuracy / 100) ** turns
                viable = accuracy_probability * survival
            elif maximum_path_damage < target_hp:
                viable = 0.0
            else:
                viable = None
        candidates.append((viable, -turns, damage_per_turn, row, turns, player_first,
                           survival, enemy_actions if player_first is not None else None))
    if not candidates:
        return {"matchup_estimate": "unavailable", "followup_action": None,
                "turns_to_ko": None}
    viable, _turn_key, damage_per_turn, best, turns, player_first, survival, enemy_actions = max(
        candidates, key=lambda item: (
            -1 if item[0] is None else item[0], item[1], item[2]))
    matchup_status = ("unsupported-changing-effect" if not stable_effects else
                      "uncertain-speed-order" if player_first is None else
                      "uncertain-multi-turn-survival" if turns > 1 and survival is None else
                      "uncertain-damage-rolls" if turns > 1 and
                      best["damage_range"]["min"] * turns < enemy_state.get("hp", 0) <=
                      best["damage_range"]["max"] * turns else "estimated-stable")
    if matchup_status == "estimated-stable" and survival is not None:
        followup_risk_estimate = max(0.0, min(1.0, 1.0 - survival))
        followup_risk_range = {"low": followup_risk_estimate,
                               "high": (1.0 if turns > 1 and best["accuracy_percent"] < 100
                                        else followup_risk_estimate)}
        followup_risk_basis = (
            "conditional on surviving the first turn; measured enemy KO odds before the follow-up"
            if turns == 1 else
            "conditional on surviving the first turn; maximum measured damage through expected attacks; misses may extend the matchup")
    else:
        followup_risk_estimate = None
        followup_risk_range = {"low": 0.0, "high": 1.0}
        followup_risk_basis = "conditional follow-up faint risk is uncertain for this matchup"
    viability_rate = (None if viable is None else {
        "estimate": viable,
        "basis": "measured accuracy and damage path, with priority and speed order"})
    return {
        "matchup_estimate": matchup_status,
        "followup_action": {"kind": "move", "move_id": best["move_id"],
                            "move_name": best.get("move_name")},
        "followup_ko_before_faint": viability_rate,
        "followup_viability": viable,
        "followup_death_risk": {"estimate": followup_risk_estimate,
                                "basis": followup_risk_basis,
                                "range": followup_risk_range},
        "turns_to_ko": {"estimate": turns,
                         "basis": "measured damage range times accuracy; priority and speed order checked"},
        "mean_damage_per_turn": damage_per_turn,
        "player_moves_first": player_first,
        "enemy_actions_before_ko": enemy_actions,
    }


def _score(outcome, initial_player, initial_enemy):
    def hp_loss(initial, final):
        return sum(mon["hp"] for mon in initial) - sum(final[index]["hp"] for index in range(len(initial)))
    player_loss = hp_loss(initial_player, outcome["parties"][0])
    enemy_loss = hp_loss(initial_enemy, outcome["parties"][1])
    player_alive = sum(outcome["parties"][0][i]["hp"] > 0 for i in range(len(initial_player)))
    enemy_alive = sum(outcome["parties"][1][i]["hp"] > 0 for i in range(len(initial_enemy)))
    return float(enemy_loss - player_loss + 100 * (player_alive - enemy_alive))


def _label(action, game_data, snapshot=None):
    if action["kind"] == "move":
        return (game_data.get("moves", {}).get(action["move_id"]) or {}).get("name", action["move_id"])
    slot = action["slot"]
    order = (snapshot or {}).get("_player_order")
    original = order[slot] if isinstance(order, list) and 0 <= slot < len(order) else slot
    return f"Switch to party slot {original + 1}"


def _summarize(snapshot, game_data, actions, samples, outcomes, scores, known_seed,
               post_queries=None):
    enemy_party = snapshot["enemy"]["party"]
    player_party = snapshot["player"]["party"]
    move_rows = []
    for action in actions:
        if action["kind"] != "move":
            continue
        trajectories = outcomes[_action_key(action)]
        kos = sum(enemy_party[0]["hp"] > 0 and sample["parties"][1].get(0, {}).get("hp") == 0
                  for sample in trajectories)
        row = {"move_id": action["move_id"], "move_name": _label(action, game_data, snapshot),
               "method": "known-seed" if known_seed else "estimated",
               "ko": "Opponent party member reaches 0 HP in the completed turn."}
        row.update(_observed_rate(kos, len(trajectories), known_seed))
        if not known_seed:
            row.update(status="estimated", sampler_id="paired-sfc32-v1")
        else:
            row["deterministic"] = True
        move_rows.append(row)
    planned = snapshot.get("planned_action")
    baseline = next((action for action in actions if planned and
                     _action_key(action) == _action_key(planned)), None)
    if baseline is None:
        def baseline_key(action):
            observation = _action_observations(
                action, outcomes[_action_key(action)], player_party, enemy_party, known_seed)
            deaths = observation["death_risk"]["num"] / observation["death_risk"]["den"]
            kos = observation["ko_before_faint"]["num"] / observation["ko_before_faint"]["den"]
            return deaths, -kos, -observation["remaining_hp_fraction"]
        baseline = min(actions, key=baseline_key)
    baseline_rows = outcomes[_action_key(baseline)]
    enemy_move_counts = Counter()
    enemy_move_kos = Counter()
    ai_counts = Counter()
    for sample in baseline_rows:
        actions_seen = sample["actions"]
        enemy_action = next((entry for entry in actions_seen if entry["side"] == 1), None)
        if enemy_action:
            key = (enemy_action["kind"], enemy_action["id"])
        else:
            key = ("none", 0)
        ai_counts[key] += 1
        if key[0] == "move":
            enemy_move_counts[key[1]] += 1
            enemy_move_kos[key[1]] += sample["parties"][0].get(0, {}).get("hp", 1) == 0
    reverse_moves = {value: key for key, value in run_dynamic.ID_MAP["ids"]["moves"].items()}
    reverse_items = {value: key for key, value in run_dynamic.ID_MAP["ids"]["items"].items()}
    enemy_rows = []
    for move_id in dict.fromkeys(enemy_party[0].get("moves") or []):
        if move_id == "MOVE_NONE":
            continue
        numeric = run_dynamic.numeric_id("moves", move_id)
        total = enemy_move_counts[numeric]
        if not total:
            enemy_rows.append({"move_id": move_id,
                               "move_name": (game_data.get("moves", {}).get(move_id) or {}).get("name", move_id),
                               "status": "unavailable", "reason": "insufficient-samples",
                               "detail": "The trainer AI did not select this move in the completed trajectory set.",
                               "missing": []})
            continue
        row = {"move_id": move_id,
               "move_name": (game_data.get("moves", {}).get(move_id) or {}).get("name", move_id),
               "method": "known-seed" if known_seed else "estimated",
               "ko": "Player active reaches 0 HP when this move is selected."}
        row.update(_observed_rate(enemy_move_kos[numeric], total, known_seed))
        if not known_seed:
            row.update(status="estimated", sampler_id="paired-sfc32-v1")
        else:
            row["deterministic"] = True
        enemy_rows.append(row)
    ai_rows = []
    for (kind, numeric), count in ai_counts.items():
        if kind == "item":
            source_id = reverse_items.get(numeric, f"ID_{numeric}")
            label = rom_query.item_label(game_data, source_id)
        elif kind == "move":
            source_id = reverse_moves.get(numeric, f"ID_{numeric}")
            label = (game_data.get("moves", {}).get(source_id) or {}).get("name", source_id)
        elif kind == "switch":
            source_id = f"SLOT_{numeric + 1}"
            label = f"Switch to party slot {numeric + 1}"
        else:
            source_id = f"SLOT_{numeric + 1}"
            label = "No action (battle ended)"
        ai_rows.append({"kind": kind, "id": source_id, "label": label,
                        "probability": _observed_rate(count, len(baseline_rows), known_seed),
                        "top": count == max(ai_counts.values()),
                        **({"deterministic": True} if known_seed else {})})
    rows = []
    for action in actions:
        key = _action_key(action)
        trajectories = outcomes[key]
        if not trajectories:
            continue
        observed = _action_observations(action, trajectories, player_party, enemy_party, known_seed)
        estimates = [estimate for cards in (post_queries or {}).get(key, [])
                     if (estimate := _followup_estimate(
                         snapshot, game_data, action, cards)) is not None]
        best_followup = next((estimate for estimate in estimates
                              if estimate.get("followup_action")), None)
        if estimates:
            status_values = [estimate["matchup_estimate"] for estimate in estimates]
            observed["matchup_estimate"] = (
                "estimated-stable" if status_values and all(
                    value == "estimated-stable" for value in status_values)
                else next((value for value in status_values
                           if value != "estimated-stable"), "uncertain"))
        values = scores[key]
        mean = sum(values) / len(values) if values else 0.0
        variance = sum((value - mean) ** 2 for value in values) / max(1, len(values) - 1)
        radius = 0.0 if known_seed else 1.96 * math.sqrt(variance / len(values))
        enemy_counts = Counter()
        for sample in trajectories:
            enemy_action = next((entry for entry in sample["actions"] if entry["side"] == 1), None)
            if enemy_action:
                enemy_counts[(enemy_action["kind"], enemy_action["id"])] += 1
        enemy_distribution = []
        for (kind, numeric), count in enemy_counts.items():
            source_id = ({"move": reverse_moves, "item": reverse_items}.get(kind, {})).get(
                numeric, f"SLOT_{numeric + 1}")
            enemy_distribution.append({"kind": kind, "id": source_id,
                                       "probability": _observed_rate(count, len(trajectories), known_seed)})
        row = {"kind": action["kind"], "id": action.get("move_id", action.get("slot")),
               "label": _label(action, game_data, snapshot), "score": {"mean": mean},
               "score_interval_95": {"low": mean - radius, "high": mean + radius},
               "death_risk": observed["death_risk"],
               "first_turn_death_risk": observed["death_risk"],
               "ranking_death_risk": _ranking_death_risk(
                   observed["death_risk"],
                   best_followup.get("followup_death_risk") if best_followup else None,
                   known_seed),
               "ko_before_faint": observed["ko_before_faint"],
               "probability": observed["ko_before_faint"],
                "turns_to_ko": ({"estimate": round(sum(e["turns_to_ko"]["estimate"] for e in estimates
                                                            if e.get("turns_to_ko")) /
                                                    max(1, sum(bool(e.get("turns_to_ko")) for e in estimates)), 2),
                                 "basis": best_followup["turns_to_ko"]["basis"]}
                                if best_followup and best_followup.get("turns_to_ko") else None),
                "matchup_estimate": observed["matchup_estimate"],
                "followup_action": best_followup["followup_action"] if best_followup else None,
                "followup_ko_before_faint": (
                    best_followup.get("followup_ko_before_faint") if best_followup else None),
                "followup_viability": (best_followup.get("followup_viability")
                                       if best_followup else None),
                "followup_death_risk": (best_followup.get("followup_death_risk")
                                        if best_followup else None),
               "enemy_action_distribution": enemy_distribution,
               "ordered_faints": [{"sample": index + 1, **faint}
                                  for index, sample in enumerate(trajectories)
                                  for faint in sample.get("faints", [])],
               "effective_samples": len(trajectories),
               "remaining_hp_fraction": observed["remaining_hp_fraction"]}
        if known_seed:
            row["deterministic"] = True
            row["method"] = "known-seed"
        rows.append(row)
    def risk_estimate(row):
        ranking_risk = row["ranking_death_risk"]
        return (ranking_risk["estimate"] if ranking_risk["estimate"] is not None else
                ranking_risk["range"]["high"])

    ordered = sorted(rows, key=lambda row: (
        risk_estimate(row),
        -(row["followup_viability"] if row["followup_viability"] is not None else
          row["ko_before_faint"]["num"] / row["ko_before_faint"]["den"]),
        float("inf") if row["turns_to_ko"] is None else row["turns_to_ko"]["estimate"],
        -row["remaining_hp_fraction"]))
    leaders = ([row for row in ordered
                if _intervals_overlap(ordered[0]["ranking_death_risk"],
                                      row["ranking_death_risk"])]
               if ordered else [])
    uncertainty_reasons = []
    if not known_seed and len(leaders) > 1:
        uncertainty_reasons.append("overlapping-risk")
    if any(row.get("matchup_estimate") == "unsupported-changing-effect" for row in ordered):
        uncertainty_reasons.append("unsupported-followup")
    if any(row.get("matchup_estimate") not in ("estimated-stable", "unsupported-changing-effect")
           for row in ordered):
        uncertainty_reasons.append("insufficient-matchup")
    if any((not row.get("followup_death_risk") or
            row["followup_death_risk"].get("estimate") is None or
            row["followup_death_risk"].get("range", {}).get("high", 1.0) -
            row["followup_death_risk"].get("range", {}).get("low", 0.0) > 0.05)
           for row in ordered):
        uncertainty_reasons.append("uncertain-followup-risk")
    uncertain = bool(uncertainty_reasons)
    unique_switch = False
    if ordered and ordered[0]["kind"] == "switch":
        switch = ordered[0]
        move_rows_ranked = [row for row in ordered if row["kind"] == "move"]
        risk_proven = False
        viable_ko = False
        if move_rows_ranked:
            safest_move = min(move_rows_ranked, key=risk_estimate)
            switch_risk = switch["ranking_death_risk"]["range"]
            move_risk = safest_move["ranking_death_risk"]["range"]
            risk_proven = switch_risk["high"] < move_risk["low"]
            viable_ko = (switch.get("followup_viability") is not None and
                         switch["followup_viability"] > 0 and
                         switch.get("matchup_estimate") == "estimated-stable")
        unique_switch = risk_proven and viable_ko
        if not unique_switch:
            uncertain = True
            uncertainty_reasons.append("switch-not-proven")
            if switch not in leaders:
                leaders.append(switch)
    for index, row in enumerate(ordered):
        row["top_option"] = row in leaders if uncertain else index == 0
        row["uniquely_recommended"] = index == 0 and (row["kind"] != "switch" or unique_switch)
    score_rows = ordered
    status = "engine-fixed-seed" if known_seed else "estimated"
    actual_samples = min((len(outcomes[_action_key(action)]) for action in actions), default=0)
    meta = {} if known_seed else {"effective_samples": actual_samples,
                                 "samples": actual_samples, "sampler_id": "paired-sfc32-v1"}
    has_followup = any(row.get("followup_action") for row in ordered)
    return {
        "player_moves": {"status": status, **meta, **({"deterministic": True} if known_seed else {}), "moves": move_rows},
        "enemy_moves": {"status": status, **meta, **({"deterministic": True} if known_seed else {}), "moves": enemy_rows},
        "ai_actions": {"status": status, **meta, **({"deterministic": True} if known_seed else {}),
                       "conditioned_on": ("the planned player action" if planned else
                                          f"recommended action: {_label(baseline, game_data, snapshot)}"),
                       "actions": ai_rows},
        "recommendation": {"status": status, **meta, "critical_policy": "item-ability-only",
                           "interval_95": (ordered[0]["death_risk"].get("interval_95")
                                           if ordered else None),
                           "ranking_basis": (
                               "combined faint risk = first-turn risk + (surviving first turn × conditional follow-up risk); then KO-before-faint, turns to KO, and remaining HP"),
                           "ranking_uncertain": uncertain,
                           "uncertainty_reason": uncertainty_reasons,
                           "conditioned_on": ("one deterministic fixed-seed turn per legal first action"
                                               if known_seed else
                                               "paired legal first actions and pinned-engine trainer responses"),
                           "label": ("one-turn outcomes plus measured follow-up matchup estimates"
                                     if has_followup else
                                     "one-turn risk ranking; follow-up estimate unavailable"),
                           "scored_turns": 1, "actions": score_rows},
    }


_POOLS = {}
_POOL_LOCK = threading.Lock()


def _engine_paths():
    elf = Path(os.environ.get(
        "SIDEDEX_ENGINE_ELF_PATH",
        Path(__file__).parent / "native" / "rom_worker" / "build" / "mingw" /
        "pokeemerald-test-headless.elf"))
    worker_path = Path(os.environ.get("SIDEDEX_ROM_WORKER_EXE", rom_bridge.DEFAULT_WORKER))
    if not elf.is_file():
        raise FileNotFoundError(
            "missing-artifact: copy the pinned pokeemerald-test-headless.elf "
            f"(SHA-256 {rom_bridge.ENGINE_ELF_SHA256}) to native/rom_worker/build/mingw/ "
            "or set SIDEDEX_ENGINE_ELF_PATH; see tools/rom_oracle/README.md")
    if not worker_path.is_file():
        raise FileNotFoundError(
            "missing-artifact: build rom_worker.exe with build.ps1 or set "
            "SIDEDEX_ROM_WORKER_EXE; see tools/rom_oracle/README.md")
    return elf.resolve(), worker_path.resolve()


def _boot_engine(deadline, elf, worker_path):
    client = rom_bridge.RomWorkerClient(
        worker_path=worker_path, rom_path=elf,
        timeout=max(0.1, deadline - time.monotonic()),
        expected_artifact_sha256=rom_bridge.ENGINE_ELF_SHA256,
        expected_worker_sha256=rom_bridge.WORKER_SHA256,
    )
    try:
        for _ in range(400):
            if time.monotonic() >= deadline:
                raise TimeoutError("timeout: engine did not announce its request mailbox")
            ready = [line for line in run_dynamic._trace(client.run_frames(60))
                     if line.startswith("ROMTRACE|ready|")]
            if ready:
                fields = ready[-1].split("|")
                if len(fields) != 4 or int(fields[3]) != run_dynamic.MAILBOX_BYTES:
                    raise ValueError("engine-error: invalid mailbox announcement")
                mailbox = int(fields[2])
                client.save_state()
                return client, mailbox
        raise TimeoutError("timeout: engine did not announce its request mailbox")
    except BaseException:
        client.close()
        raise


def _engine_pool():
    elf, worker_path = _engine_paths()
    key = (str(elf), str(worker_path), rom_bridge.ENGINE_ELF_SHA256, rom_bridge.WORKER_SHA256)
    with _POOL_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = rom_pool.EnginePool(lambda deadline: _boot_engine(deadline, elf, worker_path), size=3)
            _POOLS[key] = pool
            pool.prewarm()
    return pool


def prewarm():
    """Boot the three pinned workers in the background when Calc opens."""
    _engine_pool()


def _engine_session(deadline):
    return _engine_pool().session(deadline)


def _seed_pairs(snapshot, count):
    rng = (snapshot.get("rng") or {})
    main = run_dynamic._ints(rng["main"], 4, 0xFFFFFFFF) if rng.get("main") is not None else None
    secondary = run_dynamic._ints(rng["secondary"], 4, 0xFFFFFFFF) if rng.get("secondary") is not None else None
    known = main is not None and secondary is not None
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).digest()
    generator = random.Random(int.from_bytes(digest, "big"))
    pairs = []
    for _ in range(1 if known else count):
        first = main or [generator.getrandbits(32) for _ in range(4)]
        second = secondary or [generator.getrandbits(32) for _ in range(4)]
        if not any(first):
            first[0] = 1
        pairs.append((first, second))
    return pairs, known


def evaluate(snapshot, game_data, deadline, cancelled=None, publish=None):
    """Evaluate paired legal actions; `deadline` is an absolute monotonic time."""
    source = snapshot.get("source_sha") if isinstance(snapshot, dict) else None
    if isinstance(snapshot, dict):
        snapshot = _canonical_moves(_prepare_snapshot(snapshot), game_data)
    issue = _unsupported(snapshot, game_data) if isinstance(snapshot, dict) else (
        "incomplete-state", "The battle snapshot is invalid.", ["snapshot"])
    if issue:
        reason, detail, missing = issue
        return _result(source, "unavailable", **{kind: _unavailable(reason, detail, missing) for kind in _CARDS})
    actions = _actions(snapshot)
    if not actions:
        card = _unavailable("incomplete-state", "No legal move or switch is available.", ["player.party[0].moves"])
        return _result(source, "unavailable", **{kind: card for kind in _CARDS})
    known = all((snapshot.get("rng") or {}).get(key) is not None for key in ("main", "secondary"))
    party_sizes = (len(snapshot["player"]["party"]), len(snapshot["enemy"]["party"]))
    initial_parties = (snapshot["player"]["party"], snapshot["enemy"]["party"])
    per_seed = max(1, len(actions))
    if known:
        count = 1
    else:
        affordable = int(max(0, deadline - time.monotonic() - 1.0) * 1000 / (_TURN_MS * per_seed))
        count = min(_MAX_SAMPLES, max(MIN_ESTIMATE_SAMPLES, affordable))
    action_keys = {_action_key(action): action for action in actions}
    outcomes = {key: [] for key in action_keys}
    scores = {key: [] for key in action_keys}
    paired_outcomes = {key: [] for key in action_keys}
    paired_scores = {key: [] for key in action_keys}
    published_samples = set()
    query_cards = None
    post_queries = {key: [] for key in action_keys}
    request_no = 0
    failure = None
    previous_seed_seconds = None

    def _guard():
        if cancelled and cancelled():
            raise TimeoutError("timeout: calculation was cancelled after a snapshot edit")
        if time.monotonic() >= deadline:
            raise TimeoutError("timeout: calculation deadline expired")

    def _publish_refinement():
        completed = min(len(paired_outcomes[key]) for key in action_keys)
        if (not publish or completed < (1 if known else MIN_ESTIMATE_SAMPLES)
                or completed in published_samples):
            return
        published_samples.add(completed)
        summary = _summarize(snapshot, game_data, actions, seeds, paired_outcomes,
                             paired_scores, known, post_queries)
        if query_cards:
            summary.update({kind: query_cards[kind] for kind in ("player_moves", "enemy_moves")})
        if snapshot["enemy"].get("items"):
            if query_cards:
                preview = dict(query_cards["ai_actions"])
                preview["preview"] = True
                preview["conditioned_on"] = "one query seed at the entered battle state"
                summary["ai_actions"] = preview
            summary["recommendation"] = _unavailable(
                "unsupported-mechanic",
                "Trainer bag items can change the matchup; resolved recommendation turns do not simulate item use.",
                ["recommendation"])
        status = "engine-fixed-seed" if known else "estimated"
        progress = {"completed_samples": completed,
                    "target_samples": len(seeds), "stage": "sampling"}
        result = _result(source, status, _meta={
            "scenario": _scenario(snapshot), "assumed_lead": True, "progress": progress,
            "refining": True, "phase": "Sampling turns",
            "completed_samples": completed, "target_samples": len(seeds)},
            **summary)
        try:
            publish(result)
        except Exception:
            pass

    try:
        seeds, known = _seed_pairs(snapshot, count)
        with _engine_session(deadline) as (client, mailbox):
            if getattr(client, "rom_sha256", None) == rom_bridge.ENGINE_ELF_SHA256:
                try:
                    _guard()
                    query_cards = _query_cards(client, mailbox, snapshot, game_data,
                                               actions[0], seeds[0], deadline)
                except (ValueError, rom_bridge.RomBridgeError):
                    query_cards = None
                if query_cards and publish:
                    preview = dict(query_cards["ai_actions"])
                    preview["conditioned_on"] = "one query seed at the entered battle state"
                    preview["preview"] = True
                    try:
                        publish(_result(source, "refining", _meta={
                            "scenario": _scenario(snapshot), "assumed_lead": True,
                            "refining": True, "phase": "Sampling turns",
                            "completed_samples": 0, "target_samples": len(seeds)},
                            player_moves=query_cards["player_moves"],
                            enemy_moves=query_cards["enemy_moves"],
                            ai_actions=preview,
                            recommendation={"status": "pending"}))
                    except Exception:
                        pass
            for seed in seeds:
                completed = len(paired_scores[next(iter(action_keys))])
                if (not known and completed >= MIN_ESTIMATE_SAMPLES
                        and previous_seed_seconds is not None
                        and time.monotonic() + max(0.5, previous_seed_seconds * 1.6 + 0.5) >= deadline):
                    break
                seed_started = time.monotonic()
                seed_turns = {}
                for start in range(0, len(actions), _MAX_BATCH):
                    _guard()
                    batch = actions[start:start + _MAX_BATCH]
                    payloads = [run_dynamic.encode_request(_request(snapshot, game_data, action, seed))
                                for action in batch]
                    request_no += 1
                    try:
                        reply = _run_batch(client, mailbox, payloads, deadline, f"calc-{request_no}")
                    except (TimeoutError, rom_bridge.WorkerTimeoutError,
                            rom_bridge.WorkerResponseError) as exc:
                        if isinstance(exc, rom_bridge.WorkerResponseError) and exc.code != "deadline_exceeded":
                            raise
                        failure = exc
                        reply = getattr(exc, "reply", None) or getattr(exc, "partial_reply", None)
                        if not isinstance(reply, dict):
                            break
                    for action, turn in zip(batch, reply["turns"]):
                        if turn.get("complete"):
                            try:
                                parsed = _parse_turn(turn.get("events"), party_sizes)
                                if parsed["critical_policy_verified"]:
                                    seed_turns[_action_key(action)] = parsed
                            except ValueError:
                                continue
                for key, turn in seed_turns.items():
                    outcomes[key].append(turn)
                for key, turn in seed_turns.items():
                    scores[key].append(_score(turn, *initial_parties))
                if len(seed_turns) == len(action_keys):
                    for key, turn in seed_turns.items():
                        paired_outcomes[key].append(turn)
                        paired_scores[key].append(scores[key][-1])
                completed_pairs = len(paired_outcomes[next(iter(action_keys))])
                query_budget = max(2.0, previous_seed_seconds * 1.5) if previous_seed_seconds else 2.0
                if (len(post_queries[next(iter(action_keys))]) == 0 and
                        len(seed_turns) == len(action_keys) and
                        (known or completed_pairs >= MIN_ESTIMATE_SAMPLES) and
                        time.monotonic() + query_budget < deadline and
                        not snapshot["enemy"].get("items")):
                    follow_payloads, follow_keys = [], []
                    for action in actions:
                        key = _action_key(action)
                        request = _followup_query_payload(
                            snapshot, game_data, action, seed_turns[key], seed)
                        if request is None:
                            continue
                        try:
                            follow_payloads.append(run_dynamic.encode_request(request))
                            follow_keys.append(key)
                        except (ValueError, KeyError):
                            continue
                    if follow_payloads:
                        try:
                            _guard()
                            follow_reply = _run_batch(
                                client, mailbox, follow_payloads, deadline,
                                f"calc-followup-{request_no + 1}")
                            for key, turn in zip(follow_keys, follow_reply["turns"]):
                                events = turn.get("events") or []
                                if (not turn.get("complete") and
                                        "ROMTRACE|query|complete" not in events):
                                    continue
                                cards = rom_query.parse(events, snapshot, game_data)
                                if (cards.get("critical_policy_verified") and
                                        cards.get("query_context") in
                                        ("post-switch", "after-candidate-turn", "current-state")):
                                    post_queries[key].append(cards)
                        except (TimeoutError, ValueError, rom_bridge.RomBridgeError):
                            pass
                _publish_refinement()
                previous_seed_seconds = time.monotonic() - seed_started
                if failure:
                    break
                if not known and len(paired_scores[next(iter(action_keys))]) >= MIN_ESTIMATE_SAMPLES:
                    current = _summarize(snapshot, game_data, actions, seeds,
                                         paired_outcomes, paired_scores, False, post_queries)
                    if not current["recommendation"]["ranking_uncertain"]:
                        break
        required = 1 if known else MIN_ESTIMATE_SAMPLES
        usable = [action for action in actions
                  if len(outcomes[_action_key(action)]) >= required
                  and len(scores[_action_key(action)]) >= required]
        if not usable:
            have_any = any(outcomes.values())
            reason = "timeout" if failure and not have_any else "insufficient-samples"
            detail = (str(failure) if reason == "timeout" else
                      f"No action has {required} complete samples; received "
                      f"{max((len(rows) for rows in outcomes.values()), default=0)}.")
            card = _unavailable(reason, detail)
            partial = {kind: card for kind in _CARDS}
            if query_cards:
                partial.update({kind: query_cards[kind] for kind in ("player_moves", "enemy_moves")})
                preview = dict(query_cards["ai_actions"])
                preview["preview"] = True
                preview["conditioned_on"] = "one query seed at the entered battle state"
                partial["ai_actions"] = preview
            return _result(source, "unavailable", **partial)
        summary = _summarize(snapshot, game_data, usable, seeds, outcomes, scores, known,
                             post_queries)
        if query_cards:
            summary.update({kind: query_cards[kind] for kind in ("player_moves", "enemy_moves")})
        if len(paired_outcomes[next(iter(action_keys))]) >= required:
            summary["recommendation"] = _summarize(
                snapshot, game_data, actions, seeds, paired_outcomes, paired_scores,
                known, post_queries)["recommendation"]
        else:
            summary["recommendation"] = _unavailable(
                "insufficient-samples", "Fewer than three complete paired samples cover every legal action."
                if not known else "A complete paired sample did not cover every legal action.")
        status = "engine-fixed-seed" if known else "estimated"
        for kind in _CARDS:
            if query_cards and kind in ("player_moves", "enemy_moves"):
                continue
            if summary[kind].get("status") != status:
                summary[kind] = _unavailable(
                    "timeout" if failure else "insufficient-samples",
                    str(failure) if failure else
                    f"Fewer than {required} complete paired samples were returned.")
        # Resolved turns do not simulate trainer item use, so any entered bag
        # makes the sampling-derived recommendation item-agnostic.
        if snapshot["enemy"].get("items"):
            if query_cards:
                preview = dict(query_cards["ai_actions"])
                preview["preview"] = True
                preview["conditioned_on"] = "one query seed at the entered battle state"
                summary["ai_actions"] = preview
            summary["recommendation"] = _unavailable(
                "unsupported-mechanic",
                "Trainer bag items can change the matchup; resolved recommendation turns do not simulate item use.",
                ["recommendation"])
        return _result(source, status, _meta={
            "scenario": _scenario(snapshot), "assumed_lead": True}, **summary)
    except FileNotFoundError as exc:
        reason = "missing-artifact" if "missing-artifact" in str(exc) else "engine-error"
        card = _unavailable(reason, str(exc))
    except rom_bridge.RomIdentityError as exc:
        reason = "artifact-mismatch" if "SHA-256" in str(exc) else "source-mismatch"
        card = _unavailable(reason, str(exc))
    except (TimeoutError, rom_bridge.WorkerTimeoutError) as exc:
        card = _unavailable("timeout", str(exc))
        partial = {kind: card for kind in _CARDS}
        if query_cards:
            partial.update({kind: query_cards[kind] for kind in ("player_moves", "enemy_moves")})
            preview = dict(query_cards["ai_actions"])
            preview.update(preview=True, deterministic=True,
                           conditioned_on="one fixed-seed query at the entered battle state")
            partial["ai_actions"] = preview
            partial["recommendation"] = _unavailable(
                "insufficient-samples",
                "Sampling timed out before enough paired action outcomes were available.",
                ["recommendation"])
        return _result(source, "unavailable", _meta={
            "scenario": _scenario(snapshot) if isinstance(snapshot, dict) else None,
            "assumed_lead": True,
            "progress": {"completed_samples": min(
                (len(paired_outcomes[key]) for key in paired_outcomes), default=0),
                "target_samples": len(seeds) if "seeds" in locals() else 0}}, **partial)
    except ValueError as exc:
        message = str(exc)
        reason = next((code for code in ("invalid-id", "incomplete-state", "no-legal-actions")
                       if code in message), "engine-error")
        card = _unavailable(reason, message)
    except (rom_bridge.RomBridgeError, OSError, RuntimeError) as exc:
        card = _unavailable("engine-error", str(exc))
    return _result(source, "unavailable", **{kind: card for kind in _CARDS})
