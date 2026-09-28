"""Exact query API for the pinned engine: split QUERY_DAMAGE / QUERY_AI_ACTION.

Contract sections 1 and 7: every operation returns a versioned result, queries
never resolve a turn, and a query leaves its source decision-state token
byte-identical. The legacy four-card `parse` wrapper is preserved because
`rom_eval` publishes its cards.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Mapping, Sequence

from tools.rom_oracle import run_dynamic

DECODE_SCHEMA = "decode-v1"
QUERY_DAMAGE_SCHEMA = "sidedex-query-damage-v1"
QUERY_AI_ACTION_SCHEMA = "sidedex-query-ai-action-v1"
QUERY_FRAME_BUDGET = 24_000
QUERY_FRAME_STEP = 10
QUERY_DEADLINE_MS = 15_000

UNAVAILABLE = 1
FOCUS_SASH = 2
STURDY = 4
MULTI_HIT_RANGE = 8
MULTI_HIT_ENUMERATED = 16
ALLOWED_FLAGS = UNAVAILABLE | FOCUS_SASH | STURDY | MULTI_HIT_RANGE | MULTI_HIT_ENUMERATED
MAX_ROLL = 0xFFFF
MODE_WEIGHTS = {0: {2: 7, 3: 7, 4: 3, 5: 3}, 1: {4: 1, 5: 1}, 2: {5: 1}, 3: {3: 1}}
CRITICAL_POLICY = "item-ability-only"  # backwards-compatible default label
CRITICAL_POLICIES = ("normal", "item-ability-only")

# `ROMTRACE|query|outcomes` (README "Complete outcome weights"): the ROM's
# complete joint distribution over the target's remaining HP, in integer parts
# per million, chunked eight branches at a time.
OUTCOME_MULTI_HIT = 1
OUTCOME_SASH = 2
OUTCOME_STURDY = 4
OUTCOME_LOW_ACCURACY = 8
OUTCOME_REFUSED = 16
OUTCOME_AXIS_BOUND = 32
ALLOWED_OUTCOME_FLAGS = (OUTCOME_MULTI_HIT | OUTCOME_SASH | OUTCOME_STURDY
                         | OUTCOME_LOW_ACCURACY | OUTCOME_REFUSED | OUTCOME_AXIS_BOUND)
OUTCOME_DENOMINATOR = 1_000_000

# FALLBACK (package C, phase 2). The v8b ROM emits the `query|outcomes` record
# and `_outcome_row` consumes it. This Python composition still runs for traces
# that predate the record (the pinned v7 ELF, and any v8 trace without it), and
# it is the single place to delete once no supported engine lacks the record.
LEGACY_OUTCOME_COMPOSITION = True


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


def _compose_multi_hit_ko(hit_record, non_crit_rolls, crit_rolls, accuracy, hp, protection=False):
    """Legacy Python composition of an enumerated multi-hit KO fraction.

    `dist` is the integer-weighted per-hit damage histogram (weight `wn` for each
    non-crit roll, `wc` for each crit roll) over the engine's 16 enumerated
    damage-modifier rolls, so the per-hit weight total is `T = 16*(wn+wc)`.
    """
    mode, min_hits, max_hits, wn, wc = hit_record
    dist = {}
    for damage in non_crit_rolls:
        dist[damage] = dist.get(damage, 0) + wn
    for damage in crit_rolls:
        dist[damage] = dist.get(damage, 0) + wc
    total = 16 * (wn + wc)
    weights = {min_hits: 1} if mode == 4 else MODE_WEIGHTS[mode]
    weight_den = sum(weights.values())
    # A full-HP Focus Sash / Sturdy triggers once: the opponent survives the hit
    # that first reaches its HP, so a KO needs the cumulative damage to reach `hp`
    # strictly before the last hit (the final hit then always lands on the 1 HP
    # survivor, so its own damage roll is unweighted).
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
    return Fraction(accuracy, 100) * probability


def _multi_hit_probability(record, hit_record, rolls, hp):
    """Backwards-compatible wrapper used by the parser tests."""
    return _compose_multi_hit_ko(hit_record, rolls[(0,)], rolls[(1,)], record[5], hp,
                                 bool(record[2] & (FOCUS_SASH | STURDY)))


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
            "label": "deterministic", "distribution": False,
            "predicted_action": {"kind": kind, "id": source_id, "label": label},
            "actions": [action]}


def _require_query_complete(events) -> None:
    if not any(line == "ROMTRACE|query|complete" for line in events) or not any(
        line == "ROMTRACE|complete|0" for line in events
    ):
        raise ValueError("incomplete-state: query did not complete")


def _scan_events(events, expected_policy=None) -> dict:
    """Validate and collect every query record without building any card."""
    _require_query_complete(events)
    records = {0: {}, 1: {}}
    hits = {}
    rolls = {}
    ai = None
    query_context = None
    faints = []
    policy_verified = False
    observed_policy = None
    outcomes = {}
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
        elif cells[:3] == ["ROMTRACE", "query", "outcomes"]:
            if len(cells) < 14 or (len(cells) - 14) % 2:
                raise ValueError("engine-error: invalid query outcomes record")
            owner, move, flags, chunk, chunks, denom, branches, hits_mode, \
                min_hits, max_hits, accuracy = _ints(cells[3:14], "outcomes")
            if (owner not in records or move <= 0 or flags & ~ALLOWED_OUTCOME_FLAGS
                    or denom != OUTCOME_DENOMINATOR or chunks < 0 or chunk < 0
                    or not 0 <= accuracy <= 100 or not 0 <= min_hits <= max_hits <= 5
                    or hits_mode > 4 or chunks and chunk >= chunks):
                raise ValueError("engine-error: invalid query outcomes header")
            entry = outcomes.setdefault((owner, move), {
                "flags": flags, "denominator": denom, "hits_mode": hits_mode,
                "min_hits": min_hits, "max_hits": max_hits, "accuracy": accuracy,
                "branches": {}, "declared_branches": branches, "pairs": 0,
                "chunks": chunks, "seen": set()})
            if (entry["chunks"] != chunks or entry["flags"] != flags
                    or entry["declared_branches"] != branches):
                raise ValueError("engine-error: inconsistent query outcomes header")
            if chunk in entry["seen"]:
                raise ValueError("engine-error: duplicate query outcomes chunk")
            entry["seen"].add(chunk)
            entry["pairs"] += len(range(14, len(cells), 2))
            for index in range(14, len(cells), 2):
                hp, weight = _ints(cells[index:index + 2], "outcome branch")
                if hp < 0 or weight < 0:
                    raise ValueError("engine-error: invalid query outcome branch")
                entry["branches"][hp] = entry["branches"].get(hp, 0) + weight
        elif cells[:3] == ["ROMTRACE", "query", "context"]:
            if len(cells) != 4 or cells[3] not in (
                "current-state", "post-switch", "after-candidate-turn"
            ) or query_context is not None:
                raise ValueError("engine-error: invalid query context")
            query_context = cells[3]
        elif cells[:2] == ["ROMTRACE", "critical-policy"]:
            if (len(cells) != 4 or cells[2] not in CRITICAL_POLICIES
                    or cells[3] != run_dynamic.CRITICAL_POLICY_MECHANISMS[cells[2]]):
                raise ValueError("engine-error: unsupported critical policy")
            if observed_policy is not None and observed_policy != cells[2]:
                raise ValueError("engine-error: conflicting critical policy")
            observed_policy = cells[2]
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
    if expected_policy is not None and observed_policy != expected_policy:
        raise ValueError(
            f"engine-error: critical policy mismatch: trace reports {observed_policy!r}, "
            f"request expected {expected_policy!r}")
    for entry in outcomes.values():
        if entry["pairs"] != entry["declared_branches"]:
            raise ValueError("engine-error: query outcomes record lost a branch")
        if entry["chunks"] == 0:
            if not entry["flags"] & OUTCOME_REFUSED or entry["branches"]:
                raise ValueError("engine-error: empty query outcomes record without a refusal")
            continue
        if len(entry["seen"]) != entry["chunks"]:
            raise ValueError("engine-error: query outcomes record is missing a chunk")
        if sum(entry["branches"].values()) != entry["denominator"]:
            raise ValueError("engine-error: query outcomes weights do not sum to the denominator")
    return {"records": records, "hits": hits, "rolls": rolls, "ai": ai,
            "context": query_context, "faints": faints,
            "outcomes": outcomes,
            "policy": observed_policy or CRITICAL_POLICY,
            "policy_verified": policy_verified, "active": query_active}


def _outcome_row(entry, minimum, maximum, crit_den, accuracy, target_hp):
    """Build an engine-exact row from the ROM's complete outcome enumeration."""
    if entry["flags"] & OUTCOME_REFUSED:
        return {"status": "unavailable", "reason": "unsupported-mechanic",
                "detail": "The ROM declined to enumerate this move's outcome weights."}
    ko = sum(weight for hp, weight in entry["branches"].items() if hp <= 0)
    probability = Fraction(ko, entry["denominator"])
    multi = bool(entry["flags"] & OUTCOME_MULTI_HIT)
    notes = []
    if entry["flags"] & OUTCOME_SASH:
        notes.append("Full-HP Focus Sash is folded into the ROM outcome weights.")
    if entry["flags"] & OUTCOME_STURDY:
        notes.append("Full-HP Sturdy is folded into the ROM outcome weights.")
    row = {"status": "engine-exact",
           "probability": {"num": probability.numerator, "den": probability.denominator},
           "num": probability.numerator, "den": probability.denominator,
           "damage_range": {"min": minimum, "max": maximum},
           "accuracy_percent": accuracy, "crit_denominator": crit_den,
           "method": ("engine-enumerated-multi-hit" if multi else
                      "engine-enumerated-single-hit"),
           "outcomes_source": "rom", "notes": notes}
    if target_hp is not None:
        row["distribution"] = [
            {"remaining_hp": hp, "damage": target_hp - hp, "weight": weight}
            for hp, weight in sorted(entry["branches"].items())]
        row["distribution_denominator"] = entry["denominator"]
    return row


