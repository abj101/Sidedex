"""Order trainers where the pinned trainer IDs establish progression."""

from __future__ import annotations

import json
import re
from pathlib import Path


_GYMS = (
    ("Rustboro", ("ROXANNE",)),
    ("Dewford", ("BRAWLY",)),
    ("Mauville", ("WATTSON",)),
    ("Lavaridge", ("FLANNERY",)),
    ("Petalburg", ("NORMAN",)),
    ("Fortree", ("WINONA",)),
    ("Mossdeep", ("TATE", "LIZA")),
    ("Sootopolis", ("JUAN",)),
)
_ELITE_FOUR = ("SIDNEY", "PHOEBE", "GLACIA", "DRAKE")
_SOURCE_LOCATIONS = json.loads(
    (Path(__file__).parent / "data/trainer_locations.json").read_text(encoding="utf-8")
)["trainers"]
_LOCATION_LABELS = sorted({
    item["label"]
    for location in _SOURCE_LOCATIONS.values()
    for item in location["maps"]
})
_LOCATION_RANK = {label: index for index, label in enumerate(_LOCATION_LABELS)}
_GYM_MAPS = (
    ("RustboroCity_Gym", 1, "Rustboro"),
    ("DewfordTown_Gym", 2, "Dewford"),
    ("MauvilleCity_Gym", 3, "Mauville"),
    ("LavaridgeTown_Gym", 4, "Lavaridge"),
    ("PetalburgCity_Gym", 5, "Petalburg"),
    ("FortreeCity_Gym", 6, "Fortree"),
    ("MossdeepCity_Gym", 7, "Mossdeep"),
    ("SootopolisCity_Gym", 8, "Sootopolis"),
)
_CHAPTERS = {
    0: "Before Gym 1",
    2: "After Gym 1 · Before Gym 2",
    4: "After Gym 2 · Before Gym 3",
    6: "After Gym 3 · Before Gym 4",
    8: "After Gym 4 · Before Gym 5",
    10: "After Gym 5 · Before Gym 6",
    12: "After Gym 6 · Before Gym 7",
    14: "After Gym 7 · Before Gym 8",
    16: "After Gym 8 · Before the Elite Four",
}


def _progression(trainer_id: str) -> tuple[int, str, int] | None:
    # Gym leaders and rematches have stable, explicit trainer-constant names.
    for gym_number, (town, names) in enumerate(_GYMS, 1):
        for name in names:
            if trainer_id != f"TRAINER_{name}" and not trainer_id.startswith(f"TRAINER_{name}_"):
                continue
            suffix = trainer_id.removeprefix(f"TRAINER_{name}").strip("_")
            match = re.fullmatch(r"(AND_LIZA_)?(\d+)?", suffix)
            if match:
                rematch = int(match.group(2) or 1)
                if name == "TATE" and "AND_LIZA" in suffix:
                    rematch = int(match.group(2) or 2)
                if rematch > 1:
                    return 22, "Gym rematches", gym_number * 100 + rematch
                stage = 2 * gym_number - 1
                return stage, f"Gym {gym_number} · {town}", rematch

    for e4_number, name in enumerate(_ELITE_FOUR, 1):
        if trainer_id.startswith(f"TRAINER_{name}"):
            suffix = trainer_id.removeprefix(f"TRAINER_{name}")
            rematch = int(suffix) if suffix.isdigit() else 1
            return 16 + e4_number, f"Elite Four · {name.title()}", rematch

    if trainer_id == "TRAINER_WALLACE":
        return 21, "Champion", 1
    # This postgame double battle is explicitly triggered in the Champions Room.
    if trainer_id in {"TRAINER_STEVEN_WITH_WALLACE", "TRAINER_WALLACE_WITH_STEVEN"}:
        return 23, "Postgame", 1
    return None


def _chapter_for_map(map_name: str) -> int | None:
    """Return only coarse Hoenn chapters supported by a map's named location."""
    route = re.match(r"Route0?(\d+)", map_name)
    if route:
        number = int(route.group(1))
        if number <= 104:
            return 0
        if 105 <= number <= 109 or number == 116:
            return 2
        if number == 110:
            return 4
        if 111 <= number <= 117:
            return 6
        if 118 <= number <= 120:
            return 10
        if 121 <= number <= 123:
            return 12
        if 124 <= number <= 128:
            return 14
        if 129 <= number <= 134:
            return 16

    areas = (
        (0, ("LittlerootTown", "OldaleTown", "PetalburgWoods")),
        (2, ("DewfordTown", "GraniteCave", "RustboroCity")),
        (4, ("SlateportCity", "MauvilleCity")),
        (6, ("VerdanturfTown", "FallarborTown", "MeteorFalls", "MtChimney", "JaggedPass", "FieryPath", "LavaridgeTown")),
        (8, ("PetalburgCity",)),
        (10, ("FortreeCity", "Route119_WeatherInstitute")),
        (12, ("LilycoveCity", "MtPyre", "AquaHideout", "MagmaHideout")),
        (14, ("MossdeepCity", "SeafloorCavern", "SootopolisCity", "SpaceCenter")),
        (16, ("EverGrandeCity", "VictoryRoad")),
    )
    for chapter, prefixes in areas:
        if map_name.startswith(prefixes):
            return chapter
    return None


