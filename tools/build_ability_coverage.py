"""Inventory abilities and narrowly classify source-proven trainer-singles cases.

Reachability classifications are source audits only; none imply ROM replay
verification. The audit rules intentionally cover only effects with an explicit
partner-only or trainer-capture gate in the pinned source.
"""

import csv
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("ability_id", "species_users", "source_test", "singles_reachability",
          "reachability_reason", "source_ref", "rom_replay")

# Every rule requires all anchors. If source text moves or changes, the script
# fails closed and leaves the ability unreviewed until this audit is revisited.
RULES = {
    "ABILITY_BALL_FETCH": {
        "status": "unreachable-trainer-singles",
        "file": "src/battle_util.c",
        "ref_anchor": "case ABILITY_BALL_FETCH:",
        "anchors": ["gBattleResults.catchAttempts[gLastUsedBall - ITEM_ULTRA_BALL] >= 1"],
        "extra": [("src/battle_script_commands.c",
                   "if (gBattleTypeFlags & BATTLE_TYPE_TRAINER)")],
        "reason": "Ball Fetch requires a prior catch attempt; the battle script blocks Poké Balls in trainer battles.",
    },
    "ABILITY_BATTERY": {
        "status": "singles-inert", "file": "src/battle_util.c",
        "ref_anchor": "case ABILITY_BATTERY:",
        "anchors": ["if (IsBattlerAlive(BATTLE_PARTNER(battlerAtk)))",
                    "if (IS_MOVE_SPECIAL(move))"],
        "reason": "Its only battle modifier boosts an alive partner's special move; trainer singles have no active partner.",
    },
    "ABILITY_POWER_SPOT": {
        "status": "singles-inert", "file": "src/battle_util.c",
        "ref_anchor": "case ABILITY_POWER_SPOT:",
        "anchors": ["if (IsBattlerAlive(BATTLE_PARTNER(battlerAtk)))"],
        "reason": "Its only battle modifier boosts an alive partner's move; trainer singles have no active partner.",
    },
    "ABILITY_FRIEND_GUARD": {
        "status": "singles-inert", "file": "src/battle_util.c",
        "ref_anchor": "case ABILITY_FRIEND_GUARD:",
        "anchors": ["static inline uq4_12_t GetDefenderPartnerAbilitiesModifier"],
        "extra": [("src/battle_util.c", "if (IsBattlerAlive(BATTLE_PARTNER(battlerDef)))")],
        "reason": "It only reduces damage to an alive partner; trainer singles have no active partner.",
    },
    "ABILITY_HEALER": {
        "status": "singles-inert", "file": "src/battle_util.c",
        "ref_anchor": "case ABILITY_HEALER:",
        "anchors": ["gBattleScripting.battler = BATTLE_PARTNER(battler);",
                    "IsBattlerAlive(gBattleScripting.battler)"],
        "reason": "It only attempts to heal an alive partner at end of turn; trainer singles have no active partner.",
    },
    "ABILITY_SYMBIOSIS": {
        "status": "singles-inert", "file": "src/battle_script_commands.c",
        "ref_anchor": "GetBattlerAbility(ally) == ABILITY_SYMBIOSIS",
        "anchors": ["&& IsBattlerAlive(ally)"],
        "reason": "Item transfer is checked only from an alive allied battler; trainer singles have no active partner.",
    },
    "ABILITY_COSTAR": {
        "status": "singles-inert", "file": "src/battle_util.c",
        "ref_anchor": "case ABILITY_COSTAR:",
        "anchors": ["&& IsDoubleBattle()", "CountBattlerStatIncreases(BATTLE_PARTNER(battler), FALSE)"],
        "reason": "Its switch-in effect requires a double battle and copies stat stages from the partner.",
    },
    "ABILITY_HOSPITALITY": {
        "status": "singles-inert", "file": "src/battle_util.c",
        "ref_anchor": "case ABILITY_HOSPITALITY:",
        "anchors": ["&& IsDoubleBattle()", "partner = BATTLE_PARTNER(battler);"],
        "reason": "Its switch-in effect requires a double battle and heals the partner.",
    },
    "ABILITY_COMMANDER": {
        "status": "singles-inert", "file": "src/battle_util.c",
        "ref_anchor": "case ABILITY_COMMANDER:",
        "anchors": ["partner = BATTLE_PARTNER(battler);",
                    "gBattleMons[partner].species == SPECIES_DONDOZO",
                    "== SPECIES_TATSUGIRI"],
        "reason": "Commander only activates with Dondozo in the partner battler slot; trainer singles have no partner battler.",
    },
    "ABILITY_RECEIVER": {
        "status": "singles-inert", "file": "src/battle_script_commands.c",
        "ref_anchor": "case VARIOUS_TRY_ACTIVATE_RECEIVER:",
        "anchors": ["gBattlerAbility = BATTLE_PARTNER(battler);",
                    "i == ABILITY_RECEIVER || i == ABILITY_POWER_OF_ALCHEMY",
                    "IsBattlerAlive(gBattlerAbility)"],
        "reason": "Receiver only copies a fainted ally's ability from the partner battler; trainer singles have no active partner.",
    },
    "ABILITY_POWER_OF_ALCHEMY": {
        "status": "singles-inert", "file": "src/battle_script_commands.c",
        "ref_anchor": "case VARIOUS_TRY_ACTIVATE_RECEIVER:",
        "anchors": ["gBattlerAbility = BATTLE_PARTNER(battler);",
                    "i == ABILITY_RECEIVER || i == ABILITY_POWER_OF_ALCHEMY",
                    "IsBattlerAlive(gBattlerAbility)"],
        "reason": "Power of Alchemy only copies a fainted ally's ability from the partner battler; trainer singles have no active partner.",
    },
}


