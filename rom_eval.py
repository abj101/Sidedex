"""Fail-closed Calc evaluator: exact query cards (v8 pinned ELF) plus a streamed open-loop
recommendation (planner.plan over rom_rollout on the v9 ELF)."""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path

import planner
import rom_bridge
import rom_identity
import rom_pool
import rom_query
import rom_rollout
from tools.rom_oracle import run_dynamic


_CARDS = ("player_moves", "enemy_moves", "ai_actions", "recommendation")
_STATS = ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")
_STAGE_KEYS = ("attack", "defense", "sp_attack", "sp_defense", "speed", "accuracy", "evasion")
# The query cards request this policy; the recommendation rolls normal ROM crits.
CRITICAL_POLICY = "item-ability-only"
ROLLOUT_POLICY = "normal"
_ENGINE_ERRORS = (OSError, RuntimeError, ValueError, TimeoutError, rom_bridge.RomBridgeError)


def _unavailable(reason, detail, missing=()):
    return {"status": "unavailable", "reason": reason, "detail": detail,
            "critical_policy": CRITICAL_POLICY, "missing": list(missing)}


def _result(source_sha, status, **cards):
    meta = cards.pop("_meta", {})
    return {"status": status, "source_sha": source_sha, "critical_policy": CRITICAL_POLICY,
            "_meta": meta, **meta,
            **{kind: cards.get(kind, _unavailable("incomplete-state", "No result was produced."))
               for kind in _CARDS}}


