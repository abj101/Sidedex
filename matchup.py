"""Type-only matchup summaries and explainable single-slot party swaps."""


def _record_types(pokemon, game_data):
    record = game_data.get("species", {}).get(pokemon.get("species"), {})
    return record.get("types") or []


def _known_moves(pokemon, game_data):
    moves = game_data.get("moves", {})
    return [moves[move_id] | {"id": move_id} for move_id in (pokemon.get("moves") or [])
            if move_id in moves and moves[move_id].get("type")
            and isinstance(moves[move_id].get("power"), (int, float))
            and moves[move_id]["power"] > 0]


def type_effectiveness(attack_type, defender_types, type_chart):
    """Return a verified multiplier, or None when any required chart entry is absent."""
    if not attack_type or not defender_types:
        return None
    multiplier = 1.0
    for defense_type in defender_types:
        value = type_chart.get(attack_type, {}).get(defense_type)
        if value is None:
            return None
        multiplier *= value
    return multiplier


def _moves_against(attacker, defender, game_data):
    defender_types = _record_types(defender, game_data)
    result = []
    for move in _known_moves(attacker, game_data):
        effectiveness = type_effectiveness(move["type"], defender_types, game_data.get("type_chart", {}))
        if effectiveness is not None:
            result.append({"move": move["id"], "type": move["type"], "effectiveness": effectiveness})
    return result


def matchup(attacker, defender, game_data):
    """Summarize known-move type effectiveness in both directions."""
    outgoing = _moves_against(attacker, defender, game_data)
    incoming = _moves_against(defender, attacker, game_data)
    return {
        "outgoing_moves": outgoing,
        "incoming_moves": incoming,
        "outgoing_best": max((move["effectiveness"] for move in outgoing), default=None),
        "incoming_best": max((move["effectiveness"] for move in incoming), default=None),
        "label": "type coverage only",
    }


def _coverage_reasons(pokemon, enemy, game_data):
    reasons = []
    for move in _moves_against(pokemon, enemy, game_data):
        if move["effectiveness"] >= 2:
            reasons.append(f"{move['move']} is super effective against {enemy.get('species')}")
            break
    for move in _moves_against(enemy, pokemon, game_data):
        if move["effectiveness"] <= 0.5:
            verb = "is immune to" if move["effectiveness"] == 0 else "resists"
            reasons.append(f"{pokemon.get('species')} {verb} {move['move']}")
            break
    return reasons


def _covered(pokemon, enemy, game_data):
    return bool(_coverage_reasons(pokemon, enemy, game_data))


def recommend_swaps(party, live_box, enemies, game_data):
    """Rank positive single-slot swaps by newly covered enemy builds.

    Caller supplies only legal, alive, available party/Box candidates. Tied swaps
    retain the same score; no damage, KO, or switch-safety inference is made.
    """
    party = list(party)
    live_box = list(live_box)
    enemies = list(enemies)
    baseline = {index for index, enemy in enumerate(enemies)
                if any(_covered(member, enemy, game_data) for member in party)}
    suggestions = []
    for candidate in live_box:
        replacements = [(None, party)] if len(party) < 6 else [(member, party[:index] + party[index + 1:]) for index, member in enumerate(party)]
        for replaced, remaining in replacements:
            gains = []
            lost = []
            for enemy_index, enemy in enumerate(enemies):
                covered_before = enemy_index in baseline
                covered_after = any(_covered(member, enemy, game_data) for member in remaining + [candidate])
                if covered_after and not covered_before:
                    gains.append({"enemy": enemy.get("species"), "reasons": _coverage_reasons(candidate, enemy, game_data)})
                elif covered_before and not covered_after:
                    lost.append(enemy.get("species"))
            score = len(gains) - len(lost)
            if score > 0:
                suggestions.append({
                    "pokemon": candidate,
                    "replaced": replaced,
                    "score": score,
                    "reasons": gains,
                    "lost": lost,
                    "label": "type coverage only",
                })
    suggestions.sort(key=lambda item: item["score"], reverse=True)
    return {
        "baseline": {"covered": len(baseline), "total": len(enemies), "uncovered": [e.get("species") for i, e in enumerate(enemies) if i not in baseline]},
        "suggestions": suggestions,
        "label": "type coverage only",
    }