def _damage_row(scanned, side, move_id, target_mon, game_data):
    numeric = run_dynamic.numeric_id("moves", move_id)
    title = (game_data.get("moves", {}).get(move_id) or {}).get("name", move_id)
    move_data = game_data.get("moves", {}).get(move_id) or {}
    record = scanned["records"][side].get(numeric)
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
        return row
    outcome = scanned["outcomes"].get((side, numeric))
    if outcome is not None:
        result = _outcome_row(outcome, minimum, maximum, crit_den, accuracy, target_hp)
        if result["status"] == "unavailable" and flags & MULTI_HIT_RANGE:
            result["damage_range"] = {"min": minimum, "max": maximum}
        row.update(result)
        return row
    if flags & MULTI_HIT_ENUMERATED:
        if not LEGACY_OUTCOME_COMPOSITION:
            raise ValueError("unsupported-mechanic: ROM outcome-weight enumeration is not wired up")
        # LEGACY-FALLBACK (see the module-level constant): single deletion point.
        hit_record = scanned["hits"][(side, numeric)]
        probability = _compose_multi_hit_ko(
            hit_record,
            scanned["rolls"][(side, numeric, 0)],
            scanned["rolls"][(side, numeric, 1)],
            accuracy, target_mon["hp"], bool(flags & (FOCUS_SASH | STURDY)))
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
        return row
    # Single-hit: the ROM already returned the complete fraction including crit
    # weights and accuracy, so Python only aggregates the integers.
    notes = []
    if flags & FOCUS_SASH:
        notes.append("Full-HP Focus Sash prevents a one-hit KO.")
    if flags & STURDY:
        notes.append("Full-HP Sturdy prevents a one-hit KO.")
    row.update(status="engine-exact", probability={"num": num, "den": den},
               num=num, den=den, damage_range={"min": minimum, "max": maximum},
               accuracy_percent=accuracy, crit_denominator=crit_den,
               method="engine-enumerated-single-hit", notes=notes)
    return row


