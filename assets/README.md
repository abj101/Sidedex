# Local game sprites

These PNGs are copied from `graphics/pokemon/*/icon.png`,
`graphics/items/icons/`, and `graphics/trainers/front_pics/` in
[iriv24/pokeemerald-expansion](https://github.com/iriv24/pokeemerald-expansion),
pinned to commit `2576d0f3f4c861c760f0c55198e37e6d7b91683` (the same source revision
as `data/game_data.json`). They are served from disk as data URLs, so the app
does not need a network connection at runtime.

Coverage at this revision: 1,364 Pokémon icons, 1,632 detailed Pokémon portraits,
635 item icons, and 110 trainer front portraits. Icons are cropped to 32×32.
Portraits are copied from `graphics/pokemon/<slug>/anim_front.png` or
`graphics/pokemon/<slug>/front.png`, including nested form directories, cropped
to the first 64×64 frame, and saved with palette index 0 transparent. Run
`tools/import_pokemon_portraits.py <pinned-source-checkout>` to import missing
portraits for species IDs in `data/game_data.json`. The importer follows the
game's front-sprite and palette references, including shared sprites with
form-specific `.pal` files and species aliases. All 1,630 species IDs in the
snapshot now have exact portrait files; two older portrait files are retained
for names that are not species IDs. Unknown IDs use an icon fallback through
`species_portrait()` when one is available.

Trainer portraits are class art; `trainer_icon()` matches the most specific
class token in an ID and returns `None` when it cannot resolve one. Pokémon and
item lookups also return `None` when no local image exists.

The filled and empty 8×8 party indicators in `party/` are the first two tiles
from `graphics/battle_interface/ball_display.png` at the same pinned revision.
Palette index 0 is saved transparent. `party_indicator_icon(filled)` in
`sprite_assets.py` serves the local icons as data URLs.

The six 24×8 status labels in `status/` are cropped from
`graphics/battle_interface/status.png` at the same pinned revision. The sheet
labels are PSN, PAR, SLP, FRZ, BRN, and FRB; toxic poison uses the PSN graphic.
`status_icon(status)` serves these local labels as data URLs.

The 8×16 nature arrows in `ui/` are cropped from
`graphics/fonts/down_arrow.png` at the same pinned revision; the up arrow is
the vertically flipped and green-tinted game tile. `nature_arrow_icon(increase)`
serves them.

The PC data panel, box title and wallpaper art, and box arrows in `ui/` come
from `graphics/pokemon_storage/` at the same revision. The panel and 14
wallpapers were assembled from the game's PNG tiles and `.bin` tilemaps.
Only edge-connected white space around the wallpaper art is transparent so the
app's card background shows through; the artwork pixels remain unchanged.
`pc_hand_cursor.png` is the original Storage sprite; the pointing frame is
cropped, enlarged, and recolored white for the browser cursor.

The pinned source repository has no root license file or stated license in its
README/docs. These assets are included locally for the requested app UI; this
provenance note is not a license grant. Confirm upstream terms before redistributing.
