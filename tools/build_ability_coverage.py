"""Inventory extracted abilities; this is not a ROM coverage claim."""

import csv
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def build(source_root=None):
    data = json.loads((ROOT / "data" / "game_data.json").read_text(encoding="utf-8"))
    users = Counter(ability for species in data["species"].values()
                    for ability in set(species.get("abilities") or []))
    test_root = source_root / "test" / "battle" / "ability" if source_root else None
    rows = []
    for ability in sorted(data["abilities"]):
        name = ability.removeprefix("ABILITY_").lower()
        test = test_root / f"{name}.c" if test_root else None
        rows.append({"ability_id": ability, "species_users": users[ability],
                     "source_test": f"test/battle/ability/{name}.c" if test and test.is_file() else "",
                     "singles_reachability": "unreviewed", "rom_replay": "unverified"})
    return rows


if __name__ == "__main__":
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    rows = build(source)
    output = ROOT / "data" / "ability_coverage.csv"
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} unverified ability records to {output}")