def _damage_cards(scanned, snapshot, game_data, move_ids=None):
    cards = {}
    for side, key, name in ((0, "player_moves", "player"), (1, "enemy_moves", "enemy")):
        rows = []
        active = scanned["active"].get(side)
        if active is not None:
            id_to_move = {value: key for key, value in run_dynamic.ID_MAP["ids"]["moves"].items()}
            move_ids_side = [id_to_move.get(value, "MOVE_NONE") for value in active["moves"]]
            target_mon = scanned["active"].get(1 - side)
            target_mon = target_mon if target_mon is not None else (
                snapshot["enemy"] if side == 0 else snapshot["player"])["party"][0]
        else:
            move_ids_side = snapshot[name]["party"][0].get("moves") or []
            target_mon = (snapshot["enemy"] if side == 0 else snapshot["player"])["party"][0]
        for move_id in move_ids_side:
            if move_id == "MOVE_NONE":
                continue
            if move_ids is not None and move_id not in move_ids and \
                    run_dynamic.numeric_id("moves", move_id) not in move_ids:
                continue
            rows.append(_damage_row(scanned, side, move_id, target_mon, game_data))
        cards[key] = {"status": "engine-exact" if any(row.get("status") == "engine-exact"
                                                    or row.get("damage_range") for row in rows)
                      else "unavailable", "moves": rows,
                      "conditioned_on": "current active target; one selected single-hit move",
                      "critical_policy": scanned["policy"],
                      "critical_policy_verified": scanned["policy_verified"],
                      "query_context": scanned["context"] or "current-state"}
    return cards


