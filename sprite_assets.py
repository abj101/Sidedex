"""Offline lookups for the compact PNG icons bundled with Sidedex."""

from base64 import b64encode
from functools import lru_cache
from pathlib import Path
from struct import unpack


ASSET_ROOT = Path(__file__).parent / "assets"


def _slug(value, prefix):
    key = str(value or "").strip().lower()
    if key.startswith(prefix):
        key = key[len(prefix):]
    return key


@lru_cache(maxsize=4096)
def _data_url(folder, slug):
    path = ASSET_ROOT / folder / f"{slug}.png"
    try:
        encoded = b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return f"data:image/png;base64,{encoded}"


def species_icon(species_id):
    """Return an offline data URL for a species ID or None when unsupported."""
    slug = _slug(species_id, "species_")
    if not slug:
        return None
    parts = slug.split("_")
    for end in range(len(parts), 0, -1):
        icon = _data_url("pokemon", "_".join(parts[:end]))
        if icon:
            return icon
    return None


def species_portrait(species_id):
    """Return the exact portrait, or the best species icon without borrowing base art."""
    slug = _slug(species_id, "species_")
    if slug:
        portrait = _data_url("pokemon", f"{slug}_portrait")
        if portrait:
            return portrait
    return species_icon(species_id)


def item_icon(item_id):
    """Return an offline data URL for an item ID or None when unsupported."""
    slug = _slug(item_id, "item_")
    return _data_url("items", slug) if slug else None


def party_indicator_icon(filled=True):
    """Return the game's filled or empty party Poké Ball icon as a data URL."""
    return _data_url("party", "pokeball_filled" if filled else "pokeball_empty")


def status_icon(status):
    """Return a local game status label sprite as a data URL."""
    slug = str(status or "").removeprefix("STATUS1_").lower()
    slug = {"toxic_poison": "poison"}.get(slug, slug)
    return _data_url("status", slug) if slug else None


def nature_arrow_icon(increase=True):
    """Return the game's nature stat increase/decrease arrow."""
    return _data_url("ui", "nature_arrow_up" if increase else "nature_arrow_down")


def pc_ui_asset(name):
    """Return one rendered asset from the original Pokémon Storage graphics."""
    return _data_url("ui", f"pc_{name}")


@lru_cache(maxsize=1)
def _trainer_art():
    paths = sorted((ASSET_ROOT / "trainers").glob("*.png"))
    result = {}
    for path in paths:
        stem = path.stem.lower()
        base = stem[:-2] if stem.endswith(("_m", "_f")) else stem
        result.setdefault(base, [])
        result[base].append(stem)
    # When an ID does not encode gender, favor the male class portrait.
    return {base: sorted(stems, key=lambda stem: (not stem.endswith("_m"), stem))
            for base, stems in result.items()}


def trainer_icon(trainer_id, trainer_pic=None):
    """Return a matching trainer portrait data URL, or None when unknown."""
    if trainer_pic:
        portrait_slug = _slug(trainer_pic, "trainer_pic_")
        portrait_slug = {"supernerd_firered": "super_nerd_firered",
                         "cue_ball_firered": "cue_ball_front_pic"}.get(portrait_slug, portrait_slug)
        portrait = _data_url("trainers", portrait_slug)
        if portrait:
            return portrait
    slug = _slug(trainer_id, "trainer_")
    if not slug:
        return None
    slug = slug.replace("grunt_aqua", "aqua_grunt").replace("grunt_magma", "magma_grunt")
    art = _trainer_art()
    named_portraits = {
        "roxanne": "leader_roxanne", "brawly": "leader_brawly",
        "wattson": "leader_wattson", "flannery": "leader_flannery",
        "norman": "leader_norman", "winona": "leader_winona",
        "tate": "leader_tate_and_liza", "liza": "leader_tate_and_liza",
        "sidney": "elite_four_sidney", "phoebe": "elite_four_phoebe",
        "glacia": "elite_four_glacia", "drake": "elite_four_drake",
        "steven": "steven", "wallace": "champion_wallace",
        "archie": "aqua_leader_archie", "wally": "wally",
    }
    for name, portrait in named_portraits.items():
        if f"_{name}_" in f"_{slug}_" and portrait in art:
            return _data_url("trainers", art[portrait][0])
    if slug in art:
        return _data_url("trainers", art[slug][0])
    matches = [base for base in art if f"_{base}_" in f"_{slug}_"]
    if not matches:
        return None
    best = max(matches, key=lambda base: (len(base), base))
    return _data_url("trainers", art[best][0])


if __name__ == "__main__":
    dimensions = unpack(">II", (ASSET_ROOT / "pokemon" / "charjabug.png").read_bytes()[16:24])
    assert dimensions == (32, 32)
    assert species_icon("SPECIES_CHARJABUG").startswith("data:image/png;base64,")
    assert species_portrait("SPECIES_POOCHYENA").startswith("data:image/png;base64,")
    assert species_portrait("SPECIES_GROWLITHE_HISUI") == _data_url("pokemon", "growlithe_hisui_portrait")
    assert species_portrait("SPECIES_ROTOM_WASH") == _data_url("pokemon", "rotom_wash_portrait")
    assert species_portrait("SPECIES_ARCEUS_FIRE") == _data_url("pokemon", "arceus_fire_portrait")
    assert species_portrait("SPECIES_ARCEUS_FIRE") != species_portrait("SPECIES_ARCEUS_NORMAL")
    assert species_portrait("SPECIES_ZACIAN_CROWNED") == _data_url("pokemon", "zacian_crowned_portrait")
    assert species_portrait("SPECIES_MINIOR_RED") == species_portrait("SPECIES_MINIOR_METEOR_RED")
    for base, variant in (("PICHU", "PICHU_SPIKY_EARED"), ("ROTOM", "ROTOM_WASH"),
                          ("PALKIA", "PALKIA_PRIMAL"), ("KYUREM", "KYUREM_BLACK"),
                          ("GRENINJA", "GRENINJA_ASH")):
        assert species_portrait(f"SPECIES_{base}") != species_portrait(f"SPECIES_{variant}")
    assert species_portrait("SPECIES_GROWLITHE") == _data_url("pokemon", "growlithe_portrait")
    assert item_icon("ITEM_FOCUS_SASH").startswith("data:image/png;base64,")
    assert party_indicator_icon().startswith("data:image/png;base64,")
    assert party_indicator_icon(False).startswith("data:image/png;base64,")
    assert nature_arrow_icon().startswith("data:image/png;base64,")
    assert nature_arrow_icon(False).startswith("data:image/png;base64,")
    assert unpack(">II", (ASSET_ROOT / "party" / "pokeball_filled.png").read_bytes()[16:24]) == (8, 8)
    assert unpack(">II", (ASSET_ROOT / "party" / "pokeball_empty.png").read_bytes()[16:24]) == (8, 8)
    assert unpack(">II", (ASSET_ROOT / "ui" / "nature_arrow_up.png").read_bytes()[16:24]) == (8, 16)
    assert unpack(">II", (ASSET_ROOT / "ui" / "nature_arrow_down.png").read_bytes()[16:24]) == (8, 16)
    assert trainer_icon("TRAINER_AQUA_GRUNT_1").startswith("data:image/png;base64,")
    assert trainer_icon("TRAINER_ROXANNE_1").startswith("data:image/png;base64,")
    assert species_icon("SPECIES_NOT_IN_SOURCE") is None
    assert species_portrait("SPECIES_NOT_IN_SOURCE") is None
    print("sprite asset lookups passed")