def source_commit(source_root):
    try:
        return subprocess.run(["git", "-C", str(source_root), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _find_line(path, anchor):
    try:
        for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if anchor in line:
                return line_no
    except OSError:
        pass
    return None


def classify(ability, source_root):
    """Return a source-backed classification or None if any audit anchor is absent."""
    rule = RULES.get(ability)
    if not rule:
        return None
    source_file = source_root / rule["file"]
    ref_line = _find_line(source_file, rule["ref_anchor"])
    if ref_line is None or any(_find_line(source_file, anchor) is None
                               for anchor in rule["anchors"]):
        return None
    refs = [f"{rule['file']}:{ref_line}"]
    if rule["status"] == "singles-inert":
        setup_file = "src/battle_main.c"
        setup_anchor = "IsTrainerDoubleBattle(gTrainerBattleOpponent_A) ? BATTLE_TYPE_DOUBLE : 0"
        setup_line = _find_line(source_root / setup_file, setup_anchor)
        if setup_line is None:
            return None
        refs.append(f"{setup_file}:{setup_line}")
    for extra_file, anchor in rule.get("extra", []):
        extra_line = _find_line(source_root / extra_file, anchor)
        if extra_line is None:
            return None
        refs.append(f"{extra_file}:{extra_line}")
    return {"singles_reachability": rule["status"],
            "reachability_reason": rule["reason"],
            "source_ref": "; ".join(refs)}


def build(source_root=None):
    data = json.loads((ROOT / "data" / "game_data.json").read_text(encoding="utf-8"))
    source_lock = json.loads((ROOT / "data" / "source_lock.json").read_text(encoding="utf-8"))
    users = Counter(ability for species in data["species"].values()
                    for ability in set(species.get("abilities") or []))
    source_root = Path(source_root).resolve() if source_root else None
    source_matches_pin = bool(source_root and source_commit(source_root) == source_lock["commit"])
    test_root = source_root / "test" / "battle" / "ability" if source_root else None
    rows = []
    for ability in sorted(data["abilities"]):
        name = ability.removeprefix("ABILITY_").lower()
        test = test_root / f"{name}.c" if test_root else None
        audit = classify(ability, source_root) if source_matches_pin else None
        rows.append({"ability_id": ability, "species_users": users[ability],
                     "source_test": f"test/battle/ability/{name}.c" if test and test.is_file() else "",
                     "singles_reachability": audit["singles_reachability"] if audit else "unreviewed",
                     "reachability_reason": audit["reachability_reason"] if audit else "",
                     "source_ref": audit["source_ref"] if audit else "",
                     "rom_replay": "unverified"})
    return rows


if __name__ == "__main__":
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    rows = build(source)
    output = ROOT / "data" / "ability_coverage.csv"
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    classified = sum(row["singles_reachability"] != "unreviewed" for row in rows)
    print(f"Wrote {len(rows)} ability records; {classified} source-audited, all ROM replay unverified.")