def _unsupported_rows(cards):
    return [{"side": "player" if key == "player_moves" else "enemy",
             "move_id": row["move_id"], "reason": row.get("reason", "unsupported-mechanic"),
             "detail": row.get("detail")}
            for key in ("player_moves", "enemy_moves")
            for row in cards[key]["moves"] if row.get("status") == "unavailable"]


def _envelope(schema, scanned, extra=None):
    result = {"query_schema": schema, "decode_schema": DECODE_SCHEMA,
              "critical_policy": scanned["policy"],
              "critical_policy_verified": scanned["policy_verified"],
              "query_context": scanned["context"] or "current-state"}
    if extra:
        result.update(extra)
    return result


def parse_damage(events, snapshot, game_data, *, token=None, move_ids=None,
                 expected_policy=None) -> dict:
    """QUERY_DAMAGE result: versioned damage distribution/range with fail-closed reasons."""
    scanned = _scan_events(events, expected_policy=expected_policy)
    cards = _damage_cards(scanned, snapshot, game_data, move_ids=move_ids)
    result = _envelope(QUERY_DAMAGE_SCHEMA, scanned, {
        "player_moves": cards["player_moves"], "enemy_moves": cards["enemy_moves"],
        "unsupported": _unsupported_rows(cards)})
    return _with_token(result, token)


def parse_ai_action(events, game_data, *, token=None, expected_policy=None) -> dict:
    """QUERY_AI_ACTION result: one deterministic fixed-seed AI action, no turn resolved."""
    scanned = _scan_events(events, expected_policy=expected_policy)
    card = _ai_card(scanned["ai"], game_data)
    result = _envelope(QUERY_AI_ACTION_SCHEMA, scanned, {"ai_actions": card})
    return _with_token(result, token)


def _with_token(result, token):
    if token is None:
        return result
    result.update({"token_sha256": token.sha256, "identity": dict(token.identity),
                   "boundary": token.boundary, "turn": token.turn, "rng": dict(token.rng)})
    return result


def parse(events, snapshot, game_data):
    """Legacy four-card projection used by `rom_eval`; kept frozen."""
    scanned = _scan_events(events)
    cards = _damage_cards(scanned, snapshot, game_data)
    cards["ai_actions"] = _ai_card(scanned["ai"], game_data)
    cards["critical_policy"] = scanned["policy"]
    cards["critical_policy_verified"] = scanned["policy_verified"]
    cards["query_context"] = scanned["context"] or "current-state"
    cards["faints"] = scanned["faints"]
    cards["active"] = scanned["active"]
    return cards