def _gym_from_location(location: dict) -> tuple[int, str] | None:
    maps = location["maps"]
    if len(maps) != 1:
        return None
    map_name = maps[0]["map_name"]
    for token, number, town in _GYM_MAPS:
        if map_name.startswith(token):
            return number, f"Gym {number} · {town}"
    return None


def build_trainer_index(trainers: list[dict]) -> list[dict]:
    """Return trainer records ordered by known progression, then searchable Other.

    Each result contains the original ``trainer`` record, ``group``, numeric
    ``rank``, and a readable ``label``. Unknown locations sort alphabetically
    by display name and ID; their order makes no encounter claim.
    """
    indexed = []
    for trainer in trainers:
        trainer_id = trainer.get("id", "")
        name = trainer.get("name") or trainer_id or "Unknown trainer"
        progression = _progression(trainer_id)
        location = _SOURCE_LOCATIONS.get(trainer_id)
        if progression:
            stage, group, variant = progression
            if group == "Gym rematches":
                gym_number, rematch = divmod(variant, 100)
                rank = stage * 1_000_000 + gym_number * 1_000 + rematch
                label = f"{name} · Gym {gym_number} rematch {rematch}"
            else:
                rank = stage * 1_000_000 + 50_000 + (1 if trainer_id == "TRAINER_LIZA" else 0)
            if group == "Champion":
                label = f"{name} · Champion battle"
            elif group == "Postgame":
                label = f"{name} · Champions Room partner battle"
            elif trainer_id == "TRAINER_LIZA":
                label = f"{name} · Partner 2"
            elif group not in {"Gym rematches"}:
                label = f"{name} · {'Rematch ' + str(variant) if variant > 1 else 'First battle'}"
        elif location and _gym_from_location(location):
            gym_number, group = _gym_from_location(location)
            stage = 2 * gym_number - 1
            rank = stage * 1_000_000 + 1_000 + (trainer.get("numeric_id") or 0)
            label = name
        elif location and len(location["maps"]) == 1:
            map_info = location["maps"][0]
            map_label = map_info["label"]
            chapter = _chapter_for_map(map_info["map_name"])
            if chapter is None:
                group = "Unstaged locations"
                rank = 50_000_000 + _LOCATION_RANK[map_label] * 1000 + (trainer.get("numeric_id") or 0)
            else:
                group = _CHAPTERS[chapter]
                rank = chapter * 1_000_000 + _LOCATION_RANK[map_label] * 1000 + (trainer.get("numeric_id") or 0)
            label = f"{map_label} · {name}"
        elif location:
            group = "Unstaged locations"
            rank = 50_000_000 + len(_LOCATION_RANK) * 1000 + (trainer.get("numeric_id") or 0)
            map_labels = ", ".join(item["label"] for item in location["maps"])
            label = f"{map_labels} · {name}"
        else:
            group = "Other"
            # Unmapped trainers stay searchable, with no claimed encounter order.
            rank = 100_000_000
            label = name
        search_terms = [trainer_id, name, group]
        if location:
            search_terms.extend(item["label"] for item in location["maps"])
        indexed.append({
            "trainer": trainer,
            "group": group,
            "rank": rank,
            "label": label,
            "search": " ".join(search_terms).casefold(),
        })

    label_counts = {}
    for row in indexed:
        key = (row["group"], row["label"])
        label_counts[key] = label_counts.get(key, 0) + 1
    occurrences = {}
    for row in indexed:
        if label_counts[(row["group"], row["label"])] > 1:
            key = (row["group"], row["label"])
            occurrences[key] = occurrences.get(key, 0) + 1
            number = row["trainer"].get("numeric_id")
            suffix = f"#{number}" if number is not None else str(occurrences[key])
            row["label"] += f" · {suffix}"

    indexed.sort(key=lambda row: (
        row["rank"],
        row["label"].casefold(),
        row["trainer"].get("id", ""),
    ))
    return indexed


if __name__ == "__main__":
    import json
    from pathlib import Path

    records = json.loads((Path(__file__).parent / "data/game_data.json").read_text(encoding="utf-8"))["trainers"]
    index = build_trainer_index(records)
    groups = [row["group"] for row in index]
    assert groups.index("Before Gym 1") < groups.index("Gym 1 · Rustboro")
    assert groups.index("Gym 1 · Rustboro") < groups.index("After Gym 1 · Before Gym 2")
    assert groups.index("Gym 7 · Mossdeep") < groups.index("Gym 8 · Sootopolis")
    assert groups.index("Gym 8 · Sootopolis") < groups.index("Elite Four · Sidney")
    assert groups.index("Elite Four · Drake") < groups.index("Champion") < groups.index("Gym rematches")
    assert groups.index("Gym rematches") < groups.index("Postgame") < groups.index("Unstaged locations") < groups.index("Other")
    assert next(row for row in index if row["trainer"]["id"] == "TRAINER_WALLACE")["group"] == "Champion"
    assert next(row for row in index if row["trainer"]["id"] == "TRAINER_ROXANNE_2")["group"] == "Gym rematches"
    assert next(row for row in index if row["trainer"]["id"] == "TRAINER_JOSH")["group"] == "Gym 1 · Rustboro"
    assert next(row for row in index if row["trainer"]["id"] == "TRAINER_SAWYER_1")["group"] == "After Gym 3 · Before Gym 4"
    assert len(_SOURCE_LOCATIONS) > 800
    assert all(row["trainer"] in records for row in index)
    print(f"Indexed {len(index)} trainers; {groups.count('Other')} remain in Other.")
