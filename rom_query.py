"""Parse the pinned engine's damage query and trainer-action prediction."""

from fractions import Fraction

from tools.rom_oracle import run_dynamic

UNAVAILABLE = 1
FOCUS_SASH = 2
STURDY = 4
MULTI_HIT_RANGE = 8
MULTI_HIT_ENUMERATED = 16
ALLOWED_FLAGS = UNAVAILABLE | FOCUS_SASH | STURDY | MULTI_HIT_RANGE | MULTI_HIT_ENUMERATED
MAX_ROLL = 0xFFFF
MODE_WEIGHTS = {0: {2: 7, 3: 7, 4: 3, 5: 3}, 1: {4: 1, 5: 1}, 2: {5: 1}, 3: {3: 1}}
CRITICAL_POLICY = "item-ability-only"


def _ints(cells, label):
    try:
        return [int(cell) for cell in cells]
    except ValueError as exc:
        raise ValueError(f"engine-error: invalid query {label} number") from exc


def _human(source_id):
    name = source_id
    for prefix in ("ITEM_", "MOVE_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.replace("_", " ").title()


def item_label(game_data, source_id):
    """Item name from the data table, falling back to a title-cased source ID."""
    return ((game_data.get("items") or {}).get(source_id) or {}).get("name") or _human(source_id)


def _multi_hit_probability(record, hit_record, rolls, hp):
    """Exact seed-independent KO fraction for an enumerated multi-hit move.

    `dist` is the integer-weighted per-hit damage histogram (weight `wn` for each
    non-crit roll, `wc` for each crit roll) over the engine's 16 enumerated
    damage-modifier rolls, so the per-hit weight total is `T = 16*(wn+wc)`.
    """
    mode, min_hits, max_hits, wn, wc = hit_record
    dist = {}
    for damage in rolls[(0,)]:
        dist[damage] = dist.get(damage, 0) + wn
    for damage in rolls[(1,)]:
        dist[damage] = dist.get(damage, 0) + wc
    total = 16 * (wn + wc)
    weights = {min_hits: 1} if mode == 4 else MODE_WEIGHTS[mode]
    weight_den = sum(weights.values())
    # A full-HP Focus Sash / Sturdy triggers once: the opponent survives the hit
    # that first reaches its HP, so a KO needs the cumulative damage to reach `hp`
    # strictly before the last hit (the final hit then always lands on the 1 HP
    # survivor, so its own damage roll is unweighted).
    protection = bool(record[2] & (FOCUS_SASH | STURDY))
    probability = Fraction(0)
    for count, weight in weights.items():
        if protection and count < 2:
            continue
        steps = count - 1 if protection else count
        cumulative = {0: 1}
        for _ in range(steps):
            nxt = {}
            for value, ways in cumulative.items():
                for damage, damage_ways in dist.items():
                    nxt[value + damage] = nxt.get(value + damage, 0) + ways * damage_ways
            cumulative = nxt
        reaching = sum(ways for value, ways in cumulative.items() if value >= hp)
        probability += Fraction(weight, weight_den) * Fraction(reaching, total ** steps)
    return Fraction(record[5], 100) * probability


def _ai_card(ai, game_data):
    if ai is None:
        return {"status": "unavailable", "reason": "incomplete-state",
                "detail": "Query did not return a trainer action."}
    kind, numeric = ai
    if kind == "move":
        source_id = {value: key for key, value in run_dynamic.ID_MAP["ids"]["moves"].items()}.get(
            numeric, f"ID_{numeric}")
        label = (game_data.get("moves", {}).get(source_id) or {}).get("name", source_id)
    elif kind == "item":
        source_id = {value: key for key, value in run_dynamic.ID_MAP["ids"]["items"].items()}.get(
            numeric, f"ID_{numeric}")
        label = item_label(game_data, source_id)
    else:
        source_id = f"SLOT_{numeric + 1}"
        label = f"Switch to party slot {numeric + 1}"
    action = {"kind": kind, "id": source_id, "label": label,
              "probability": {"num": 1, "den": 1}, "top": True}
    return {"status": "engine-fixed-seed", "conditioned_on": "entered battle state",
            "predicted_action": {"kind": kind, "id": source_id, "label": label},
            "actions": [action]}


def parse(events, snapshot, game_data):
    """Return engine-enumerated KO cards and one fixed-seed AI choice."""
    if not any(line == "ROMTRACE|query|complete" for line in events) or not any(
        line == "ROMTRACE|complete|0" for line in events
    ):
        raise ValueError("incomplete-state: query did not complete")
    records = {0: {}, 1: {}}
    hits = {}
    rolls = {}
    ai = None
    query_context = None
    faints = []
    policy_verified = False
    query_active = {}
    for line in events:
        cells = line.split("|")
        if cells[:3] == ["ROMTRACE", "query", "damage"]:
            if len(cells) != 12:
                raise ValueError("engine-error: invalid query damage record")
            side, move, num, den, flags, minimum, maximum, accuracy, crit_den = _ints(cells[3:], "damage")
            if (side not in records or move <= 0 or flags & ~ALLOWED_FLAGS
                    or not 0 <= minimum <= maximum or not 0 <= accuracy <= 100
                    or crit_den < 0 or not 0 <= num <= den):
                raise ValueError("engine-error: invalid query damage bounds")
            if not flags & (UNAVAILABLE | MULTI_HIT_ENUMERATED) and (den != 1600 * crit_den or crit_den == 0):
                raise ValueError("engine-error: invalid exact query denominator")
            if move in records[side]:
                raise ValueError("engine-error: duplicate query move")
            records[side][move] = (num, den, flags, minimum, maximum, accuracy, crit_den)
        elif cells[:3] == ["ROMTRACE", "query", "hits"]:
            if len(cells) != 10:
                raise ValueError("engine-error: invalid query hits record")
            side, move, mode, min_hits, max_hits, wn, wc = _ints(cells[3:], "hits")
            if (side not in records or move <= 0 or not 0 <= mode <= 4
                    or not 1 <= min_hits <= max_hits or wn < 0 or wc < 0 or wn + wc < 1):
                raise ValueError("engine-error: invalid query hit count bounds")
            if (side, move) in hits:
                raise ValueError("engine-error: duplicate query hit count")
            hits[(side, move)] = (mode, min_hits, max_hits, wn, wc)
        elif cells[:3] == ["ROMTRACE", "query", "hitroll"]:
            # 22 cells = owner, move, crit and the engine's 16 damage rolls.
            if len(cells) != 22:
                raise ValueError("engine-error: invalid query hit roll record")
            side, move, crit = _ints(cells[3:6], "hit roll")
            damage = _ints(cells[6:], "hit roll")
            if (side not in records or move <= 0 or crit not in (0, 1)
                    or any(not 0 <= value <= MAX_ROLL for value in damage)):
                raise ValueError("engine-error: invalid query hit roll bounds")
            if (side, move, crit) in rolls:
                raise ValueError("engine-error: duplicate query hit roll")
            rolls[(side, move, crit)] = damage
        elif cells[:3] == ["ROMTRACE", "query", "ai"]:
            if len(cells) != 5 or cells[3] not in ("move", "switch", "item") or ai is not None:
                raise ValueError("engine-error: invalid query AI record")
            try:
                ai = (cells[3], int(cells[4]))
            except ValueError as exc:
                raise ValueError("engine-error: invalid query AI choice") from exc
        elif cells[:3] == ["ROMTRACE", "query", "context"]:
            if len(cells) != 4 or cells[3] not in (
                "current-state", "post-switch", "after-candidate-turn"
            ) or query_context is not None:
                raise ValueError("engine-error: invalid query context")
            query_context = cells[3]
        elif cells[:2] == ["ROMTRACE", "critical-policy"]:
            if len(cells) != 4 or cells[2] != CRITICAL_POLICY or cells[3] != "battle-script-filtered":
                raise ValueError("engine-error: unsupported critical policy")
            policy_verified = True
        elif cells[:3] == ["ROMTRACE", "query", "active"]:
            if len(cells) != 15:
                raise ValueError("engine-error: invalid active query record")
            side, slot, species, hp, max_hp, *stages = _ints(cells[3:], "active")
            if (side not in records or slot < 0 or species <= 0 or not 0 < hp <= max_hp
                    or any(stage > 12 for stage in stages)):
                raise ValueError("engine-error: invalid active query record")
            query_active[side] = {"party_slot": slot, "species_id": species,
                                  "hp": hp, "max_hp": max_hp, "stat_stages": stages,
                                  "moves": []}
        elif cells[:3] == ["ROMTRACE", "query", "active-move"]:
            if len(cells) != 6:
                raise ValueError("engine-error: invalid active move query record")
            side, slot, move = _ints(cells[3:], "active move")
            if side not in records or side not in query_active or not 0 <= slot < 4:
                raise ValueError("engine-error: invalid active move query record")
            query_active[side]["moves"].append(move)
        elif cells[:2] == ["ROMTRACE", "faint"]:
            if len(cells) != 6:
                raise ValueError("engine-error: invalid faint record")
            turn, battler, party_slot, species = _ints(cells[2:], "faint")
            if turn < 0 or battler < 0 or party_slot < 0 or species <= 0:
                raise ValueError("engine-error: invalid faint record")
            faints.append({"turn": turn, "battler": battler,
                           "party_slot": party_slot, "species_id": species})
    for side in records:
        for move, record in records[side].items():
            if record[2] & MULTI_HIT_ENUMERATED and not (
                (side, move) in hits and (side, move, 0) in rolls and (side, move, 1) in rolls
            ):
                raise ValueError("engine-error: enumerated multi-hit move lacks hit rolls")

    cards = {}
    for side, key, name in ((0, "player_moves", "player"), (1, "enemy_moves", "enemy")):
        rows = []
        active = query_active.get(side)
        if active is not None:
            id_to_move = {value: key for key, value in run_dynamic.ID_MAP["ids"]["moves"].items()}
            move_ids = [id_to_move.get(value, "MOVE_NONE") for value in active["moves"]]
            target_mon = query_active.get(1 - side)
            target_mon = target_mon if target_mon is not None else (
                snapshot["enemy"] if side == 0 else snapshot["player"])["party"][0]
        else:
            move_ids = snapshot[name]["party"][0].get("moves") or []
            target_mon = (snapshot["enemy"] if side == 0 else snapshot["player"])["party"][0]
        for move_id in move_ids:
            if move_id == "MOVE_NONE":
                continue
            numeric = run_dynamic.numeric_id("moves", move_id)
            title = (game_data.get("moves", {}).get(move_id) or {}).get("name", move_id)
            move_data = game_data.get("moves", {}).get(move_id) or {}
            record = records[side].get(numeric)
            if record is None:
                raise ValueError(f"incomplete-state: missing query for {move_id}")
            num, den, flags, minimum, maximum, accuracy, crit_den = record
            target_hp = target_mon.get("hp") if isinstance(target_mon, dict) else None
            target_max_hp = target_mon.get("max_hp") if isinstance(target_mon, dict) else None
            row = {"move_id": move_id, "move_name": title,
                   "power": move_data.get("power"), "category": move_data.get("category"),
                   "target_hp": target_hp, "target_max_hp": target_max_hp}
            if flags & UNAVAILABLE:
                row.update(status="unavailable", reason="unsupported-mechanic",
                           detail=("KO chance not calculated; range assumes unchanged damage modifiers between hits."
                                   if flags & MULTI_HIT_RANGE else
                                   "Exact single-hit KO probability is unavailable for this move."))
                if flags & MULTI_HIT_RANGE:
                    row["damage_range"] = {"min": minimum, "max": maximum}
            elif flags & MULTI_HIT_ENUMERATED:
                hit_record = hits[(side, numeric)]
                hp = target_mon["hp"]
                probability = _multi_hit_probability(
                    record, hit_record, {(0,): rolls[(side, numeric, 0)],
                                         (1,): rolls[(side, numeric, 1)]}, hp)
                notes = []
                if flags & FOCUS_SASH:
                    notes.append("Full-HP Focus Sash prevents a one-hit KO.")
                if flags & STURDY:
                    notes.append("Full-HP Sturdy prevents a one-hit KO.")
                row.update(status="engine-exact",
                           probability={"num": probability.numerator, "den": probability.denominator},
                           num=probability.numerator, den=probability.denominator,
                           damage_range={"min": minimum, "max": maximum},
                           accuracy_percent=accuracy, crit_denominator=hit_record[3] + hit_record[4],
                           method="engine-enumerated-multi-hit", notes=notes)
            else:
                notes = []
                if flags & FOCUS_SASH:
                    notes.append("Full-HP Focus Sash prevents a one-hit KO.")
                if flags & STURDY:
                    notes.append("Full-HP Sturdy prevents a one-hit KO.")
                row.update(status="engine-exact", probability={"num": num, "den": den},
                           num=num, den=den, damage_range={"min": minimum, "max": maximum},
                           accuracy_percent=accuracy, crit_denominator=crit_den,
                           method="engine-enumerated-single-hit", notes=notes)
            rows.append(row)
        cards[key] = {"status": "engine-exact" if any(row.get("status") == "engine-exact"
                                                    or row.get("damage_range") for row in rows)
                      else "unavailable", "moves": rows,
                      "conditioned_on": "current active target; one selected single-hit move",
                      "critical_policy": CRITICAL_POLICY,
                      "critical_policy_verified": policy_verified,
                      "query_context": query_context or "current-state"}
    cards["ai_actions"] = _ai_card(ai, game_data)
    cards["critical_policy"] = CRITICAL_POLICY
    cards["critical_policy_verified"] = policy_verified
    cards["query_context"] = query_context or "current-state"
    cards["faints"] = faints
    cards["active"] = query_active
    return cards
