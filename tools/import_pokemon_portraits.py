"""Import missing front sprites from the pinned game source (requires Pillow)."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops


ROOT = Path(__file__).resolve().parents[1]


def shared_frames():
    """Species whose game entries pair a shared front sprite with a form palette."""
    for name in ("pikachu", "eevee"):
        yield f"{name}_starter", f"{name}/anim_front.png", f"{name}/normal.pal"
    for name, frame, forms, default in (
        ("arceus", "anim_front.png", "normal fighting flying poison ground rock bug ghost steel fire water grass electric psychic ice dragon dark fairy", "normal"),
        ("deerling", "anim_front.png", "spring summer autumn winter", "spring"),
        ("flabebe", "anim_front.png", "red yellow orange blue white", "red"),
        ("floette", "anim_front.png", "red yellow orange blue white", "red"),
        ("florges", "anim_front.png", "red yellow orange blue white", "red"),
        ("squawkabilly", "front.png", "green blue yellow white", "green"),
        ("silvally", "front.png", "normal fighting flying poison ground rock bug ghost steel fire water grass electric psychic ice dragon dark fairy", "normal"),
    ):
        for form in forms.split():
            palette = f"{name}/normal.pal" if form == default else f"{name}/{form}/normal.pal"
            yield f"{name}_{form}", f"{name}/{frame}", palette
    for color in "red orange yellow green blue indigo violet".split():
        yield f"minior_core_{color}", "minior/core/front.png", f"minior/core/{color}/normal.pal"
    for sweet in "strawberry berry love star clover flower ribbon".split():
        for cream in "vanilla_cream ruby_cream matcha_cream mint_cream lemon_cream salted_cream ruby_swirl caramel_swirl rainbow_swirl".split():
            palette_name = "default" if cream == "vanilla_cream" else cream
            yield (f"alcremie_{sweet}_{cream}", f"alcremie/{sweet}/front.png",
                   f"alcremie/{sweet}/{sweet}_{palette_name}.pal")
    for form in "plant sandy trash".split():
        yield f"mothim_{form}", "mothim/anim_front.png", "mothim/normal.pal"
    for drive in "douse shock burn chill".split():
        yield f"genesect_{drive}", "genesect/anim_front.png", f"genesect/{drive}_drive/normal.pal"
    yield "vivillon_icy_snow", "vivillon/anim_front.png", "vivillon/normal.pal"
    yield "vivillon_pokeball", "vivillon/poke_ball/anim_front.png", "vivillon/poke_ball/normal.pal"
    for name in ("scatterbug", "spewpa"):
        for form in "icy_snow polar tundra continental garden elegant meadow modern marine archipelago high_plains sandstorm river monsoon savanna sun ocean jungle fancy pokeball".split():
            yield f"{name}_{form}", f"{name}/anim_front.png", f"{name}/normal.pal"
    yield "furfrou_natural", "furfrou/anim_front.png", "furfrou/normal.pal"
    for color in "red orange yellow green blue indigo violet".split():
        yield f"minior_meteor_{color}", "minior/front.png", "minior/normal.pal"
    yield "ogerpon_teal", "ogerpon/front.png", "ogerpon/normal.pal"


def direct_frames(source):
    """Resolve species entries with explicit frontPic and palette references."""
    header = (source / "src/data/graphics/pokemon.h").read_text(encoding="utf-8")
    symbols = {}
    for kind in ("FrontPic", "Palette"):
        for symbol, path in re.findall(
            rf'gMon{kind}_([A-Za-z0-9_]+)\[\] = INCBIN_U32\("(graphics/pokemon/[^" ]+)"\)', header
        ):
            symbols.setdefault((kind, symbol), path)
    files = list((source / "src/data/pokemon/species_info").glob("*.h"))
    files.append(source / "src/data/pokemon/species_info.h")
    for file in files:
        contents = file.read_text(encoding="utf-8")
        for species_id, body in re.findall(
            r'^\s{4,}\[(SPECIES_[A-Z0-9_]+)\]\s*=\s*\{(.*?)^\s{4,}\},', contents, re.M | re.S
        ):
            front = re.search(r'\.frontPic\s*=\s*gMonFrontPic_([A-Za-z0-9_]+)', body)
            palette = re.search(r'\.palette\s*=\s*gMonPalette_([A-Za-z0-9_]+)', body)
            if not front or not palette:
                continue
            frame_path = symbols.get(("FrontPic", front.group(1)))
            palette_path = symbols.get(("Palette", palette.group(1)))
            if frame_path and palette_path:
                yield (species_id.removeprefix("SPECIES_").lower(),
                       frame_path.removeprefix("graphics/pokemon/").replace(".4bpp.lz", ".png"),
                       palette_path.removeprefix("graphics/pokemon/").replace(".gbapal.lz", ".pal"))


def render_frame(image_path, palette_path=None):
    with Image.open(image_path) as image:
        frame = image.crop((0, 0, 64, 64))
        if palette_path:
            lines = palette_path.read_text(encoding="ascii").splitlines()
            if lines[:2] != ["JASC-PAL", "0100"] or not 1 <= int(lines[2]) <= 16:
                raise ValueError(f"Unexpected palette format: {palette_path}")
            frame.putpalette([int(channel) for line in lines[3:3 + int(lines[2])] for channel in line.split()])
        frame.info["transparency"] = 0
        return frame.convert("RGBA")


def save_frame(image_path, target, palette_path=None):
    render_frame(image_path, palette_path).save(target)


def main(source):
    commit = json.loads((ROOT / "data/source_lock.json").read_text(encoding="utf-8"))["commit"]
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if actual != commit:
        raise SystemExit(f"Expected source commit {commit}, got {actual}")

    species = json.loads((ROOT / "data/game_data.json").read_text(encoding="utf-8"))["species"]
    graphics = source / "graphics/pokemon"
    destination = ROOT / "assets/pokemon"
    imported = 0
    for filename in ("anim_front.png", "front.png"):
        for path in sorted(graphics.rglob(filename)):
            slug = "_".join(path.parent.relative_to(graphics).parts)
            record = species.get(f"SPECIES_{slug.upper()}")
            if not record or record.get("alias_of"):
                continue
            target = destination / f"{slug}_portrait.png"
            if target.exists():
                continue
            save_frame(path, target)
            imported += 1
    for slug, frame, palette in shared_frames():
        record = species.get(f"SPECIES_{slug.upper()}")
        if not record or record.get("alias_of"):
            continue
        target = destination / f"{slug}_portrait.png"
        if target.exists():
            continue
        save_frame(graphics / frame, target, graphics / palette)
        imported += 1
    for slug, frame, palette in direct_frames(source):
        record = species.get(f"SPECIES_{slug.upper()}")
        if not record or record.get("alias_of"):
            continue
        target = destination / f"{slug}_portrait.png"
        expected = render_frame(graphics / frame, graphics / palette)
        if target.exists():
            with Image.open(target) as current:
                if current.size == expected.size and ImageChops.difference(current.convert("RGBA"), expected).getbbox() is None:
                    continue
        expected.save(target)
        imported += 1
    for species_id, record in species.items():
        alias = record.get("alias_of")
        if not alias:
            continue
        target = destination / f"{species_id.removeprefix('SPECIES_').lower()}_portrait.png"
        source_portrait = destination / f"{alias.removeprefix('SPECIES_').lower()}_portrait.png"
        if source_portrait.exists() and (not target.exists() or target.read_bytes() != source_portrait.read_bytes()):
            shutil.copyfile(source_portrait, target)
            imported += 1
    print(f"Imported {imported} source portraits")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
