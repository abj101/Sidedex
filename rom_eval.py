"""Small, fail-closed evaluator for the local compiled battle test ELF."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from collections import Counter
from pathlib import Path

import rom_bridge
from tools.rom_oracle import run_dynamic


_CARDS = ("player_moves", "enemy_moves", "ai_actions", "recommendation")
_STATS = ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")
_MAX_BATCH = 8
_MAX_SAMPLES = 8


def _unavailable(reason, detail, missing=()):
    return {"status": "unavailable", "reason": reason, "detail": detail,
            "missing": list(missing)}


def _result(source_sha, status, **cards):
    return {"status": status, "source_sha": source_sha,
            **{kind: cards.get(kind, _unavailable("incomplete-state", "No result was produced."))
               for kind in _CARDS}}


def _unsupported(snapshot, game_data):
    if snapshot.get("schema_version") != 2:
        return "incomplete-state", "The evaluator requires a schema-v2 battle snapshot.", []
    if snapshot.get("source_sha") != (game_data.get("source") or {}).get("commit"):
        return "source-mismatch", "The snapshot and game data use different pinned sources.", []
    if snapshot.get("difficulty") not in ("Easy", "Normal"):
        return "incomplete-state", "This compiled scenario supports Easy or Normal difficulty.", ["difficulty"]
    player = snapshot.get("player") or {}
    enemy = snapshot.get("enemy") or {}
    if player.get("active_index") != 0 or enemy.get("active_index") != 0:
        return "incomplete-state", "The current oracle supports the first party slot as active on both sides.", ["active_index"]
    if not snapshot.get("progress", {}).get("milestone_id"):
        return "incomplete-state", "The selected numeric level cap is not a ROM progression milestone.", ["progress.milestone_id"]
    if snapshot.get("state_origin") not in ("battle-start", "manual-scenario"):
        return "incomplete-state", "The battle origin is unknown.", ["state_origin"]
    if snapshot.get("state_origin") == "manual-scenario":
        return "incomplete-state", "Direct midbattle state cannot be reconstructed from this engine request.", ["state_origin"]
    turns = (snapshot.get("history") or {}).get("turns", [])
    if not isinstance(turns, list) or len(turns) > 1:
        return "incomplete-state", "The current engine can replay at most one observed turn.", ["history.turns"]
    field = snapshot.get("field") or {}
    if enemy.get("items"):
        return "incomplete-state", "Trainer item counts are not represented by the engine hook.", ["enemy.items"]
    if any(field.get(key) not in (None, [], False) for key in
           ("weather", "terrain", "trick_room", "player_screens", "enemy_screens",
            "player_hazards", "enemy_hazards")):
        return "incomplete-state", "The current engine request cannot encode this field state.", ["field"]
    for side_name, side in (("player", player), ("enemy", enemy)):
        party = side.get("party")
        if not isinstance(party, list) or not 1 <= len(party) <= 6:
            return "incomplete-state", f"{side_name} party must contain one to six complete builds.", [f"{side_name}.party"]
        for index, mon in enumerate(party):
            if mon.get("stat_stages"):
                return "incomplete-state", "The engine request cannot encode stat stages.", [f"{side_name}.party[{index}].stat_stages"]
            if mon.get("status") not in (None, "none") and mon.get("status") not in run_dynamic.ID_MAP["ids"]["statuses"]:
                return "invalid-id", f"Unknown status on {side_name} party slot {index}.", [f"{side_name}.party[{index}].status"]
            if snapshot.get("state_origin") != "battle-start" and mon.get("status") is None:
                return "incomplete-state", "A non-start scenario is missing a known status value.", [f"{side_name}.party[{index}].status"]
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
        elif origin == "battle-start":
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
            "nature": mon["nature"], "hp": mon["hp"], "max_hp": mon["max_hp"],
            "status1": status1, "moves": moves, "pp": pp,
            "ivs": [ivs.get(stat, 0) for stat in _STATS],
            "evs": [evs.get(stat, 0) for stat in _STATS]}


def _request(snapshot, game_data, action, seed):
    turns = (snapshot.get("history") or {}).get("turns", [])
    party = snapshot["player"]["party"]
    enemy = snapshot["enemy"]
    return {"ai_flags": _mask(enemy["ai_flags"]), "candidate_action": action,
            "replay_turns": turns,
            "player_active_index": snapshot["player"]["active_index"],
            "opponent_active_index": enemy["active_index"],
            "trainer_items": enemy.get("items") or {},
            "rng_main": seed[0], "rng_secondary": seed[1], "field": _field(snapshot),
            "player_party": [_mon(mon, game_data, snapshot.get("state_origin")) for mon in party],
            "opponent_party": [_mon(mon, game_data, snapshot.get("state_origin"))
                               for mon in enemy["party"]]}


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


def _action_key(action):
    fields = ("kind", "move_id") if action.get("kind") == "move" else ("kind", "slot")
    return json.dumps({key: action[key] for key in fields}, sort_keys=True)


def _parse_turn(events, party_sizes):
    if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
        raise ValueError("engine-error: malformed turn events")
    done = [int(row.split("|")[2]) for row in events if row.startswith("ROMTRACE|complete|")]
    if len(done) != 1:
        raise ValueError("incomplete-state: turn did not have exactly one complete marker")
    turn = done[0]
    if turn < 1:
        raise ValueError("incomplete-state: turn counter did not advance")
    # The engine snapshots the resulting state as turn N and labels the choices
    # that produced it as turn N-1, so the resolved actions live on turn - 1.
    action_turn = turn - 1
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
    if not actions or len(records) < 2:
        raise ValueError("incomplete-state: outcome lacks actions or both resulting parties")
    for side, size in enumerate(party_sizes):
        party = records.get(side, {})
        if len(party) < size or sorted(party) != list(range(len(party))):
            raise ValueError("incomplete-state: outcome lacks a complete resulting party")
    if not any(line.startswith(f"ROMTRACE|active|{turn}|") for line in events):
        raise ValueError("incomplete-state: outcome lacks active battler state")
    if not any(line.startswith(f"ROMTRACE|rng|{turn}|") for line in events):
        raise ValueError("incomplete-state: outcome lacks resulting RNG state")
    return {"turn": turn, "actions": actions, "parties": records}


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


def _score(outcome, initial_player, initial_enemy):
    def hp_loss(initial, final):
        return sum(mon["hp"] for mon in initial) - sum(final[index]["hp"] for index in range(len(initial)))
    player_loss = hp_loss(initial_player, outcome["parties"][0])
    enemy_loss = hp_loss(initial_enemy, outcome["parties"][1])
    player_alive = sum(outcome["parties"][0][i]["hp"] > 0 for i in range(len(initial_player)))
    enemy_alive = sum(outcome["parties"][1][i]["hp"] > 0 for i in range(len(initial_enemy)))
    return float(enemy_loss - player_loss + 100 * (player_alive - enemy_alive))


def _label(action, game_data):
    if action["kind"] == "move":
        return (game_data.get("moves", {}).get(action["move_id"]) or {}).get("name", action["move_id"])
    return f"Switch to party slot {action['slot'] + 1}"


def _summarize(snapshot, game_data, actions, samples, outcomes, known_seed):
    enemy_party = snapshot["enemy"]["party"]
    player_party = snapshot["player"]["party"]
    move_rows = []
    for action in actions:
        if action["kind"] != "move":
            continue
        trajectories = outcomes[_action_key(action)]
        kos = sum(enemy_party[0]["hp"] > 0 and sample["parties"][1].get(0, {}).get("hp") == 0
                  for sample in trajectories)
        row = {"move_id": action["move_id"], "move_name": _label(action, game_data),
               "method": "known-seed" if known_seed else "estimated",
               "ko": "Opponent party member reaches 0 HP in the completed turn."}
        row.update(_proportion(kos, len(trajectories)))
        if not known_seed:
            row.update(status="estimated", sampler_id="paired-sfc32-v1")
        move_rows.append(row)
    baseline = snapshot.get("planned_action")
    baseline = next((action for action in actions if baseline and
                     _action_key(action) == _action_key(baseline)), None)
    if baseline is None:
        baseline = next((action for action in actions if action["kind"] == "move"), actions[0])
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
        row.update(_proportion(enemy_move_kos[numeric], total))
        if not known_seed:
            row.update(status="estimated", sampler_id="paired-sfc32-v1")
        enemy_rows.append(row)
    ai_rows = []
    for (kind, numeric), count in ai_counts.items():
        source_id = reverse_moves.get(numeric, f"ID_{numeric}") if kind == "move" else f"SLOT_{numeric + 1}"
        ai_rows.append({"kind": kind, "id": source_id,
                        "label": ((game_data.get("moves", {}).get(source_id) or {}).get("name", source_id)
                                  if kind == "move" else f"Switch to party slot {numeric + 1}"
                                  if kind == "switch" else "No action (battle ended)"),
                        "probability": {"num": count, "den": len(baseline_rows)},
                        "top": count == max(ai_counts.values())})
    score_rows = []
    summaries = []
    for action in actions:
        values = [_score(sample, player_party, enemy_party) for sample in outcomes[_action_key(action)]]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / max(1, len(values) - 1)
        radius = 0.0 if known_seed else 1.96 * math.sqrt(variance / len(values))
        summaries.append((action, values, mean, {"low": mean - radius, "high": mean + radius}))
    ordered = sorted(summaries, key=lambda row: row[2], reverse=True)
    uncertain = False
    if len(ordered) > 1 and not known_seed:
        difference = [left - right for left, right in zip(ordered[0][1], ordered[1][1])]
        mean_diff = sum(difference) / len(difference)
        var_diff = sum((value - mean_diff) ** 2 for value in difference) / max(1, len(difference) - 1)
        uncertain = mean_diff - 1.96 * math.sqrt(var_diff / len(difference)) <= 0
    for action, _values, mean, interval in ordered:
        score_rows.append({"kind": action["kind"], "id": action.get("move_id", action.get("slot")),
                           "label": _label(action, game_data), "score": {"mean": mean},
                           "score_interval_95": interval})
    status = "engine-fixed-seed" if known_seed else "estimated"
    meta = {} if known_seed else {"effective_samples": len(samples), "sampler_id": "paired-sfc32-v1"}
    return {
        "player_moves": {"status": status, **meta, "moves": move_rows},
        "enemy_moves": {"status": status, **meta, "moves": enemy_rows},
        "ai_actions": {"status": status, **meta,
                       "conditioned_on": "the planned player action",
                       "actions": ai_rows},
        "recommendation": {"status": status, **meta, "ranking_uncertain": uncertain,
                           "label": "One-turn HP and survivor objective", "scored_turns": 1,
                           "actions": score_rows},
    }


def _engine_session(deadline):
    import contextlib

    @contextlib.contextmanager
    def session():
        elf = Path(os.environ.get("SIDEDEX_ENGINE_ELF_PATH", ""))
        worker_path = Path(os.environ.get("SIDEDEX_ROM_WORKER_EXE", rom_bridge.DEFAULT_WORKER))
        if not elf.is_file() or not worker_path.is_file():
            raise FileNotFoundError("missing-artifact: set SIDEDEX_ENGINE_ELF_PATH and build the local rom_worker")
        timeout = max(0.1, min(5.0, deadline - time.monotonic()))
        with rom_bridge.RomWorkerClient(
            worker_path=worker_path, rom_path=elf, timeout=timeout,
            expected_artifact_sha256=rom_bridge.ENGINE_ELF_SHA256,
            expected_worker_sha256=rom_bridge.WORKER_SHA256,
        ) as client:
            mailbox = None
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
                    break
            if mailbox is None:
                raise TimeoutError("timeout: engine did not announce its request mailbox")
            client.save_state()
            yield client, mailbox
    return session()


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


def evaluate(snapshot, game_data, deadline, cancelled=None):
    """Evaluate paired legal actions; `deadline` is an absolute monotonic time."""
    source = snapshot.get("source_sha") if isinstance(snapshot, dict) else None
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
    estimated_turn_ms = 200
    count = 1 if known else min(_MAX_SAMPLES, max(1, int(max(0, deadline - time.monotonic() - 1.0) * 1000
                                                           / max(1, estimated_turn_ms * len(actions)))))
    action_keys = {_action_key(action): action for action in actions}
    outcomes = {key: [] for key in action_keys}
    elapsed_turns = 0
    request_no = 0
    try:
        seeds, known = _seed_pairs(snapshot, count)
        with _engine_session(deadline) as (client, mailbox):
            for sample_index, seed in enumerate(seeds):
                for start in range(0, len(actions), _MAX_BATCH):
                    if cancelled and cancelled():
                        raise TimeoutError("timeout: calculation was cancelled after a snapshot edit")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("timeout: calculation deadline expired")
                    batch_actions = actions[start:start + _MAX_BATCH]
                    requests = [_request(snapshot, game_data, action, seed) for action in batch_actions]
                    payloads = [run_dynamic.encode_request(request) for request in requests]
                    request_no += 1
                    reply = client.batch_turns(
                        mailbox, payloads, mailbox_bytes=run_dynamic.MAILBOX_BYTES,
                        engine_elf_sha256=rom_bridge.ENGINE_ELF_SHA256,
                        deadline_ms=int(deadline * 1000), request_id=f"calc-{request_no}",
                        max_frames=24_000)
                    elapsed_turns += len(payloads)
                    for action, turn in zip(batch_actions, reply["turns"]):
                        if not turn.get("complete"):
                            continue
                        outcomes[_action_key(action)].append(
                            _parse_turn(turn.get("events"),
                                        (len(snapshot["player"]["party"]), len(snapshot["enemy"]["party"]))))
        if not all(outcomes.values()):
            card = _unavailable("insufficient-samples", "No complete engine trajectory was returned.")
            return _result(source, "unavailable", **{kind: card for kind in _CARDS})
        return _result(source, "engine-fixed-seed" if known else "estimated",
                       **_summarize(snapshot, game_data, actions, seeds, outcomes, known))
    except FileNotFoundError as exc:
        reason = "missing-artifact" if "missing-artifact" in str(exc) else "engine-error"
        card = _unavailable(reason, str(exc))
    except rom_bridge.RomIdentityError as exc:
        reason = "artifact-mismatch" if "SHA-256" in str(exc) else "source-mismatch"
        card = _unavailable(reason, str(exc))
    except (TimeoutError, rom_bridge.WorkerTimeoutError) as exc:
        card = _unavailable("timeout", str(exc))
    except ValueError as exc:
        message = str(exc)
        reason = next((code for code in ("invalid-id", "incomplete-state", "no-legal-actions")
                       if code in message), "engine-error")
        card = _unavailable(reason, message)
    except (rom_bridge.RomBridgeError, OSError, RuntimeError) as exc:
        card = _unavailable("engine-error", str(exc))
    return _result(source, "unavailable", **{kind: card for kind in _CARDS})