@dataclass(frozen=True)
class DecisionStateToken:
    """Contract section 2 decision-state token as consumed by the query API."""

    handle: str
    sha256: str
    bytes: int
    identity: Mapping[str, Any]
    rng: Mapping[str, Any]
    boundary: str = "player-choice"
    turn: int = 0
    decoded: Mapping[str, Any] = field(default_factory=dict)
    legal_player_actions: Sequence[Mapping[str, Any]] = ()
    terminal: Any = None
    request: Mapping[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        payload = {"handle": self.handle, "sha256": self.sha256,
                   "identity": dict(self.identity), "rng": dict(self.rng),
                   "boundary": self.boundary, "turn": self.turn,
                   "decoded": self.decoded}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def token_from_checkpoint(slot, request, *, identity=None, boundary="player-choice", turn=0):
    """Build a token from a worker state slot plus the root mailbox envelope."""
    if not isinstance(slot, dict) or "handle" not in slot:
        raise ValueError("invalid-id: a checkpoint slot is required")
    identity = dict(slot.get("identity") or {}) if identity is None else dict(identity)
    return DecisionStateToken(
        handle=slot["handle"], sha256=slot["sha256"], bytes=slot["bytes"],
        identity=identity, rng={"main": list(request["rng_main"]),
                                "secondary": list(request["rng_secondary"])},
        boundary=boundary, turn=turn, decoded=snapshot_from_request(request),
        request=dict(request))


def snapshot_from_request(request):
    """Projection the query parser uses for a target when the ROM sends no active record."""
    return {"player": {"party": request["player_party"]},
            "enemy": {"party": request["opponent_party"]}}


def _query_request(token, operation_name, protocol_version):
    if token.request is None:
        raise ValueError("incomplete-state: token carries no mailbox envelope")
    request = dict(token.request)
    request["operation"] = operation_name
    request["query"] = False
    request.pop("post_switch_query", None)
    request["rng_main"] = list(token.rng["main"])
    request["rng_secondary"] = list(token.rng["secondary"])
    request["critical_policy"] = run_dynamic.critical_policy_label(request)
    payload = run_dynamic.encode_request(request, protocol_version=protocol_version)
    return request, payload


def _assert_identity(client, token, protocol_version):
    identity = token.identity or {}
    expected_elf = identity.get("test_elf_sha256")
    if expected_elf and str(expected_elf).upper() != client.rom_sha256:
        raise ValueError("identity-mismatch: token test ELF differs from the worker ELF")
    expected_worker = identity.get("worker_sha256")
    if expected_worker and str(expected_worker).upper() != client.worker_sha256:
        raise ValueError("identity-mismatch: token worker differs from the connected worker")
    expected_protocol = identity.get("rom_hook_protocol")
    if expected_protocol is not None and int(expected_protocol) != int(protocol_version):
        raise ValueError("identity-mismatch: token protocol differs from the encoder")


def _slot_sha(client, handle):
    for slot in client.list_states().get("states", []):
        if slot.get("handle") == handle:
            return slot.get("sha256")
    raise ValueError("stale-handle: token state slot is gone")


def _run_query(client, mailbox, token, payload, protocol_version, deadline_ms):
    """Restore the token, run the query, and fail closed if the token changed."""
    _assert_identity(client, token, protocol_version)
    if _slot_sha(client, token.handle) != token.sha256:
        raise ValueError("identity-mismatch: worker state slot does not match the token")
    fingerprint = token.fingerprint()
    client.restore_state(token.handle)
    client.write_memory(mailbox, payload)
    started = time.monotonic()
    events = []
    frames = 0
    while frames < QUERY_FRAME_BUDGET:
        if (time.monotonic() - started) * 1000 > deadline_ms:
            raise ValueError("deadline-exceeded: query did not complete")
        events.extend(run_dynamic._trace(client.run_frames(QUERY_FRAME_STEP)))
        frames += QUERY_FRAME_STEP
        if any(event == "ROMTRACE|query|complete" for event in events):
            break
    if not any(event == "ROMTRACE|query|complete" for event in events):
        raise ValueError("incomplete-state: query did not complete")
    if _slot_sha(client, token.handle) != token.sha256 or token.fingerprint() != fingerprint:
        raise ValueError("identity-mismatch: query mutated the source token")
    return events, (time.monotonic() - started) * 1000


def query_damage(client, mailbox, token, *, game_data, protocol_version=None,
                 move_ids=None, deadline_ms=QUERY_DEADLINE_MS) -> dict:
    """Run QUERY_DAMAGE from `token` without resolving a turn."""
    version = run_dynamic._protocol_version(protocol_version)
    request, payload = _query_request(token, "query-damage", version)
    events, latency_ms = _run_query(client, mailbox, token, payload, version, deadline_ms)
    result = parse_damage(events, token.decoded or snapshot_from_request(request), game_data,
                          token=token, move_ids=move_ids,
                          expected_policy=request["critical_policy"])
    result["latency_ms"] = latency_ms
    return result


def query_ai_action(client, mailbox, token, *, game_data, protocol_version=None,
                    deadline_ms=QUERY_DEADLINE_MS) -> dict:
    """Run QUERY_AI_ACTION once from `token` without resolving a turn."""
    version = run_dynamic._protocol_version(protocol_version)
    request, payload = _query_request(token, "query-ai-action", version)
    events, latency_ms = _run_query(client, mailbox, token, payload, version, deadline_ms)
    result = parse_ai_action(events, game_data, token=token,
                             expected_policy=request["critical_policy"])
    result["latency_ms"] = latency_ms
    return result


def latency_percentiles(values) -> dict:
    """Nearest-rank p50/p95 for query latency, reported separately from transitions."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None}
    def pick(fraction):
        return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]
    return {"count": len(ordered), "p50": pick(0.50), "p95": pick(0.95),
            "min": ordered[0], "max": ordered[-1]}