_POOLS = {}
_POOL_LOCK = threading.Lock()


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
    weather = field.get("weather")
    if weather not in (None, "WEATHER_NONE") and (
            rom_rollout.engine() != "native" or weather not in run_dynamic.WEATHER_CODES):
        return "incomplete-state", "Weather needs the native engine and a rain, sun, sandstorm, hail or snow value.", ["field.weather"]
    if any(field.get(key) not in (None, [], False) for key in
           ("terrain", "trick_room", "player_screens", "enemy_screens",
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
            if not any(move != "MOVE_NONE" for move in mon.get("moves") or []):
                name = (game_data.get("species", {}).get(mon.get("species")) or {}).get("name") or mon.get("species")
                return "missing-input", f"{name} has no moves. Add at least one move.", [f"{side_name}.party[{index}].moves"]
    if not isinstance(enemy.get("ai_flags"), list) or "AI_FLAG_UNKNOWN" in enemy["ai_flags"]:
        return "incomplete-state", "The trainer AI flags are not resolved.", ["enemy.ai_flags"]
    if len(turns) and any(not isinstance(turn.get(key), dict) for turn in turns
                           for key in ("player_action", "enemy_action")):
        return "incomplete-state", "A replay turn must contain both observed actions.", ["history.turns"]
    return None


def _field(snapshot):
    # Empty/unknown effects are the clear battle-start state represented by the hook.
    return {"weather": (snapshot.get("field") or {}).get("weather") or "WEATHER_NONE", "terrain": "TERRAIN_NONE", "trick_room": False,
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
               "critical_policy": CRITICAL_POLICY,
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
        mailbox["address"], payloads, mailbox_bytes=run_dynamic.MAILBOX_BYTES,
        engine_elf_sha256=client.rom_sha256,
        deadline_ms=int(deadline * 1000), request_id=request_id, max_frames=24_000)


def _query_cards(client, mailbox, snapshot, game_data, action, seed, deadline):
    request = _request(snapshot, game_data, action, seed)
    request["query"] = True
    payload = run_dynamic.encode_request(request, result_buffer=mailbox)
    reply = _run_batch(client, mailbox, [payload], deadline, "calc-query")
    cards = rom_query.parse(reply["turns"][0]["events"], snapshot, game_data)
    if not cards.get("critical_policy_verified"):
        raise ValueError("unsupported-mechanic: query did not verify item-ability-only critical-hit policy")
    return cards


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


def _canonical_ids(snapshot, game_data):
    # Some extracted alias links point from a pinned name to its alias; never rewrite a pinned name.
    pinned_species = run_dynamic.ID_MAP["ids"]["species"]
    species_aliases = {species_id: row["alias_of"] for species_id, row in game_data.get("species", {}).items()
                       if isinstance(row, dict) and row.get("alias_of") in pinned_species
                       and species_id not in pinned_species}
    aliases = {move_id: row["alias_of"] for move_id, row in game_data.get("moves", {}).items()
               if isinstance(row, dict) and row.get("alias_of")}
    for side_name in ("player", "enemy"):
        for mon in (snapshot.get(side_name) or {}).get("party") or []:
            if mon.get("species") in species_aliases:
                mon["species"] = species_aliases[mon["species"]]
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


def _engine_paths():
    if rom_rollout.engine() == "native":
        return rom_rollout.engine_paths()
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


def _boot_engine(deadline, elf, worker_path, command=None):
    client = rom_bridge.RomWorkerClient(
        worker_path=worker_path, rom_path=elf,
        timeout=max(0.1, deadline - time.monotonic()),
        expected_artifact_sha256=(rom_identity.NATIVE_ENGINE_SHA256 if command else rom_bridge.ENGINE_ELF_SHA256),
        expected_worker_sha256=rom_bridge.WORKER_SHA256, command=command,
    )
    try:
        for _ in range(400):
            if time.monotonic() >= deadline:
                raise TimeoutError("timeout: engine did not announce its request mailbox")
            ready = [line for line in run_dynamic._trace(client.run_frames(60))
                     if line.startswith("ROMTRACE|ready|")]
            if ready:
                mailbox = rom_bridge.parse_v9_ready(ready)  # request + result buffers the ROM announced
                client.save_state()
                # An immutable named pristine-ready slot: every request restores
                # it, because the default slot is overwritten by node installs.
                client.sidedex_ready_handle = client.save_checkpoint("calc-ready")["handle"]
                return client, mailbox
        raise TimeoutError("timeout: engine did not announce its request mailbox")
    except BaseException:
        client.close()
        raise


def _engine_pool():
    elf, worker_path = _engine_paths()
    command = rom_rollout.native_command()
    key = (str(elf), str(worker_path), rom_bridge.ENGINE_ELF_SHA256, rom_bridge.WORKER_SHA256, tuple(command or ()))
    with _POOL_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = rom_pool.EnginePool(lambda deadline: _boot_engine(deadline, elf, worker_path, command), size=1)
            _POOLS[key] = pool
            pool.prewarm()
    return pool


def prewarm():
    """Boot the query worker and the rollout pool in the background when Calc opens."""
    _engine_pool()
    try:
        rom_rollout.engine_pool()
    except FileNotFoundError:
        pass


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


def _error_card(exc):
    """Map an engine failure onto an unavailable card with its contract reason code."""
    message = str(exc)
    if isinstance(exc, FileNotFoundError):
        reason = "missing-artifact" if "missing-artifact" in message else "engine-error"
    elif isinstance(exc, rom_bridge.RomIdentityError):
        reason = "artifact-mismatch" if "SHA-256" in message else "source-mismatch"
    elif isinstance(exc, (TimeoutError, rom_bridge.WorkerTimeoutError)):
        reason = "timeout"
    elif isinstance(exc, ValueError):
        reason = next((code for code in ("invalid-id", "incomplete-state", "no-legal-actions",
                                         "unsupported-mechanic", "identity-mismatch")
                       if code in message), "engine-error")
    else:
        reason = "engine-error"
    return _unavailable(reason, message)


def _query(snapshot, game_data, action, seed, deadline):
    """The exact damage/KO/AI-action query cards from the pinned query worker."""
    with _engine_session(deadline) as (client, mailbox):
        ready_handle = getattr(client, "sidedex_ready_handle", None)
        if ready_handle is not None:
            client.restore_state(ready_handle)
        return _query_cards(client, mailbox, snapshot, game_data, action, seed, deadline)


def _open_rollout(request, deadline):
    """Hold the request at its decision point -> (root, planner rollout over the whole pool)."""
    pool = rom_rollout.engine_pool()
    with pool.session(deadline) as (_client, runner):
        root = rom_rollout.make_root(runner, request)
    return root, rom_rollout.PoolRollout(pool, deadline)


def _word_row(word, game_data, snapshot):
    if word[0] == 0:
        reverse = {value: key for key, value in run_dynamic.ID_MAP["ids"]["moves"].items()}
        move_id = reverse.get(word[1], f"ID_{word[1]}")
        return {"kind": "move", "id": move_id,
                "label": (game_data.get("moves", {}).get(move_id) or {}).get("name", move_id)}
    order = snapshot.get("_player_order")
    original = order[word[1]] if isinstance(order, list) and 0 <= word[1] < len(order) else word[1]
    return {"kind": "switch", "id": word[1], "label": f"Switch to party slot {original + 1}"}


def _card(snap, game_data, snapshot, known):
    """One planner snapshot -> the recommendation card (actions best first)."""
    leading = snap["leading"]
    rows = [{**_word_row(word, game_data, snapshot), "p_win": s["p_win"], "score": {"mean": s["mean"]},
             "hp_left": s["hp_left"], "faints": s["faints"], "half_width": s["half_width"], "samples": s["samples"],
             "top_option": word in leading,
             "uniquely_recommended": snap["done"] and leading == [word]}
            for word, s in sorted(snap["actions"].items(), key=lambda item: -item[1]["mean"])]
    return {"status": "engine-fixed-seed" if known else "estimated", "critical_policy": ROLLOUT_POLICY,
            "method": "open-loop-uct", **({"deterministic": True} if known else {}),
            "ranking_uncertain": len(leading) > 1, "resolved": snap["reason"] == "resolved",
            "final": snap["done"], "samples": snap["seeds"], "actions": rows,
            "label": "P(win) by first action; the trainer AI plays the rest of the battle for both sides"}


def _recommend(snapshot, game_data, action, seed, known, deadline, cancelled, emit):
    if snapshot["enemy"].get("items"):
        return _unavailable("unsupported-mechanic",
                            "Trainer bag items can change the matchup; rollouts do not simulate item use.",
                            ["recommendation"])
    request = {**_request(snapshot, game_data, action, seed), "critical_policy": ROLLOUT_POLICY}
    root, rollout = _open_rollout(request, deadline)
    if known:  # the entered RNG is exact: every seed is that one
        inner, pinned = rollout, {"rng1": seed[0], "rng2": seed[1]}
        rollout = lambda r, items: inner(r, [{**item, **pinned} for item in items])  # noqa: E731
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).digest()
    card = None
    try:
        for snap in planner.plan(rollout, root, root.legal, rng=random.Random(int.from_bytes(digest, "big")),
                                 budget_s=max(1.0, deadline - time.monotonic() - 2.0),
                                 max_seeds=8 if known else 4096):
            if cancelled and cancelled():
                raise TimeoutError("timeout: calculation was cancelled after a snapshot edit")
            card = _card(snap, game_data, snapshot, known)
            if not snap["done"]:
                emit(card)
    except TimeoutError:
        if card is None:
            raise
    return card


def evaluate(snapshot, game_data, deadline, cancelled=None, publish=None):
    """Query cards plus the planner recommendation; `deadline` is an absolute monotonic time."""
    source = snapshot.get("source_sha") if isinstance(snapshot, dict) else None
    if isinstance(snapshot, dict):
        snapshot = _canonical_ids(_prepare_snapshot(snapshot), game_data)
    issue = _unsupported(snapshot, game_data) if isinstance(snapshot, dict) else (
        "incomplete-state", "The battle snapshot is invalid.", ["snapshot"])
    if issue:
        reason, detail, missing = issue
        return _result(source, "unavailable", **{kind: _unavailable(reason, detail, missing) for kind in _CARDS})
    actions = _actions(snapshot)
    if not actions:
        card = _unavailable("incomplete-state", "No legal move or switch is available.", ["player.party[0].moves"])
        return _result(source, "unavailable", **{kind: card for kind in _CARDS})
    seeds, known = _seed_pairs(snapshot, 1)
    meta = {"scenario": _scenario(snapshot), "assumed_lead": True}
    pending = {"status": "pending"}
    try:
        cards = _query(snapshot, game_data, actions[0], seeds[0], deadline)
        cards["ai_actions"] = {**cards["ai_actions"], "preview": True,
                               "conditioned_on": "one query seed at the entered battle state"}
        query = {kind: cards[kind] for kind in _CARDS[:3]}
    except _ENGINE_ERRORS as exc:
        query = dict.fromkeys(_CARDS[:3], _error_card(exc))

    def emit(card):
        if publish:
            try:
                publish(_result(source, "refining", _meta={
                    **meta, "refining": True, "phase": "Simulating battles",
                    "completed_samples": card.get("samples", 0), "target_samples": 0},
                    **{k: v if v["status"] != "unavailable" else pending for k, v in query.items()},
                    recommendation=card))
            except Exception:
                pass

    emit(pending)
    try:
        recommendation = _recommend(snapshot, game_data, actions[0], seeds[0], known, deadline, cancelled, emit)
    except _ENGINE_ERRORS as exc:
        recommendation = _error_card(exc)
    return _result(source, recommendation["status"], _meta=meta, recommendation=recommendation, **query)
