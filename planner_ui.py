"""Pokedex and Calc interfaces for the local battle planner."""

import html
import json
import random
import re
import sqlite3
from contextlib import closing
from fractions import Fraction
import streamlit as st

import calc_service
from focused_build_component import focused_build
from hp_badge_component import hp_badge
from focused_profile_component import focused_profile
from calc_roster_component import calc_roster
from calc_config_component import calc_config_picker
from matchup import matchup
from pc_box_component import pc_box
from sprite_assets import item_icon, nature_arrow_icon, party_indicator_icon, species_icon, species_portrait, trainer_icon
from trainer_progression import build_trainer_index


TYPE_COLORS = {
    "NORMAL": "#a8a77a", "FIRE": "#e66d39", "WATER": "#4696d5",
    "ELECTRIC": "#d4ad32", "GRASS": "#5da65a", "ICE": "#64bdbd",
    "FIGHTING": "#bd5144", "POISON": "#9a5ca5", "GROUND": "#b89b5c",
    "FLYING": "#8a9ed0", "PSYCHIC": "#d7658d", "BUG": "#8ea13b",
    "ROCK": "#a59559", "GHOST": "#735e9a", "DRAGON": "#7667bc",
    "DARK": "#645a54", "STEEL": "#78949f", "FAIRY": "#c976af",
}
STAT_FIELDS = (("hp", "HP"), ("attack", "Attack"), ("defense", "Defense"),
               ("sp_attack", "Sp. Atk"), ("sp_defense", "Sp. Def"), ("speed", "Speed"))
NATURES = {
    "adamant": ("attack", "sp_attack"), "bashful": (None, None),
    "bold": ("defense", "attack"), "brave": ("attack", "speed"),
    "calm": ("sp_defense", "attack"), "careful": ("sp_defense", "sp_attack"),
    "docile": (None, None), "gentle": ("sp_defense", "defense"),
    "hardy": (None, None), "hasty": ("speed", "defense"),
    "impish": ("defense", "sp_attack"), "jolly": ("speed", "sp_attack"),
    "lax": ("defense", "sp_defense"), "lonely": ("attack", "defense"),
    "mild": ("sp_attack", "defense"), "modest": ("sp_attack", "attack"),
    "naive": ("speed", "sp_defense"), "naughty": ("attack", "sp_defense"),
    "quiet": ("sp_attack", "speed"), "quirky": (None, None),
    "rash": ("sp_attack", "sp_defense"), "relaxed": ("defense", "speed"),
    "sassy": ("sp_defense", "speed"), "serious": (None, None),
    "timid": ("speed", "attack"),
}

CALC_STATE_VERSION = 3
CALC_DIFFICULTIES = ("Easy", "Normal")
CALC_ITEM_PREFIX = {"player": "calc_item_player_", "enemy": "calc_item_enemy_"}
STAT_KEYS = tuple(key for key, _ in STAT_FIELDS)
STAT_STAGE_FIELDS = (("attack", "Attack"), ("defense", "Defense"), ("sp_attack", "Sp. Atk"),
                     ("sp_defense", "Sp. Def"), ("speed", "Speed"),
                     ("accuracy", "Accuracy"), ("evasion", "Evasion"))
ALLOWED_STAGES = frozenset(key for key, _ in STAT_STAGE_FIELDS)
CALC_CARDS = (("player_moves", "Your moves · KO result"),
              ("recommendation", "Simulated recommendation"),
              ("ai_actions", "AI simulated action"),
              ("enemy_moves", "Enemy moves · KO result"))


def esc(value):
    return html.escape(str(value or ""), quote=True)


def human(value):
    if not value:
        return "Unknown"
    result = str(value)
    for prefix in ("ITEM_", "ABILITY_", "NATURE_", "TYPE_", "MOVE_", "SPECIES_"):
        if result.startswith(prefix):
            result = result[len(prefix):]
            break
    return result.replace("_", " ").title()


def species_name(species_id, data):
    name = data.get("species", {}).get(species_id, {}).get("name") or human(species_id)
    base = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    macro = (species_id or "").removeprefix("SPECIES_")
    if macro.startswith(base + "_"):
        form = macro[len(base) + 1:].replace("_", " ").title()
        return f"{name} · {form}"
    return name


def icon(src, label, css_class="sd-sprite"):
    if src:
        return f'<img class="{css_class}" src="{esc(src)}" alt="{esc(label)}">'
    return f'<span class="sd-fallback" aria-label="{esc(label)}">{esc(label[:2].upper())}</span>'


def pokemon_icon(species_id, data, css_class="sd-sprite"):
    return icon(species_portrait(species_id), species_name(species_id, data), css_class)


def badges(types):
    result = []
    for item in types or []:
        label = human(item)
        result.append(f'<span class="sd-type" style="--type:{TYPE_COLORS.get(label.upper(), "#657b82")}">{esc(label)}</span>')
    return "".join(result)


def party_balls(count):
    balls = "".join(f'<img src="{esc(party_indicator_icon(slot < count))}" alt="">' for slot in range(6))
    return f'<span class="sd-party-balls" role="img" aria-label="{esc(count)} of 6 Pokémon">{balls}</span>'


def move_cards_markup(move_ids, data):
    cards = []
    for slot, move_id in enumerate((move_ids or [])[:4]):
        move = data.get("moves", {}).get(move_id, {})
        move_type = human(move.get("type"))
        color = TYPE_COLORS.get(move_type.upper(), "#9db3b6")
        parts = [move_type]
        if move.get("power") is not None:
            parts.append(f"Power {move['power'] or '—'}")
        accuracy = move.get("accuracy")
        parts.append(f"Accuracy {accuracy if accuracy else '—'}")
        meta = f'<span class="sd-type" style="--type:{color}">{esc(move_type)}</span>' + "".join(f"<span>{esc(part)}</span>" for part in parts[1:])
        pp = f'<span class="sd-move-pp">{esc(move["pp"])} PP</span>' if move.get("pp") is not None else ""
        description = esc((move.get("description") or "").replace("\n", " "))
        description_html = f"<p>{description}</p>" if description else ""
        name = move.get("name") or human(move_id)
        delete = f'<button class="sd-move-delete" type="button" aria-label="Delete {esc(name)}" title="Delete move"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3m-9 0 1 13h10l1-13M10 10v7m4-7v7"/></svg></button>'
        cards.append(f'<div class="sd-move-card" data-slot="{slot}" data-move-id="{esc(move_id)}" tabindex="0" role="button" aria-label="Change {esc(name)}" aria-expanded="false" style="--type:{color}"><div class="sd-move-head"><strong>{esc(name)}</strong><span class="sd-move-head-right">{pp}{delete}</span></div><div class="sd-move-meta">{meta}</div>{description_html}<div class="sd-picker-menu" hidden><input type="search" aria-label="Search moves" placeholder="Search moves" autocomplete="off"><div class="sd-picker-options" role="listbox" aria-label="Moves"></div></div></div>')
    for slot in range(len(cards), 4):
        cards.append(f'<div class="sd-move-card sd-move-empty" data-slot="{slot}"><button class="sd-move-add" type="button" aria-label="Add move to slot {slot + 1}" aria-expanded="false">+</button><div class="sd-picker-menu" hidden><input type="search" aria-label="Search moves" placeholder="Search moves" autocomplete="off"><div class="sd-picker-options" role="listbox" aria-label="Moves"></div></div></div>')
    return f'<div class="sd-moves">{"".join(cards)}</div>'


def matchup_indicator_state(selected, enemy, data):
    result = matchup({"species": selected["species"], "moves": known_moves(selected, data)}, enemy, data)
    outgoing, incoming = result["outgoing_best"], result["incoming_best"]
    if outgoing is None or incoming is None:
        return "unknown"
    return "player" if outgoing > incoming else "enemy" if incoming > outgoing else "neutral"


def matchup_indicator(selected, enemy, data):
    enemy_name = species_name(enemy.get("species"), data)
    selected_name = species_name(selected["species"], data)
    state = matchup_indicator_state(selected, enemy, data)
    labels = {
        "player": f"Advantage: {selected_name} has stronger known type effectiveness against {enemy_name}.",
        "enemy": f"Disadvantage: {enemy_name} has stronger known type effectiveness against {selected_name}.",
        "neutral": f"Neutral: best known type effectiveness is equal against {enemy_name}.",
        "unknown": f"Unknown: known move or type data is incomplete for {selected_name} and {enemy_name}.",
    }
    glyph = {"player": "↑", "enemy": "↓", "neutral": "↔", "unknown": "?"}[state]
    label = labels[state]
    return f'<span class="sd-match-indicator {state}" role="img" aria-label="{esc(label)}" title="{esc(label)}">{glyph}</span>'


def build_markup(ability, item, nature):
    item_art = icon(item_icon(item), human(item), "sd-icon-inline") if item and item_icon(item) else ""
    return (f'<div class="sd-build"><b>Ability</b><span>{esc(human(ability))}</span>'
            f'<b>Item</b><span class="sd-build-item">{item_art}{esc(human(item))}</span>'
            f'<b>Nature</b><span>{esc(human(nature))}</span></div>')


def roster(trainer, data, resolve_level, highest_level, selected=None):
    cards = []
    for enemy in trainer.get("party", []):
        sid = enemy.get("species")
        name = species_name(sid, data) if sid else "Unknown Pokémon"
        level = resolve_level(enemy.get("raw_level", enemy.get("level")), highest_level)
        build = build_markup(enemy.get("ability"), enemy.get("item"), enemy.get("nature"))
        indicator = matchup_indicator(selected, enemy, data) if selected else ""
        cards.append(f'<div class="sd-enemy-card">{indicator}<div class="sd-enemy-top">{pokemon_icon(sid, data)}<div><strong>{esc(name)}</strong><div class="sd-sub sd-level-types">Lv {esc(level)}<span class="sd-badges">{badges(data.get("species", {}).get(sid, {}).get("types"))}</span></div></div></div>{build}</div>')
    st.html(f'<div class="sd-roster">{"".join(cards)}</div>')


def select_pokemon(entry_id):
    st.session_state["selected_pokemon_id"] = entry_id


def party_grid(rows, data, selected_id, connect, save_entry):
    for index in range(6):
        if index % 2 == 0:
            slots = st.columns(2, gap="small")
        with slots[index % 2]:
            if index >= len(rows):
                with st.container(key=f"party_slot_empty_{index}"):
                    if st.button("Add Pokémon to empty party slot", help="Add a Pokémon directly to your party", key=f"party_empty_{index}", width="stretch"):
                        edit_pokemon(connect, data, save_entry, default_in_party=True)
                continue
            row = rows[index]
            name = species_name(row["species"], data)
            selected = row["id"] == selected_id
            slot_key = f"party_slot_selected_{row['id']}" if selected else f"party_slot_{row['id']}"
            with st.container(key=slot_key):
                build = build_markup(row["ability"], row["item"], row["nature"])
                types = badges(data.get("species", {}).get(row["species"], {}).get("types"))
                st.html(f'<div class="sd-enemy-card"><div class="sd-enemy-top">{pokemon_icon(row["species"], data)}<div><strong>{esc(name)}</strong><div class="sd-sub sd-level-types">Lv {esc(row["level"] or "?")}<span class="sd-badges">{types}</span></div></div></div>{build}</div>')
                st.button(f"Select {name}", key=f"party_select_{row['id']}", width="stretch",
                          on_click=select_pokemon, args=(row["id"],))


def box_grid(rows, selected_id, data, box_no, db, move_box_slot, *, select_on_click=True, compact=False):
    entries = []
    for row in rows:
        species = data.get("species", {}).get(row["species"], {})
        entries.append({
            "id": row["id"], "slot": row["box_slot"], "name": species_name(row["species"], data),
            "level": row["level"], "status": row["status"].title(),
            "types": [human(value) for value in species.get("types", [])],
            "ability": human(row["ability"]), "nature": human(row["nature"]),
            "item": human(row["item"]) if row["item"] else "None",
            "item_icon_url": item_icon(row["item"]) or "",
            "mini_url": species_icon(row["species"]),
            "portrait_url": species_portrait(row["species"]),
        })
    result = pc_box(entries, selected_id, box_no=box_no, compact=compact,
                    key="pc-box" if select_on_click else "pc-box-calc")
    move = result.move
    if move and move.get("token") != st.session_state.get("pc_last_move_token"):
        st.session_state["pc_last_move_token"] = move["token"]
        try:
            move_box_slot(db, int(move["id"]), int(move["box_no"]), int(move["slot"]))
            st.rerun(scope="fragment")
        except (ValueError, TypeError, KeyError) as exc:
            st.error(str(exc))
    if result.box is not None and result.box != box_no:
        st.session_state["pc_box_number"] = result.box
        st.rerun(scope="fragment")
    if select_on_click and result.selected is not None and result.selected != selected_id:
        select_pokemon(result.selected)
        st.rerun(scope="fragment")


def detail_markup(row, data):
    return f'<strong class="sd-focused-name">{esc(species_name(row["species"], data))}</strong>'


def nature_effect(nature):
    return NATURES.get(human(nature).lower(), (None, None))


def nature_option(value):
    if not value:
        return "Choose nature"
    name = human(value)
    up, down = nature_effect(value)
    if not up:
        return f"{name} · neutral"
    labels = {"attack": "atk", "defense": "def", "sp_attack": "spa",
              "sp_defense": "spdef", "speed": "spd"}
    return f"{name} · +{labels[up]} / -{labels[down]}"


def save_focused_field(connect, save_entry, row, field, value):
    if field == "nature" and not value:
        value = "NATURE_SERIOUS"
    elif field == "item" and not value:
        value = None
    values = {key: row[key] for key in (
        "species", "level", "ability", "item", "nature", "status", "in_party",
        "owner", "pair_id", "box_no",
    )}
    values[field] = value
    values.update({"moves": value if field == "moves" else json.loads(row["moves"] or "[]"),
                   "ivs": json_dict(row["ivs"]), "evs": json_dict(row["evs"])})
    with closing(connect()) as db:
        save_entry(db, values, row["id"])


def calculate_stats(base_stats, level, nature, ivs=None, evs=None, apply_nature=True):
    """Calculate generation III stats, treating unspecified IVs/EVs as zero."""
    up, down = nature_effect(nature)
    ivs, evs = ivs or {}, evs or {}
    result = {}
    for key, base in base_stats.items():
        if not isinstance(base, (int, float)):
            continue
        iv = max(0, min(31, int(ivs.get(key, 0) or 0)))
        ev = max(0, min(255, int(evs.get(key, 0) or 0)))
        value = ((2 * base + iv + ev // 4) * level) // 100
        if key == "hp":
            value += level + 10
        else:
            value += 5
            if apply_nature:
                value = int(value * (1.1 if key == up else 0.9 if key == down else 1))
        result[key] = value
    return result


def stat_bars_markup(base_stats, level, nature, ivs=None, evs=None, show_unknown=False):
    stats = calculate_stats(base_stats, level, nature, ivs, evs) if level is not None else base_stats
    original_stats = (calculate_stats(base_stats, level, nature, ivs, evs, apply_nature=False)
                      if level is not None else stats)
    up, down = nature_effect(nature) if level is not None else (None, None)
    bars = []
    for key, label in STAT_FIELDS:
        value = stats.get(key)
        if not isinstance(value, (int, float)):
            if show_unknown:
                bars.append(f'<div class="sd-stat"><span>{esc(label)}</span><div></div><b>Unknown</b></div>')
            continue
        base = base_stats[key]
        color = "#df6670" if base < 60 else "#e1b850" if base < 100 else "#67b985"
        width = min(100, max(3, round(value / 300 * 100)))
        nature_class = " up" if key == up else " down" if key == down else ""
        arrow = (icon(nature_arrow_icon(key == up), f"Nature raises {label}" if key == up else f"Nature lowers {label}", "sd-nature-arrow")
                 if key in (up, down) else "")
        delta = value - original_stats.get(key, value)
        delta_markup = f'<small class="sd-stat-delta">{delta:+g}</small>' if delta else ""
        bars.append(f'<div class="sd-stat{nature_class}"><span>{esc(label)}</span><div><i style="width:{width}%;background:{color}"></i></div><b>{value:g}{arrow}{delta_markup}</b></div>')
    if bars and (not show_unknown or len(stats) == len(STAT_FIELDS)):
        total = sum(value for value in stats.values() if isinstance(value, (int, float)))
        bars.append(f'<div class="sd-stat sd-stat-total"><span>Total</span><div><i style="width:{min(100, round(total / 900 * 100))}%;background:#67b985"></i></div><b>{total:g}</b></div>')
    return "".join(bars)


def species_profile_markup(row, data):
    species_id = row["species"]
    record = data.get("species", {}).get(species_id, {})
    base_stats = record.get("stats") or {}
    level = int(row["level"] or 1)
    nature = row["nature"]
    ivs, evs = json_dict(row["ivs"]), json_dict(row["evs"])
    ability_label = human(row["ability"])
    ability_description = (data.get("abilities", {}).get(row["ability"])
                           or ("Description unavailable." if row["ability"] else "Choose an ability to see its effect.")).replace("\n", " ")
    abilities_html = f'<strong>Ability</strong><div class="sd-ability-editor"><span class="sd-ability-picker"><button class="sd-ability-trigger" type="button" aria-label="Change ability" aria-expanded="false">{esc(ability_label)}</button><div class="sd-picker-menu" role="listbox" aria-label="Ability" hidden></div></span><span class="sd-ability-description">{esc(ability_description)}</span></div>'
    if not base_stats and not record.get("abilities"):
        return f'<div class="sd-profile-body"><div class="sd-profile-stats"><strong>Stats</strong><p>Stats are not available for this Pokémon.</p>{abilities_html}</div><div class="sd-profile-moves"><strong>Known moves</strong>{move_cards_markup(known_moves(row, data), data)}</div></div>'
    moves = known_moves(row, data)
    stats = f'<div class="sd-profile">{stat_bars_markup(base_stats, level, nature, ivs, evs)}{abilities_html}</div>'
    return f'<div class="sd-profile-body"><div class="sd-profile-stats"><strong>Stats</strong>{stats}</div><div class="sd-profile-moves"><strong>Known moves</strong>{move_cards_markup(moves, data)}</div></div>'


def focused_fields(row, data, connect, save_entry):
    entry_id = row["id"]
    current_nature = row["nature"] or "NATURE_SERIOUS"
    nature_choices = [f"NATURE_{name.upper()}" for name in NATURES]
    if current_nature not in nature_choices:
        nature_choices.append(current_nature)
    nature_choices.remove(current_nature)
    nature_choices.insert(0, current_nature)
    nature_options = [{"value": value, "label": human(value),
                       "effect": nature_option(value).partition(" · ")[2]}
                      for value in nature_choices]
    item_options = [{"value": value, "label": human(value) if value else "None",
                     "icon": item_icon(value) or ""}
                    for value in options_from_trainers(data, "item", row["item"])]
    result = focused_build(
        level=row["level"] or "?", nature=current_nature,
        nature_label=human(current_nature), nature_options=nature_options,
        item=row["item"] or "", item_label=human(row["item"]) if row["item"] else "None",
        item_options=item_options, item_icon_url=item_icon(row["item"]) or "",
        key=f"focused-build-{entry_id}",
    )
    changed = result.changed
    token_key = f"focused_change_token_{entry_id}"
    if changed and changed.get("token") != st.session_state.get(token_key):
        st.session_state[token_key] = changed["token"]
        field, value = changed.get("field"), changed.get("value")
        if field in {"nature", "item"} or (field == "level" and isinstance(value, int) and 1 <= value <= 100):
            save_focused_field(connect, save_entry, row, field, value)
            st.rerun(scope="fragment")


def focused_profile_fields(row, data, connect, save_entry):
    species = data.get("species", {}).get(row["species"], {})
    known = known_moves(row, data)[:4]
    abilities = list(dict.fromkeys(species.get("abilities") or []))
    if not row["ability"] and abilities:
        save_focused_field(connect, save_entry, row, "ability", random.choice(abilities))
        st.rerun(scope="fragment")
    if row["ability"] and row["ability"] not in abilities:
        abilities.append(row["ability"])
    result = focused_profile(
        markup=species_profile_markup(row, data), ability=row["ability"] or "",
        abilities=[{"value": value, "label": human(value),
                    "description": (data.get("abilities", {}).get(value) or "Description unavailable.").replace("\n", " ")}
                   for value in abilities],
        known_moves=known,
        moves=[{"value": value, "label": data["moves"][value].get("name") or human(value)}
               for value in legal_moves(row["species"], data)],
        key=f"focused-profile-{row['id']}",
    )
    changed = result.changed
    token_key = f"profile_change_token_{row['id']}"
    if not changed or changed.get("token") == st.session_state.get(token_key):
        return
    st.session_state[token_key] = changed["token"]
    field, value, slot = changed.get("field"), changed.get("value"), changed.get("slot")
    if field == "ability" and value in abilities:
        save_focused_field(connect, save_entry, row, "ability", value)
    elif field == "move_remove" and isinstance(slot, int) and 0 <= slot < len(known):
        save_focused_field(connect, save_entry, row, "moves", known[:slot] + known[slot + 1:])
    elif field == "move_add" and slot == len(known) and len(known) < 4 and value in legal_moves(row["species"], data) and value not in known:
        save_focused_field(connect, save_entry, row, "moves", known + [value])
    elif field == "move_replace" and isinstance(slot, int) and 0 <= slot < len(known) and value in legal_moves(row["species"], data) and value not in known[:slot] + known[slot + 1:]:
        save_focused_field(connect, save_entry, row, "moves", known[:slot] + [value] + known[slot + 1:])
    else:
        return
    st.rerun(scope="fragment")


def nature_stats_self_check():
    base = {"hp": 100, "attack": 100, "defense": 100, "sp_attack": 100, "sp_defense": 100, "speed": 100}
    stats = calculate_stats(base, 50, "NATURE_ADAMANT")
    assert stats == {"hp": 160, "attack": 115, "defense": 105, "sp_attack": 94, "sp_defense": 105, "speed": 105}
    assert calculate_stats(base, 50, "NATURE_HARDY")["attack"] == 105


def focused_profile_self_check():
    from sqlite3 import connect

    move_data = {"moves": {"MOVE_TACKLE": {"name": "Tackle", "pp": 35, "type": "TYPE_NORMAL"}}}
    markup = move_cards_markup(["MOVE_TACKLE"], move_data)
    assert markup.count('class="sd-move-card') == 4
    assert markup.count('class="sd-move-add"') == 3
    assert 'class="sd-move-delete"' in markup
    saved = []
    row = {"id": 1, "species": "SPECIES_TEST", "level": 5, "ability": None,
           "item": None, "nature": None, "status": "alive", "in_party": 1,
           "owner": None, "pair_id": None, "box_no": 1, "moves": '["MOVE_TACKLE"]',
           "ivs": "{}", "evs": "{}"}
    save_focused_field(lambda: connect(":memory:"), lambda db, values, entry_id: saved.append(values), row, "moves", [])
    assert saved[0]["moves"] == []


def json_dict(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def options_from_trainers(data, field, current=None):
    values = {member.get(field) for trainer in data.get("trainers", []) for member in trainer.get("party", [])}
    values.discard(None)
    values.discard("")
    if current:
        values.add(current)
    return [""] + sorted(values, key=human)


def legal_moves(species_id, data):
    moves = data.get("species", {}).get(species_id, {}).get("learnable_moves") or []
    return sorted((move for move in moves if move in data.get("moves", {})),
                  key=lambda mid: data["moves"][mid].get("name") or mid)


def known_moves(row, data):
    raw = row["moves"] if "moves" in row.keys() else []
    try:
        moves = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    species_id = row["species"] if "species" in row.keys() else None
    allowed = set(data.get("species", {}).get(species_id, {}).get("learnable_moves") or [])
    return [move for move in moves if move in allowed and move in data.get("moves", {})]


@st.dialog("Add Pokémon", width="small")
def edit_pokemon(connect, data, save_entry, entry_id=None, default_in_party=False, default_box=1):
    with closing(connect()) as db:
        _edit_pokemon(db, data, save_entry, entry_id, default_in_party, default_box)


def _edit_pokemon(db, data, save_entry, entry_id=None, default_in_party=False, default_box=1):
    existing = db.execute("SELECT * FROM owned WHERE id=?", (entry_id,)).fetchone() if entry_id else None
    if entry_id and not existing:
        st.error("This Pokémon is no longer in your Box.")
        return
    species = data.get("species", {})
    species_ids = sorted((sid for sid in species if sid != "SPECIES_NONE"), key=lambda sid: (species_name(sid, data), sid))
    if not species_ids:
        st.error("Species data is unavailable.")
        return
    default = existing["species"] if existing and existing["species"] in species else None
    chosen = st.selectbox("Pokémon", species_ids, index=species_ids.index(default) if default else None,
                          placeholder="Search Pokémon", format_func=lambda sid: species_name(sid, data),
                          key=f"pokemon_species_{entry_id or 'new'}")
    move_choices = legal_moves(chosen, data)
    if not existing and not default_in_party:
        default_box = st.session_state.get("new_pokemon_box_target", default_box)
    box_key = (f"pokemon_box_{entry_id}" if existing else
               f"pokemon_box_new_{'party' if default_in_party else st.session_state.get('new_pokemon_dialog_id', 0)}")
    with st.form(f"pokemon_form_{entry_id or 'new'}", enter_to_submit=False):
        left, right = st.columns(2)
        level = left.number_input("Level", min_value=1, max_value=100, value=int(existing["level"] or 1) if existing else 1)
        statuses = ("alive", "dead", "unavailable")
        status = right.selectbox("Status", statuses, index=statuses.index(existing["status"]) if existing else 0, format_func=str.title)
        in_party = st.checkbox("Put in party", value=bool(existing["in_party"]) if existing else default_in_party)
        box_no = st.selectbox("Box", range(1, 15), index=(existing["box_no"] if existing else default_box) - 1,
                              format_func=lambda number: f"Box {number}", key=box_key)
        st.markdown("**Battle build**")
        left, right = st.columns(2)
        ability_choices = species.get(chosen, {}).get("abilities") or []
        if ability_choices:
            default_ability = existing["ability"] if existing and existing["ability"] in ability_choices else random.choice(ability_choices)
            ability = left.selectbox("Ability", ability_choices, index=ability_choices.index(default_ability), format_func=human)
        else:
            ability = None
            left.selectbox("Ability", [""], format_func=lambda value: "Unknown", disabled=True)
        item_choices = options_from_trainers(data, "item", existing["item"] if existing else None)
        item = right.selectbox("Held item", item_choices, index=item_choices.index(existing["item"]) if existing and existing["item"] in item_choices else 0, format_func=lambda value: human(value) if value else "None", accept_new_options=True)
        default_nature = (existing["nature"] if existing else None) or "NATURE_SERIOUS"
        nature_choices = options_from_trainers(data, "nature", default_nature)
        nature = st.selectbox("Nature", nature_choices, index=nature_choices.index(default_nature), format_func=lambda value: human(value) if value else "Unknown", accept_new_options=True)
        try:
            old_moves = json.loads(existing["moves"]) if existing else []
        except (TypeError, json.JSONDecodeError):
            old_moves = []
        known = [mid for mid in old_moves if mid in move_choices]
        if existing and len(known) != len(old_moves):
            st.warning("Stored moves this Pokémon cannot learn are excluded from the move list.")
        moves = st.multiselect("Known moves, up to four", move_choices, default=known, max_selections=4, format_func=lambda mid: data["moves"][mid].get("name") or human(mid))
        left, right = st.columns(2)
        owner = left.text_input("Owner", value=existing["owner"] or "" if existing else "")
        pair = right.text_input("Soul Link pair", value=existing["pair_id"] or "" if existing else "")
        with st.expander("Advanced details"):
            old_ivs = json_dict(existing["ivs"] if existing else "{}")
            old_evs = json_dict(existing["evs"] if existing else "{}")
            st.caption("Individual values (IVs)")
            iv_columns = st.columns(3)
            iv_inputs = {key: iv_columns[index % 3].number_input(f"{label} IV", 0, 31, int(old_ivs.get(key, 0)), key=f"iv_{entry_id}_{key}") for index, (key, label) in enumerate(STAT_FIELDS)}
            st.caption("Effort values (EVs)")
            ev_columns = st.columns(3)
            ev_inputs = {key: ev_columns[index % 3].number_input(f"{label} EV", 0, 255, int(old_evs.get(key, 0)), key=f"ev_{entry_id}_{key}") for index, (key, label) in enumerate(STAT_FIELDS)}
        _, save_col = st.columns([3, 1])
        saved = save_col.form_submit_button("Save", type="primary", width="stretch")
    if saved:
        try:
            if not chosen:
                raise ValueError("Choose a Pokémon before saving.")
            if any(move not in move_choices for move in moves):
                raise ValueError("Choose only moves this Pokémon can learn.")
            ivs = old_ivs | {key: value for key, value in iv_inputs.items() if value or key in old_ivs}
            evs = old_evs | {key: value for key, value in ev_inputs.items() if value or key in old_evs}
            saved_id = save_entry(db, {"species": chosen, "level": level, "ability": ability,
                                       "item": item or None, "nature": nature or None, "moves": moves,
                                       "ivs": ivs, "evs": evs, "status": status, "in_party": in_party,
                                       "owner": owner or None, "pair_id": pair or None, "box_no": box_no}, entry_id)
            st.session_state["selected_pokemon_id"] = saved_id
            st.session_state["pc_box_number"] = box_no
            st.rerun()
        except (ValueError, json.JSONDecodeError) as exc:
            st.error(str(exc))
    if existing:
        with st.expander("Remove this Pokémon"):
            confirmed = st.checkbox("I understand this permanently deletes the record", key=f"delete_confirm_{entry_id}")
            if st.button("Delete Pokémon", disabled=not confirmed):
                db.execute("DELETE FROM owned WHERE id=?", (entry_id,))
                db.commit()
                st.session_state.pop("selected_pokemon_id", None)
                st.query_params.pop("pokemon", None)
                st.rerun()


def trainer_selection(trainer_rows, party, resolve_level, stacked=False, highest_level=None):
    groups = list(dict.fromkeys(row["group"] for row in trainer_rows))
    all_groups = ["All trainers"] + groups
    requested_group = st.query_params.get("group")
    default_group = requested_group if requested_group in all_groups else next(
        (group for group in groups if group.startswith("Gym 1")), groups[0] if groups else "All trainers")
    if st.session_state.get("sd_chapter") not in all_groups:
        st.session_state["sd_chapter"] = default_group
    if stacked:
        trainer_col, group_col = st.container(), st.container()
    else:
        trainer_col, group_col = st.columns([2, 1], gap="medium")
    with group_col:
        group = st.selectbox("Chapter", all_groups, key="sd_chapter", persist_state="session")
    candidates = trainer_rows if group == "All trainers" else [row for row in trainer_rows if row["group"] == group]
    by_id = {row["trainer"]["id"]: row for row in candidates}
    options = list(by_id)
    requested_trainer = st.session_state.get("shared_trainer_id", st.query_params.get("trainer"))
    if st.session_state.get("sd_trainer") not in by_id:
        st.session_state["sd_trainer"] = requested_trainer if requested_trainer in by_id else options[0] if options else None
    with trainer_col:
        trainer_id = st.selectbox(
            "Trainer", options, key="sd_trainer", persist_state="session",
            format_func=lambda tid: f"{by_id[tid]['group']} · {by_id[tid]['label']}" if group == "All trainers" else by_id[tid]["label"],
            placeholder="Search trainers",
        ) if options else None
    if not trainer_id:
        return None, None, None, None
    st.session_state["shared_trainer_id"] = trainer_id
    trainer = by_id[trainer_id]["trainer"]
    if "highest_party_level" in trainer.get("context_required", []) and highest_level is None:
        # Fallback only: with an empty party there is nothing to raise to the cap.
        highest_level = st.number_input(
            "Highest party level for scaled opponent levels (user-entered context)",
            1, 100, max((row["level"] or 1 for row in party), default=50),
            key="sd_highest_party_level", persist_state="session")
    return trainer_id, trainer, by_id[trainer_id]["group"], highest_level


def render_pokedex(db, data, save_entry, resolve_level, connect, move_box_slot):
    rows = db.execute("SELECT * FROM owned ORDER BY id").fetchall()
    party = sorted((row for row in rows if row["in_party"]), key=lambda row: row["party_slot"] if row["party_slot"] is not None else 6)
    trainer_rows = build_trainer_index(data.get("trainers", []))
    with st.container(border=True, key="encounter-panel"):
        trainer_id, trainer, trainer_group, highest_level = trainer_selection(trainer_rows, party, resolve_level)
    if not trainer_id:
        st.info("No trainers are available in this chapter.")
        return
    battle_format = "Doubles" if trainer.get("double_battle") == "TRUE" else "Singles"
    selected_raw = st.session_state.get("selected_pokemon_id", st.query_params.get("pokemon"))
    try:
        selected_id = int(selected_raw) if selected_raw else (party[0]["id"] if party else rows[-1]["id"] if rows else None)
    except ValueError:
        selected_id = party[0]["id"] if party else rows[-1]["id"] if rows else None
    selected = next((row for row in rows if row["id"] == selected_id), party[0] if party else rows[-1] if rows else None)
    selected_id = selected["id"] if selected else None
    st.session_state["selected_pokemon_id"] = selected_id
    party_col, trainer_col = st.columns([1, 1], gap="large")
    with party_col:
        portrait = trainer_icon("brendan")
        if portrait:
            st.html(f'<div class="sd-trainer-head">{icon(portrait, "You")}<div><strong>You</strong><small>Player · {esc(battle_format)}</small></div>{party_balls(len(party))}</div>')
        party_grid(party, data, selected_id, connect, save_entry)
    with trainer_col:
        portrait = trainer_icon(trainer_id, trainer.get("trainer_pic"))
        st.html(f'<div class="sd-trainer-head">{icon(portrait, trainer.get("name") or "Trainer")}<div><strong>{esc(trainer.get("name") or human(trainer_id))}</strong><small>{esc(trainer_group)} · {esc(battle_format)}</small></div>{party_balls(len(trainer.get("party", [])))}</div>')
        roster(trainer, data, resolve_level, highest_level, selected)
    if selected:
        with st.container(key="selected-card"):
            with st.container(key="selected-heading"):
                portrait_col, info_col = st.columns([1, 10], gap="small", vertical_alignment="center")
                with portrait_col:
                    st.html(pokemon_icon(selected["species"], data, "sd-focused-sprite"))
                with info_col:
                    st.html(detail_markup(selected, data))
                    focused_fields(selected, data, connect, save_entry)
            st.html('<div class="sd-detail-divider"></div>')
            focused_profile_fields(selected, data, connect, save_entry)
            with st.container(horizontal=True, key="selected-pokemon-actions"):
                if st.button("Edit Pokémon", icon=":material/edit:", help="Edit Pokémon", key="edit_selected"):
                    edit_pokemon(connect, data, save_entry, selected["id"])
                new_party = not bool(selected["in_party"])
                open_box_no = st.session_state.setdefault("pc_box_number", 1)
                move_label = "Move to party" if new_party else f"Move to Box {open_box_no}"
                if st.button(move_label, icon=":material/drive_file_move:", help=move_label, key="move_selected"):
                    values = {key: selected[key] for key in ("species", "level", "ability", "item", "nature", "status", "owner", "pair_id", "box_no")}
                    values.update({"moves": known_moves(selected, data), "ivs": json_dict(selected["ivs"]), "evs": json_dict(selected["evs"]), "in_party": new_party})
                    if not new_party:
                        values["box_no"] = open_box_no
                    try:
                        save_entry(db, values, selected["id"])
                        st.rerun(scope="fragment")
                    except ValueError as exc:
                        st.error(str(exc))
    with st.container(key="pc-box-card"):
        box_no = st.session_state.setdefault("pc_box_number", 1)
        box_grid([row for row in rows if not row["in_party"] and row["box_no"] == box_no], selected_id, data, box_no, db, move_box_slot)
        if st.button("Add Pokémon to Box", icon=":material/add:", help="Add a Pokémon to your Box", key="add_box"):
            st.session_state["new_pokemon_box_target"] = box_no
            st.session_state["new_pokemon_dialog_id"] = st.session_state.get("new_pokemon_dialog_id", 0) + 1
            edit_pokemon(connect, data, save_entry, default_box=box_no)
    with st.expander("Game data and limitations"):
        source = data.get("source", {})
        st.caption(f"Source: {source.get('repo', 'unknown')} at {source.get('commit', 'unknown')}")
        st.caption("Conditional parties, adjusted difficulty levels, damage, and AI decisions are not modeled.")
        if data.get("warnings"):
            st.caption(f"Extractor diagnostics: {len(data['warnings'])} unresolved fields")


def _coerce_int(value):
    """Return an int for ints, whole floats, numpy scalars, and numeric strings."""
    if isinstance(value, bool) or value is None:
        return None
    if hasattr(value, "item") and not isinstance(value, (int, float, str)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("+-").isdigit():
            return int(text)
    return None


def stat_map(values, allowed):
    """Keep only known stats. An absent key stays unknown; a stored zero stays zero."""
    if not isinstance(values, dict):
        return {}
    result = {}
    for key, value in values.items():
        number = _coerce_int(value)
        if key in allowed and number is not None:
            result[key] = number
    return result


def list_or_none(value):
    if not isinstance(value, (list, tuple)):
        return None
    items = [str(item) for item in value if item not in (None, "")]
    return items or None


def seed_choice(key, options, default):
    """Point a widget key at a value its current option set contains."""
    if key not in st.session_state or st.session_state[key] not in options:
        st.session_state[key] = default
    return st.session_state[key]


def item_enabled(flags, key, default=True):
    return bool(flags.get(key, default)) if isinstance(flags, dict) else bool(default)


def player_use_item(row, flags):
    return item_enabled(flags, CALC_ITEM_PREFIX["player"] + str(row["id"]))


def enemy_use_item(trainer_id, index, flags):
    return item_enabled(flags, f"{CALC_ITEM_PREFIX['enemy']}{trainer_id}_{index}")


def calc_player_build(row, data, state, flags):
    entry = (state.get("player") or {}).get(str(row["id"]))
    entry = entry if isinstance(entry, dict) else {}
    moves = list(dict.fromkeys(known_moves(row, data)))[:4]
    pp = entry.get("pp") if isinstance(entry.get("pp"), dict) else {}
    use_item = player_use_item(row, flags)
    ivs, evs = json_dict(row["ivs"]), json_dict(row["evs"])
    return {"slot_id": row["id"], "species": row["species"], "level": _coerce_int(row["level"]),
            "ability": row["ability"], "item": (row["item"] or "ITEM_NONE") if use_item else None,
            "nature": row["nature"],
            "ivs": {stat: _coerce_int(ivs.get(stat)) or 0 for stat in STAT_KEYS},
            "evs": {stat: _coerce_int(evs.get(stat)) or 0 for stat in STAT_KEYS},
            "moves": moves,
            "pp": {move_id: _coerce_int(pp.get(move_id)) for move_id in moves if move_id in pp},
            "max_hp": _coerce_int(entry.get("max_hp")), "hp": _coerce_int(entry.get("hp")),
            "status": entry.get("status") if isinstance(entry.get("status"), str) else None,
            "stat_stages": stat_map(entry.get("stat_stages"), ALLOWED_STAGES),
            "use_item": use_item}


def calc_enemy_build(member, data, index, level, state, flags, trainer_id):
    entry = (state.get("enemy") or {}).get(str(index))
    entry = entry if isinstance(entry, dict) else {}
    moves = list(dict.fromkeys(move for move in (member.get("moves") or [])
                               if isinstance(move, str)))[:4]
    pp = entry.get("pp") if isinstance(entry.get("pp"), dict) else {}
    use_item = enemy_use_item(trainer_id, index, flags)
    return {"slot_id": None, "species": member.get("species"), "level": _coerce_int(level),
            "ability": member.get("ability"), "item": member.get("item") if use_item else None,
            "nature": member.get("nature"), "ivs": stat_map(member.get("ivs"), STAT_KEYS),
            "evs": stat_map(member.get("evs"), STAT_KEYS), "moves": moves,
            "pp": {move_id: _coerce_int(pp.get(move_id)) for move_id in moves if move_id in pp},
            "max_hp": _coerce_int(entry.get("max_hp")), "hp": _coerce_int(entry.get("hp")),
            "status": entry.get("status") if isinstance(entry.get("status"), str) else None,
            "stat_stages": stat_map(entry.get("stat_stages"), ALLOWED_STAGES),
            "use_item": use_item}


def calc_planned_action(state):
    action = state.get("planned_action")
    if not isinstance(action, dict):
        return None
    kind = action.get("kind")
    if kind == "move" and isinstance(action.get("move_id"), str) and action["move_id"]:
        return {"kind": "move", "move_id": action["move_id"],
                "target_index": _coerce_int(action.get("target_index"))}
    slot = _coerce_int(action.get("slot"))
    if kind == "switch" and slot is not None and slot >= 0:
        return {"kind": "switch", "slot": slot}
    return None


def parse_rng_words(value):
    """The four SFC32 words, or None when the seed is unknown."""
    if isinstance(value, (list, tuple)):
        words = list(value)
    elif isinstance(value, str):
        words = re.split(r"[,\s]+", value.strip())
        if not all(re.fullmatch(r"\d+", word) for word in words):
            return None
    else:
        return None
    numbers = [_coerce_int(word) for word in words]
    if len(numbers) != 4 or any(number is None or number < 0 or number > 0xFFFFFFFF
                                for number in numbers):
        return None
    return numbers


def parse_flag_text(text):
    return {word: True for word in re.split(r"[,\s]+", str(text or "")) if word}


def calc_progress_flags(state):
    flags = state.get("progress_flags") if isinstance(state.get("progress_flags"), dict) else {}
    return {str(key): bool(value) for key, value in flags.items()}


def calc_history(state):
    history = state.get("history") if isinstance(state.get("history"), dict) else {}
    turns = history.get("turns")
    return [turn for turn in turns if isinstance(turn, dict)] if isinstance(turns, list) else []


def calc_field(state):
    field = state.get("field") if isinstance(state.get("field"), dict) else {}
    return {"weather": field.get("weather") or None, "terrain": field.get("terrain") or None,
            "trick_room": True if field.get("trick_room") is True else None,
            "player_screens": list_or_none(field.get("player_screens")),
            "enemy_screens": list_or_none(field.get("enemy_screens")),
            "player_hazards": list_or_none(field.get("player_hazards")),
            "enemy_hazards": list_or_none(field.get("enemy_hazards"))}


def build_calc_snapshot(data, player_party, enemy_party, state, *, trainer_id, difficulty,
                        level_cap_id=None, player_item_flags=None, enemy_item_flags=None,
                        enemy_items=None, ai_flags=None, enemy_levels=None, source_sha=None):
    """Build the CALC_CONTRACT section 2 BattleSnapshot, or None when it cannot be built.

    The build form's blank IV/EV entries mean zero. Unknown HP, PP, RNG, and
    planned actions stay unknown until supplied by the battle state.
    """
    if not trainer_id or difficulty not in CALC_DIFFICULTIES:
        return None
    state = state if isinstance(state, dict) else {}
    player_party = list(player_party or [])[:6]
    enemy_party = list(enemy_party or [])
    enemy_levels = list(enemy_levels) if isinstance(enemy_levels, (list, tuple)) else []
    if any(not isinstance(member, dict) for member in enemy_party):
        return None
    slot = _coerce_int(state.get("player_active_slot"))
    enemy_index = _coerce_int(state.get("enemy_active_index"))
    if slot is None or not 0 <= slot < len(player_party) or not player_party[slot]:
        return None
    if enemy_index is None or not 0 <= enemy_index < len(enemy_party):
        return None
    active_row = player_party[slot]
    members = [row for row in player_party if row]
    active_index = next((index for index, row in enumerate(members) if row is active_row), None)
    if active_index is None:
        return None
    cap = next((_coerce_int(entry.get("level")) for entry in data.get("level_caps") or []
                if isinstance(entry, dict) and entry.get("id") == level_cap_id), None)
    if cap is not None:
        import battle_engine
        members = battle_engine.simulate_party_level_cap([dict(row) for row in members], cap, data)
    builds = [calc_player_build(row, data, state, player_item_flags) for row in members]
    enemy_builds = [calc_enemy_build(member, data, index,
                                     enemy_levels[index] if index < len(enemy_levels)
                                     else _coerce_int(member.get("raw_level", member.get("level"))),
                                     state, enemy_item_flags, trainer_id)
                    for index, member in enumerate(enemy_party)]
    planned = calc_planned_action(state)
    if planned and planned["kind"] == "switch" and not 0 <= planned["slot"] < len(builds):
        planned = None
    if planned and planned["kind"] == "switch" and planned["slot"] == active_index:
        planned = None
    items = {}
    for key, value in (enemy_items or {}).items():
        count = _coerce_int(value)
        if isinstance(key, str) and count is not None:
            items[key] = count
    rng = state.get("rng") if isinstance(state.get("rng"), dict) else {}
    # Version-2 snapshots keep both game RNG streams explicit. A legacy four-word
    # value is treated as the main stream only when the user entered it.
    legacy_rng = parse_rng_words(state.get("rng")) if not isinstance(state.get("rng"), dict) else None
    rng_main = parse_rng_words(rng.get("main")) if isinstance(rng, dict) else None
    rng_secondary = parse_rng_words(rng.get("secondary")) if isinstance(rng, dict) else None
    if legacy_rng is not None:
        rng_main = legacy_rng
    return {"schema_version": 2,
            "state_origin": ("battle-start" if state.get("state_origin") == "battle-start"
                             else "manual-scenario"),
            "source_sha": str(source_sha) if source_sha is not None
                          else str((data.get("source") or {}).get("commit") or ""),
            "difficulty": difficulty,
            "progress": {"milestone_id": str(level_cap_id or ""), "flags": calc_progress_flags(state)},
            "player": {"party": builds, "active_index": active_index},
            "enemy": {"trainer_id": trainer_id, "party": enemy_builds, "active_index": enemy_index,
                      "items": items,
                      "ai_flags": ([str(flag) for flag in ai_flags if isinstance(flag, str)]
                                   if isinstance(ai_flags, list) else ["AI_FLAG_UNKNOWN"])},
            "field": calc_field(state),
            "rng": {"main": rng_main, "secondary": rng_secondary},
            "history": {"turns": calc_history(state)},
            "planned_action": planned}


def percent_text(probability):
    """Exact rounded percentage from the exact fraction; never a sampled value."""
    if not isinstance(probability, dict) or not probability.get("den"):
        return "Unknown"
    return f"{round(Fraction(100 * int(probability['num']), int(probability['den'])))}%"


def fraction_text(probability):
    return f"{int(probability['num'])}/{int(probability['den'])}"


def probability_text(probability):
    return f"{percent_text(probability)} ({fraction_text(probability)})"


def score_text(score):
    num, den = int(score["num"]), int(score["den"])
    return str(num) if den == 1 else f"{round(Fraction(num, den))} ({num}/{den})"


def basis_text(evidence):
    verified = isinstance(evidence, dict) and (evidence.get("oracle_verified")
                                               or evidence.get("basis") == "oracle-verified")
    return "oracle-verified" if verified else "source-transcribed, not emulator-verified"


def card_result(result, kind):
    """The card's own result; a whole-run failure applies to every card."""
    if not isinstance(result, dict):
        return None
    card = result.get(kind)
    if isinstance(card, dict):
        return card
    if result.get("status") == "unavailable":
        return result
    return None


def card_unavailable_markup(card):
    reason = esc(card.get("reason") or "unavailable")
    detail = esc(card.get("detail") or "No exact result is available for this state.")
    parts = [f'<p class="sd-calc-unavailable"><b>{reason}</b> {detail}</p>']
    missing = [esc(str(item)) for item in (card.get("missing") or [])]
    if missing:
        parts.append('<ul class="sd-calc-missing">'
                     + "".join(f"<li>{item}</li>" for item in missing) + "</ul>")
    mechanics = [esc(str(item)) for item in (card.get("mechanics") or [])]
    if mechanics:
        parts.append('<ul class="sd-calc-mechanics">'
                     + "".join(f"<li>{item}</li>" for item in mechanics) + "</ul>")
    return "".join(parts)


def move_entry_row(move, basis):
    """One gated move entry: an exact row, or that move's own unavailability."""
    name = move.get("move_name") or human(move.get("move_id"))
    if move.get("status") == "unavailable":
        entry = dict(move)
        entry.setdefault("detail", f"{name} cannot be evaluated in this state.")
        return (f'<li class="sd-calc-prob-row sd-calc-entry-unavailable">'
                f'<span class="sd-calc-prob-name">{esc(name)}</span>'
                f'<small class="sd-calc-percent">Not calculated</small>'
                f'{card_unavailable_markup(entry)}</li>')
    probability = move.get("probability") if isinstance(move.get("probability"), dict) else None
    if probability is None and type(move.get("num")) is int and type(move.get("den")) is int:
        probability = {"num": move["num"], "den": move["den"]}
    method = move.get("method") or move.get("status") or ""
    notes = " · ".join(str(note) for note in (move.get("notes") or [])
                       if "seed" not in str(note))
    estimated = move.get("status") == "estimated"
    outcome = ("KO in this simulation" if probability and probability.get("num") else
               "No KO in this simulation") if method in ("known-seed", "verified-fixed-seed") else probability_text(probability)
    sample = move.get("effective_samples")
    interval = move.get("interval_95") if isinstance(move.get("interval_95"), dict) else None
    estimate_meta = ""
    if estimated:
        interval_text = (f"95% interval {round(float(interval.get('low', 0)) * 100)}–"
                         f"{round(float(interval.get('high', 0)) * 100)}%" if interval else "")
        estimate_meta = " · ".join(part for part in (
            "model-based estimate", f"{sample} samples" if isinstance(sample, int) else "",
            interval_text, str(move.get("sampler_id") or "")) if part)
    return (
        f'<li class="sd-calc-prob-row"><span class="sd-calc-prob-name">{esc(name)}</span>'
        f'<small class="sd-calc-percent">{esc(outcome)}</small>'
        f'<small class="sd-calc-ko">{esc(move.get("ko") or "")}</small>'
        f'<small class="sd-calc-basis">{esc(estimate_meta or " · ".join(part for part in (("verified ROM turn" if method == "verified-fixed-seed" else "simulated turn" if method == "known-seed" else basis), basis) if part))}</small>'
        + (f'<small class="sd-calc-notes">{esc(notes)}</small>' if notes else "") + "</li>")


def move_card_body(card):
    """Moves are gated per entry, so an unsupported move never hides its siblings."""
    basis = ("ROM-verified" if card.get("status") == "verified-fixed-seed" else
             "model-based sampled outcomes" if card.get("status") == "estimated" else
             basis_text(card.get("evidence")))
    moves = [move for move in (card.get("moves") or []) if isinstance(move, dict)]
    rows = [move_entry_row(move, basis) for move in moves]
    if not rows:
        return '<p class="sd-calc-unavailable">No move probabilities were returned for this state.</p>'
    gated = any(move.get("status") == "unavailable" for move in moves)
    return ('<ol class="sd-calc-rows">' + "".join(rows) + "</ol>"
            + ('<p class="sd-calc-notes">A move marked Not calculated carries its own reason; '
               "the remaining entries are shown with their own evidence.</p>" if gated else ""))


def ai_card_body(card):
    rows = []
    for action in card.get("actions") or []:
        if not isinstance(action, dict):
            continue
        probability = action.get("probability") if isinstance(action.get("probability"), dict) else None
        if probability is None and type(action.get("num")) is int and type(action.get("den")) is int:
            probability = {"num": action["num"], "den": action["den"]}
        label = action.get("label") or human(action.get("id"))
        top = bool(action.get("top"))
        rows.append(
            f'<li class="sd-calc-prob-row{" sd-calc-top" if top else ""}">'
            f'<span class="sd-calc-prob-name">{esc(label)}</span>'
            f'<span class="sd-calc-prob-kind">{esc(str(action.get("kind") or ""))}</span>'
            f'<small class="sd-calc-percent">{esc("Chosen in verified ROM turn" if card.get("status") == "verified-fixed-seed" else probability_text(probability))}</small>'
            f'<small class="sd-calc-basis">{esc("top tie" if top else "")}</small></li>')
    if not rows:
        return '<p class="sd-calc-unavailable">No legal AI action probabilities were returned.</p>'
    conditioned = card.get("conditioned_on") or "entered battle state"
    return ('<ol class="sd-calc-rows">' + "".join(rows) + "</ol>"
            + f'<p class="sd-calc-conditioned">conditioned on the {esc(conditioned)}</p>')


def recommendation_card_body(card):
    label = card.get("label") or "two-turn lookahead"
    rows = []
    for action in card.get("actions") or []:
        if not isinstance(action, dict):
            continue
        score = action.get("score") if isinstance(action.get("score"), dict) else None
        rows.append(
            f'<li class="sd-calc-prob-row sd-calc-top">'
            f'<span class="sd-calc-prob-name">{esc(action.get("label") or human(action.get("id")))}</span>'
            f'<span class="sd-calc-prob-kind">{esc(str(action.get("kind") or ""))}</span>'
            f'<small class="sd-calc-percent">{esc(score_text(score) if score else "Unknown")}</small></li>')
    if not rows:
        return '<p class="sd-calc-unavailable">No legal action was available.</p>'
    turns = card.get("scored_turns")
    turns_label = f" · {int(turns)} scored turns" if isinstance(turns, int) else ""
    breakdown = [esc(str(line)) for line in (card.get("breakdown") or [])]
    return (f'<p class="sd-calc-card-label">{esc(label)}{esc(turns_label)}</p>'
            + '<ol class="sd-calc-rows">' + "".join(rows) + "</ol>"
            + ('<ul class="sd-calc-breakdown">' + "".join(f"<li>{line}</li>" for line in breakdown)
               + "</ul>" if breakdown else ""))


def calc_card_body(kind, card, moves=None, pending=False):
    if not isinstance(card, dict) or not card.get("status"):
        if pending:
            return '<p class="sd-calc-pending">Calculating…</p>'
        names = list(moves or [])[:4]
        if kind in ("player_moves", "enemy_moves") and names:
            names += ["Unknown move"] * (4 - len(names))
            return '<ol class="sd-calc-rows">' + "".join(
                f'<li class="sd-calc-unknown"><span>{esc(name)}</span><small>Not calculated</small></li>'
                for name in names) + "</ol>"
        return ('<p class="sd-calc-unavailable">Not calculated. '
                "Choose an active Pokémon and an opponent.</p>")
    status = card.get("status")
    if status == "unavailable":
        return card_unavailable_markup(card)
    if status not in ("exact", "engine-fixed-seed", "verified-fixed-seed", "estimated"):
        return card_unavailable_markup({"reason": "incomplete-state",
                                       "detail": "No verified or estimated result is available yet."})
    if status == "estimated":
        interval = card.get("interval_95") if isinstance(card.get("interval_95"), dict) else None
        samples = card.get("effective_samples")
        low = round(float(interval.get("low", 0)) * 100) if interval else None
        high = round(float(interval.get("high", 0)) * 100) if interval else None
        label = ("Model-based estimate" + (f" · {samples} samples" if isinstance(samples, int) else "")
                 + (f" · 95% interval {low}–{high}%" if low is not None and high is not None else "")
                 + (f" · {esc(card.get('sampler_id'))}" if card.get("sampler_id") else ""))
        banner = f'<p class="sd-calc-estimate">{label}</p>'
    elif status == "verified-fixed-seed":
        banner = '<p class="sd-calc-basis">Verified fixed-seed ROM result</p>'
    elif status == "engine-fixed-seed":
        banner = '<p class="sd-calc-basis">Compiled game engine · fixed seed</p>'
    elif status == "exact":
        banner = f'<p class="sd-calc-basis">{esc(basis_text(card.get("evidence")))}</p>'
    else:
        banner = ""
    if kind in ("player_moves", "enemy_moves"):
        return banner + move_card_body(card)
    if kind == "ai_actions":
        return banner + ai_card_body(card)
    if kind == "recommendation":
        uncertainty = card.get("ranking_uncertain") is True
        note = '<p class="sd-calc-estimate">Ranking remains uncertain across sampled trajectories.</p>' if uncertainty else ""
        return banner + note + recommendation_card_body(card)
    return '<p class="sd-calc-unavailable">Unknown result card.</p>'


def calc_result_card(title, kind=None, card=None, moves=None, pending=False):
    return (f'<div class="sd-calc-result-card"><strong>{esc(title)}</strong>'
            f'{calc_card_body(kind, card, moves, pending)}</div>')


def calc_service_fetch(snapshot, game_data):
    """Non-blocking lookup of the current snapshot's result; None while pending.

    The service keys results by normalized snapshot, so a stale result can never
    render here. Nothing in this path blocks or samples.
    """
    try:
        result = calc_service.cached(snapshot)
        if result is None:
            calc_service.request(snapshot, game_data)
            result = calc_service.poll(snapshot)
        return result
    except (TypeError, ValueError, KeyError):
        return {"status": "unavailable", "reason": "engine-error",
                "detail": "The battle engine could not read this battle state.",
                "missing": [], "mechanics": []}


@st.fragment(run_every=0.5)
def calc_wait_for_result(snapshot):
    """Poll while a calculation is pending, then rerun so its cards can render."""
    try:
        ready = calc_service.poll(snapshot) is not None
    except (TypeError, ValueError, KeyError):
        ready = True
    if ready:
        st.rerun(scope="app")


def calc_slot(row, data, level=None, focused=False):
    if row is None:
        return None
    species_id = row["species"]
    return {"id": row.get("id") if isinstance(row, dict) else row["id"],
            "name": species_name(species_id, data), "level": level if level is not None else row["level"],
            "types": [{"label": human(value), "color": TYPE_COLORS.get(human(value).upper(), "#657b82")}
                      for value in data.get("species", {}).get(species_id, {}).get("types", [])],
            "ability": human(row["ability"]), "nature": human(row["nature"]),
            "item_icon_url": item_icon(row["item"]) or "",
            "item_label": human(row["item"]) if row["item"] else "",
            "icon_url": species_icon(species_id), "focused": focused}


def calc_trainer_header(name, subtitle, portrait, count):
    return (f'<div class="sd-calc-trainer"><div class="sd-calc-trainer-identity">'
            f'{icon(portrait, name, "sd-sprite-sm")}'
            f'<div><strong>{esc(name)}</strong><small>{esc(subtitle)}</small></div></div>'
            f'<div class="sd-calc-trainer-balls">{party_balls(count)}</div></div>')


def enemy_profile_markup(enemy, data, level):
    species = data.get("species", {}).get(enemy.get("species"), {})
    base_stats = species.get("stats") or {}
    resolved_level = int(level) if str(level).isdigit() else None
    bars = stat_bars_markup(base_stats, resolved_level, enemy.get("nature"),
                            enemy.get("ivs") or {}, enemy.get("evs") or {}, show_unknown=True)
    move_markup = []
    for move_id in (enemy.get("moves") or [])[:4]:
        move = data.get("moves", {}).get(move_id, {})
        move_type = human(move.get("type"))
        color = TYPE_COLORS.get(move_type.upper(), "#9db3b6")
        details = [move_type]
        if move.get("power") is not None:
            details.append(f"Power {move['power'] or '—'}")
        details.append(f"Accuracy {move['accuracy'] if move.get('accuracy') else '—'}")
        meta = f'<span class="sd-type" style="--type:{color}">{esc(move_type)}</span>'
        meta += "".join(f"<span>{esc(detail)}</span>" for detail in details[1:])
        pp = f'<span class="sd-move-pp">{esc(move["pp"])} PP</span>' if move.get("pp") is not None else ""
        description = esc((move.get("description") or "").replace("\n", " "))
        description_html = f"<p>{description}</p>" if description else ""
        name = move.get("name") or human(move_id)
        move_markup.append(f'<div class="sd-move-card sd-calc-enemy-move" style="--type:{color}"><div class="sd-move-head"><strong>{esc(name)}</strong><span class="sd-move-head-right">{pp}</span></div><div class="sd-move-meta">{meta}</div>{description_html}</div>')
    move_markup.extend('<div class="sd-move-card sd-calc-enemy-move sd-calc-unknown-move" style="--type:#65747b"><div class="sd-move-head"><strong>Unknown move</strong></div></div>' for _ in range(4 - len(move_markup)))
    move_markup = f'<div class="sd-moves">{"".join(move_markup)}</div>'
    ability_id = enemy.get("ability")
    ability = human(ability_id) if ability_id else "Unknown"
    ability_description = (data.get("abilities", {}).get(ability_id)
                           or ("Description unavailable." if ability_id else ""))
    ability_description = ability_description.replace("\n", " ")
    ability_description_html = (f'<span class="sd-ability-description">{esc(ability_description)}</span>'
                                if ability_description else "")
    stats_label = "Stats" if resolved_level is not None else "Base stats"
    return (f'<div class="sd-profile-body"><div class="sd-profile-stats"><strong>{stats_label}</strong>'
            f'<div class="sd-profile">{bars}<strong>Ability</strong>'
            f'<div class="sd-ability-editor"><span class="sd-ability-trigger">{esc(ability)}</span>{ability_description_html}</div></div></div>'
            f'<div class="sd-profile-moves"><strong>Known moves</strong>{move_markup}</div></div>')


def calc_snapshot(data, party_slots, trainer_id, enemy_order, battle_state=None, level_cap_id=None):
    names = {str(row["id"]): species_name(row["species"], data) for row in party_slots if row}
    return {"party_ids": [row["id"] if row else None for row in party_slots],
            "party_names": names, "trainer_id": trainer_id,
            "enemy_order": enemy_order, "player_focus": st.session_state.get("calc_player_focus_id"),
            "enemy_focus": st.session_state.get("calc_enemy_focus"),
            "level_cap": st.session_state.get("calc_level_cap", 100),
            "level_cap_id": level_cap_id,
            "difficulty": st.session_state.get("calc_difficulty", "Normal"),
            "calc_state_version": CALC_STATE_VERSION,
            "battle_state": ({key: value for key, value in battle_state.items()
                              if key != "_preserve_unknown_hp"}
                             if isinstance(battle_state, dict) else {}),
            "item_flags": st.session_state.get("calc_item_flags", {}).copy()}


def level_cap_id_for(data, level):
    for cap in data.get("level_caps") or []:
        if isinstance(cap, dict) and _coerce_int(cap.get("level")) == level:
            return cap.get("id") if isinstance(cap.get("id"), str) else None
    return None


def level_cap_choices(data, current):
    """(levels, labels) for the ROM milestone selectbox, or (None, None) without caps."""
    caps = [cap for cap in (data.get("level_caps") or [])
            if isinstance(cap, dict) and _coerce_int(cap.get("level")) is not None]
    if not caps:
        return None, None
    labels = {}
    for cap in caps:
        labels.setdefault(_coerce_int(cap["level"]),
                          str(cap.get("label") or cap.get("id") or f"Level {cap['level']}"))
    levels = sorted(labels)
    if current not in levels:
        if current is not None and 1 <= current <= 100:
            levels = sorted({*levels, current})
            labels[current] = f"Saved level {current} (not a ROM milestone)"
        else:
            current = levels[-1]
    return levels, labels


def level_cap_widget():
    """Set a level directly, matching the editable level badge in the focused panel."""
    return st.number_input("Level cap", min_value=1, max_value=100, value=100,
                           step=1, key="calc_level_cap", persist_state="session")


def difficulty_widget():
    """Easy and Normal only: the pinned ROM has no Hard mode."""
    current = st.session_state.get("calc_difficulty")
    if current not in CALC_DIFFICULTIES:
        if current == "Hard":
            st.warning("Hard difficulty was selected, but this ROM has no Hard mode. "
                       "Choose Easy or Normal and save the config again.")
        current = "Normal"
        st.session_state["calc_difficulty"] = current
    seed_choice("calc_difficulty", CALC_DIFFICULTIES, current)
    return st.selectbox("Difficulty", list(CALC_DIFFICULTIES),
                        key="calc_difficulty", persist_state="session")


def simulate_highest_level(party_rows, level_cap, data):
    """Highest party level after the ROM's raise-to-cap routine (src/caps.c:128).

    Every unmarked, non-empty party member is raised to the cap, so with all
    members unmarked this is the cap itself. Returns None when there is nothing
    to simulate (empty party) or no milestone is selected.
    """
    cap = _coerce_int(level_cap)
    rows = [dict(row) for row in (party_rows or [])]
    if not rows or cap is None:
        return None
    try:
        import battle_engine
        members = battle_engine.simulate_party_level_cap(rows, cap, data)
    except (ImportError, AttributeError, TypeError, ValueError):
        # ponytail: no engine, so use the routine's documented all-unmarked result
        return cap
    levels = [_coerce_int(member.get("level")) for member in members if isinstance(member, dict)]
    levels = [level for level in levels if level is not None]
    return max(levels) if levels else cap


def calc_trainer_items(trainer):
    items = {}
    for item_id in (trainer or {}).get("items") or []:
        if isinstance(item_id, str) and item_id:
            items[item_id] = items.get(item_id, 0) + 1
    return items


def history_from_log(rows):
    """Group ordered observed-turn rows into the contract's history.turns entries.

    move/switch rows become the side's action; every other row is an event. An
    event the engine cannot represent exactly makes seed-based results unavailable.
    """
    turns = []
    by_turn = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        turn = _coerce_int(row.get("turn"))
        if turn is None or turn < 1:
            continue
        side = str(row.get("side") or "").strip().lower()
        kind = str(row.get("kind") or "").strip().lower()
        if not kind:
            continue
        detail = row.get("id")
        detail = str(detail).strip() if detail not in (None, "") else None
        value = _coerce_int(row.get("value"))
        entry = by_turn.get(turn)
        if entry is None:
            entry = {"player_action": None, "enemy_action": None, "events": []}
            by_turn[turn] = entry
            turns.append(entry)
        action_key = "player_action" if side == "player" else "enemy_action" if side == "enemy" else None
        if action_key and kind == "move" and detail:
            entry[action_key] = {"kind": "move", "move_id": detail, "target_index": None}
        elif action_key and kind == "switch" and value is not None and value >= 0:
            entry[action_key] = {"kind": "switch", "slot": value}
        elif kind:
            event = {"kind": kind}
            if side in ("player", "enemy"):
                event["side"] = side
            if detail:
                event["id"] = detail
            if value is not None:
                event["value"] = value
            entry["events"].append(event)
    return turns


def history_to_log_rows(turns):
    """Flatten the saved observed-turn structure for the manual history editor."""
    rows = []
    for turn_no, turn in enumerate(turns or [], 1):
        if not isinstance(turn, dict):
            continue
        for side, key in (("player", "player_action"), ("enemy", "enemy_action")):
            action = turn.get(key)
            if not isinstance(action, dict):
                continue
            kind = action.get("kind")
            rows.append({"turn": turn_no, "side": side, "kind": kind or "move",
                         "id": action.get("move_id") or "",
                         "value": action.get("slot") if kind == "switch" else None})
        for event in turn.get("events") or []:
            if not isinstance(event, dict):
                continue
            rows.append({"turn": turn_no, "side": event.get("side") or "",
                         "kind": event.get("kind") or "event",
                         "id": event.get("id") or "", "value": event.get("value")})
    return rows


def set_rng_stream(state, stream, raw):
    rng = state.setdefault("rng", {})
    if not isinstance(rng, dict):
        rng = {"main": parse_rng_words(rng), "secondary": None}
        state["rng"] = rng
    rng[stream] = parse_rng_words(raw)


def calc_save_config(connect, payload, name, selected_id=None):
    with closing(connect()) as db:
        if selected_id:
            updated = db.execute("UPDATE team_configs SET name=?, settings=? WHERE id=?",
                                 (name, json.dumps(payload), selected_id))
            if not updated.rowcount:
                raise ValueError("That saved config no longer exists.")
            saved_id = selected_id
        else:
            saved_id = db.execute("INSERT INTO team_configs(name,settings) VALUES(?,?)",
                                  (name, json.dumps(payload))).lastrowid
        db.commit()
    return saved_id


@st.dialog("Delete team config")
def calc_delete_dialog(connect, config_id, config_name):
    st.write(f"Delete {config_name}?")
    if st.button("Delete config", type="primary"):
        with closing(connect()) as db:
            db.execute("DELETE FROM team_configs WHERE id=?", (config_id,))
            db.commit()
        if st.session_state.get("calc_loaded_config_id") == config_id:
            st.session_state["calc_loaded_config_id"] = None
        st.session_state["calc_last_load_notice"] = f"Deleted config {config_name}."
        st.rerun()


def calc_validate_state(payload):
    """Validate the saved battle state. Older configs simply have none."""
    version = payload.get("calc_state_version", 1)
    if type(version) is not int or version not in (1, 2, CALC_STATE_VERSION):
        raise ValueError("Saved Calc state version is invalid.")
    if version == 1 or "battle_state" not in payload:
        return {}
    state = payload.get("battle_state")
    if state is None:
        return {}
    if not isinstance(state, dict):
        raise ValueError("Saved battle state is invalid.")
    for key in ("player_active_slot", "enemy_active_index"):
        value = state.get(key)
        if value is not None and (_coerce_int(value) is None or _coerce_int(value) < 0):
            raise ValueError("Saved battle state is invalid.")
    if state.get("planned_action") is not None and calc_planned_action(state) is None:
        raise ValueError("Saved planned action is invalid.")
    rng = state.get("rng")
    if rng is not None:
        if isinstance(rng, dict):
            if any(rng.get(stream) is not None and parse_rng_words(rng.get(stream)) is None
                   for stream in ("main", "secondary")):
                raise ValueError("Saved RNG state is invalid.")
        elif parse_rng_words(rng) is None:
            raise ValueError("Saved RNG state is invalid.")
    if state.get("state_origin") not in (None, "battle-start", "manual-scenario"):
        raise ValueError("Saved battle state is invalid.")
    for side in ("player", "enemy"):
        if state.get(side) is not None and not isinstance(state.get(side), dict):
            raise ValueError("Saved battle state is invalid.")
    history = state.get("history")
    if history is not None and (not isinstance(history, dict)
                                or not isinstance(history.get("turns", []), list)):
        raise ValueError("Saved battle state is invalid.")
    if state.get("field") is not None and not isinstance(state.get("field"), dict):
        raise ValueError("Saved battle state is invalid.")
    return state


def calc_state_for_trainer(state, trainer_id):
    """Discard turn data when the selected opponent changes."""
    if isinstance(state, dict) and state.get("_trainer_id") == trainer_id:
        return state
    return {"_trainer_id": trainer_id}


def calc_state_for_focus(state, party_slots, player_focus, enemy_focus):
    """Keep the battle active pair aligned with the two focused battlers."""
    player_slot = next((slot for slot, row in enumerate(party_slots)
                        if row and row.get("id") == player_focus), None)
    enemy_index = enemy_focus if type(enemy_focus) is int and enemy_focus >= 0 else None
    if (state.get("player_active_slot"), state.get("enemy_active_index")) != (player_slot, enemy_index):
        state["planned_action"] = None
    state["player_active_slot"] = player_slot
    state["enemy_active_index"] = enemy_index
    return state


def calc_default_max_hp(data, species, level, ivs, evs):
    """Starting HP from the displayed build; missing HP IV/EV use the profile's zero default."""
    base = ((data.get("species", {}).get(species) or {}).get("stats") or {}).get("hp")
    level = _coerce_int(level)
    if type(base) is not int or level is None or not 1 <= level <= 100:
        return None
    return calculate_stats({"hp": base}, level, None, ivs, evs).get("hp")


def calc_seed_hp(state, side, key, default_max):
    """Seed fresh scenarios at full HP while preserving saved unknown values."""
    entries = state.setdefault(side, {})
    entry = entries.setdefault(str(key), {})
    if default_max is not None:
        current = _coerce_int(entry.get("hp"))
        entry["max_hp"] = default_max
        if current is not None:
            entry["hp"] = min(max(0, current), default_max)
        elif ("hp" not in entry and state.get("state_origin") == "battle-start"
              and not state.get("_preserve_unknown_hp")):
            entry["hp"] = default_max
    return entry


def calc_start_battle_at_full_hp(state, player_party, enemy_party, data, player_levels, enemy_levels):
    """Explicitly establish a battle-start scenario with every known member at full HP."""
    state.setdefault("player", {})
    state.setdefault("enemy", {})
    for index, row in enumerate(player_party or []):
        if not row:
            continue
        member = state["player"].setdefault(str(row["id"]), {})
        level = _coerce_int(player_levels[index] if index < len(player_levels) else row.get("level"))
        maximum = calc_default_max_hp(data, row.get("species"), level,
                                      json_dict(row.get("ivs")), json_dict(row.get("evs")))
        if maximum is not None:
            member.update(max_hp=maximum, hp=maximum, status="none", stat_stages={})
        else:
            member.pop("hp", None)
            member.pop("max_hp", None)
            member.update(status="none", stat_stages={})
        member.pop("pp", None)
    for index, member_row in enumerate(enemy_party or []):
        if not isinstance(member_row, dict):
            continue
        entry = state["enemy"].setdefault(str(index), {})
        level = _coerce_int(enemy_levels[index] if index < len(enemy_levels)
                            else member_row.get("raw_level", member_row.get("level")))
        maximum = calc_default_max_hp(data, member_row.get("species"), level,
                                      member_row.get("ivs"), member_row.get("evs"))
        if maximum is not None:
            entry.update(max_hp=maximum, hp=maximum, status="none", stat_stages={})
        else:
            entry.pop("hp", None)
            entry.pop("max_hp", None)
            entry.update(status="none", stat_stages={})
        entry.pop("pp", None)
    state.update(state_origin="battle-start", planned_action=None,
                 history={"turns": []}, field={})
    state["rng"] = {"main": None, "secondary": None}
    return state


def calc_hp_badge(entry, side, key, state=None):
    """Compact current/max HP editor beside the focused Pokémon's name."""
    current = _coerce_int(entry.get("hp"))
    maximum = _coerce_int(entry.get("max_hp"))
    result = hp_badge(current=current, maximum=maximum, key=f"calc_{side}_hp_badge_{key}")
    changed = result.changed
    token_key = f"calc_hp_change_token_{side}_{key}"
    if changed and changed.get("token") != st.session_state.get(token_key):
        st.session_state[token_key] = changed["token"]
        value = changed.get("value")
        if type(value) is int and maximum is not None and 0 <= value <= maximum:
            entry["hp"] = value
            if isinstance(state, dict):
                state["state_origin"] = "manual-scenario"
            st.rerun()


def calc_load_config(db, record, data, apply_party_slots, trainer_rows):
    payload = json.loads(record["settings"])
    if not isinstance(payload, dict):
        raise ValueError("Saved config is invalid.")
    requested = payload.get("party_ids")
    if not isinstance(requested, list) or len(requested) > 6 or any(
        value is not None and (type(value) is not int or value <= 0) for value in requested
    ) or len([value for value in requested if value is not None]) != len(set(
        value for value in requested if value is not None)):
        raise ValueError("Saved party slots are invalid.")
    requested = requested + [None] * (6 - len(requested))
    level_cap = payload.get("level_cap", 100)
    difficulty = payload.get("difficulty", "Normal")
    if difficulty == "Hard":
        raise ValueError("This config was saved as Hard, but this ROM has no Hard difficulty. "
                         "Choose Easy or Normal and save the config again.")
    if type(level_cap) is not int or not 1 <= level_cap <= 100 or difficulty not in CALC_DIFFICULTIES:
        raise ValueError("Saved Calc settings are invalid.")
    state = calc_validate_state(payload)
    had_saved_state = bool(state)
    # Older configs may intentionally have no recorded HP; do not fill it from
    # current profile data when those configs are opened.
    state["_preserve_unknown_hp"] = True
    flags = payload.get("item_flags") or {}
    if not isinstance(flags, dict) or any(
        not isinstance(key, str) or not key.startswith(("calc_item_player_", "calc_item_enemy_"))
        or type(value) is not bool for key, value in flags.items()
    ):
        raise ValueError("Saved item switches are invalid.")
    ids = {row["trainer"]["id"]: row for row in trainer_rows}
    trainer_id = payload.get("trainer_id")
    if trainer_id not in ids:
        raise ValueError("The saved trainer is no longer available.")
    count = len(ids[trainer_id]["trainer"].get("party", []))
    order = payload.get("enemy_order")
    if not isinstance(order, list) or sorted(order) != list(range(count)):
        raise ValueError("Saved opponent slots are invalid.")
    enemy_focus = payload.get("enemy_focus")
    if enemy_focus is not None and (type(enemy_focus) is not int or enemy_focus not in range(count)):
        raise ValueError("Saved opponent focus is invalid.")
    player_focus = payload.get("player_focus")
    if player_focus is not None and (type(player_focus) is not int or player_focus <= 0):
        raise ValueError("Saved player focus is invalid.")
    names = payload.get("party_names") or {}
    if not isinstance(names, dict):
        names = {}
    available = {row["id"]: row for row in db.execute("SELECT id,status FROM owned")}
    slots = [value if value in available and available[value]["status"] == "alive" else None
             for value in requested]
    missing = [str(names.get(str(value), f"Pokémon #{value}"))
               for value, kept in zip(requested, slots) if value is not None and kept is None]
    apply_party_slots(db, slots)
    st.session_state["shared_trainer_id"] = trainer_id
    st.session_state["sd_chapter"] = ids[trainer_id]["group"]
    st.session_state["sd_trainer"] = trainer_id
    st.session_state["calc_player_focus_id"] = player_focus
    st.session_state["calc_enemy_focus"] = enemy_focus
    st.session_state["calc_level_cap"] = level_cap
    st.session_state["calc_difficulty"] = difficulty
    st.session_state["calc_item_flags"] = flags
    for key in list(st.session_state):
        if key.startswith("calc_item_") and key not in flags and key != "calc_item_flags":
            del st.session_state[key]
    for key, value in flags.items():
        st.session_state[key] = bool(value)
    for key in list(st.session_state):
        if key.startswith("calc_state_") and key != "calc_battle_state":
            del st.session_state[key]
        elif key.startswith(("calc_battle_", "calc_rng_", "calc_status_", "calc_field_",
                            "calc_manual_hp_", "calc_manual_pp_", "calc_history_")):
            del st.session_state[key]
    if state:
        state["_trainer_id"] = trainer_id
    st.session_state["calc_battle_state"] = state
    st.session_state.setdefault("calc_enemy_orders", {})[trainer_id] = order
    prior_version = payload.get("calc_state_version", 1)
    note = ("" if prior_version == CALC_STATE_VERSION
            else " Older saved battle values were preserved; unknown values remain unknown."
            if had_saved_state else " This older config has no saved battle state.")
    return "Loaded config. " + ("Skipped: " + ", ".join(missing) + "." if missing else "") + note


def render_calc(db, data, save_entry, resolve_level, connect, move_box_slot, apply_party_slots):
    rows = db.execute("SELECT * FROM owned ORDER BY id").fetchall()
    party_slots = [None] * 6
    for row in rows:
        if row["in_party"] and row["party_slot"] is not None and 0 <= row["party_slot"] < 6:
            party_slots[row["party_slot"]] = row
    party = [row for row in party_slots if row]
    trainer_rows = build_trainer_index(data.get("trainers", []))
    with st.container(key="calc_screen"):
        result_area = st.container(key="calc_results")
        grid_area = st.container(key="calc_grid")

        with grid_area:
            own_col, own_focus_col, enemy_focus_col, enemy_col = st.columns([1, 2, 2, 1], gap="small")
            with own_col:
                player_trainer_area = st.container(key="calc_player_trainer_panel")
            with own_focus_col:
                player_panel_area = st.container(key="calc_player_panel")
            with enemy_col:
                enemy_roster_area = st.container()
                trainer_settings_area = st.container(key="calc_trainer_settings")

        configs = db.execute("SELECT id,name,settings FROM team_configs ORDER BY name COLLATE NOCASE").fetchall()
        by_config = {row["id"]: row for row in configs}
        loaded_id = st.session_state.get("calc_loaded_config_id")
        loaded_name = by_config[loaded_id]["name"] if loaded_id in by_config else ""
        with player_trainer_area:
            config_result = calc_config_picker(
                configs=[{"id": row["id"], "name": row["name"]} for row in configs],
                key="calc-config-picker")
        notice = st.session_state.pop("calc_last_load_notice", None)
        if notice:
            st.toast(notice)
        load_event = config_result.load
        if load_event and load_event.get("token") != st.session_state.get("calc_config_load_token"):
            st.session_state["calc_config_load_token"] = load_event["token"]
            record = by_config.get(load_event.get("id"))
            if record:
                try:
                    st.session_state["calc_last_load_notice"] = calc_load_config(
                        db, record, data, apply_party_slots, trainer_rows)
                    st.session_state["calc_loaded_config_id"] = record["id"]
                    st.rerun()
                except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    st.error(f"Could not load config: {exc}")
        delete_event = config_result.delete
        if delete_event and delete_event.get("token") != st.session_state.get("calc_config_delete_token"):
            st.session_state["calc_config_delete_token"] = delete_event["token"]
            record = by_config.get(delete_event.get("id"))
            if record:
                calc_delete_dialog(connect, record["id"], record["name"])
        with trainer_settings_area:
            st.markdown("**Trainer selector**")
            level_cap = level_cap_widget()
            if level_cap_id_for(data, level_cap) is None:
                st.caption(f"Level {level_cap} is not a ROM progression milestone; compiled battle calculations are unavailable at this cap.")
            difficulty_widget()
            simulated_level = simulate_highest_level(party, level_cap, data)
            trainer_id, trainer, trainer_group, highest_level = trainer_selection(
                trainer_rows, party, resolve_level, stacked=True, highest_level=simulated_level)
        previous_state = st.session_state.get("calc_battle_state")
        trainer_changed = not isinstance(previous_state, dict) or previous_state.get("_trainer_id") != trainer_id
        if trainer_changed:
            st.session_state.pop("calc_state_planned_action", None)
            for key in list(st.session_state):
                if key.startswith(("calc_battle_", "calc_rng_", "calc_status_", "calc_field_",
                                   "calc_manual_hp_", "calc_manual_pp_", "calc_history_")):
                    del st.session_state[key]
        battle_state = calc_state_for_trainer(previous_state, trainer_id)
        st.session_state["calc_battle_state"] = battle_state
        save_event = config_result.save
        if save_event and save_event.get("token") != st.session_state.get("calc_config_save_token"):
            st.session_state["calc_config_save_token"] = save_event["token"]
            name = str(save_event.get("name") or "").strip()
            if not name:
                st.error("Enter a config name.")
            elif not trainer:
                st.error("Choose a trainer before saving a config.")
            else:
                try:
                    enemy_order = st.session_state.get("calc_enemy_orders", {}).get(
                        trainer_id, list(range(len(trainer.get("party", [])))))
                    selected_id = loaded_id if name == loaded_name else None
                    saved_id = calc_save_config(
                        connect, calc_snapshot(data, party_slots, trainer_id, enemy_order,
                                               st.session_state.get("calc_battle_state"),
                                               level_cap_id_for(data, st.session_state.get("calc_level_cap"))),
                        name, selected_id)
                    st.session_state["calc_loaded_config_id"] = saved_id
                    st.session_state["calc_last_load_notice"] = f"Saved config {name}."
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("That config name is already used.")
                except ValueError as exc:
                    st.error(str(exc))
        if not trainer:
            st.info("Choose a trainer to open Calc.")
            return

        trainer_name = trainer.get("name") or human(trainer_id)
        enemies = trainer.get("party", [])
        orders = st.session_state.setdefault("calc_enemy_orders", {})
        order = orders.get(trainer_id)
        if sorted(order or []) != list(range(len(enemies))):
            order = list(range(len(enemies)))
            orders[trainer_id] = order
        player_focus = st.session_state.get("calc_player_focus_id")
        if player_focus not in [row["id"] for row in party]:
            player_focus = party[0]["id"] if party else None
            st.session_state["calc_player_focus_id"] = player_focus
        selected = next((row for row in party if row["id"] == player_focus), None)
        enemy_focus = st.session_state.get("calc_enemy_focus")
        if enemy_focus not in range(len(enemies)):
            enemy_focus = order[0] if order else None
            st.session_state["calc_enemy_focus"] = enemy_focus
        enemy = enemies[enemy_focus] if enemy_focus is not None else None
        enemy_level = resolve_level(enemy.get("raw_level", enemy.get("level")), highest_level) if enemy else "?"
        enemy_levels = [resolve_level(member.get("raw_level", member.get("level")), highest_level)
                        for member in enemies]
        level_cap_id = level_cap_id_for(data, st.session_state.get("calc_level_cap"))
        battle_state = calc_state_for_focus(battle_state, party_slots, player_focus, enemy_focus)
        if not isinstance(battle_state.get("rng"), dict):
            battle_state["rng"] = {"main": parse_rng_words(battle_state.get("rng")),
                                    "secondary": None}
        battle_state.setdefault("state_origin", "battle-start")
        for member_row in party:
            calc_seed_hp(
                battle_state, "player", member_row["id"],
                calc_default_max_hp(data, member_row["species"],
                                    level_cap if level_cap_id else member_row["level"],
                                    json_dict(member_row["ivs"]), json_dict(member_row["evs"])))
        for index, member in enumerate(enemies):
            battle_level = _coerce_int(enemy_levels[index])
            if battle_level is not None and st.session_state.get("calc_difficulty") == "Easy" and trainer.get("is_boss"):
                battle_level = max(1, battle_level - 2)
            calc_seed_hp(battle_state, "enemy", index,
                         calc_default_max_hp(data, member.get("species"), battle_level,
                                             member.get("ivs"), member.get("evs")))

        with trainer_settings_area:
            st.markdown("**Battle state**")
            player_active_options = [slot for slot, row in enumerate(party_slots) if row]
            enemy_active_options = list(range(len(enemies)))
            player_active = battle_state.get("player_active_slot")
            if player_active not in player_active_options:
                player_active = None
            enemy_active = battle_state.get("enemy_active_index")
            if enemy_active not in enemy_active_options:
                enemy_active = None
            player_active = st.selectbox(
                "Your active Pokémon", player_active_options,
                index=player_active_options.index(player_active) if player_active is not None else None,
                key="calc_battle_player_active", placeholder="Choose active Pokémon",
                format_func=lambda slot: species_name(party_slots[slot]["species"], data))
            enemy_active = st.selectbox(
                "Opponent active Pokémon", enemy_active_options,
                index=enemy_active_options.index(enemy_active) if enemy_active is not None else None,
                key="calc_battle_enemy_active", placeholder="Choose active Pokémon",
                format_func=lambda index: species_name(enemies[index].get("species"), data))
            if (player_active, enemy_active) != (battle_state.get("player_active_slot"),
                                                  battle_state.get("enemy_active_index")):
                battle_state["planned_action"] = None
                st.session_state.pop("calc_state_planned_action", None)
            battle_state["player_active_slot"] = player_active
            battle_state["enemy_active_index"] = enemy_active
            player_active_row = party_slots[player_active] if player_active is not None else None
            player_active_entry = (battle_state.get("player") or {}).get(str(player_active_row["id"]), {}) if player_active_row else {}
            enemy_active_entry = (battle_state.get("enemy") or {}).get(str(enemy_active), {}) if enemy_active is not None else {}
            if player_active_row and enemy_active is not None:
                if st.button("Start battle at full HP", key="calc_start_full_hp",
                             help="Explicitly set every party member to full HP and record this as the battle start."):
                    player_hp_levels = [level_cap if level_cap_id else row.get("level") if row else None
                                        for row in party_slots]
                    enemy_start_levels = [
                        max(1, _coerce_int(level) - 2)
                        if _coerce_int(level) is not None and st.session_state.get("calc_difficulty") == "Easy"
                        and trainer.get("is_boss") else level for level in enemy_levels]
                    calc_start_battle_at_full_hp(battle_state, party_slots, enemies, data,
                                                 player_hp_levels, enemy_start_levels)
                    for key in list(st.session_state):
                        if key.startswith(("calc_status_", "calc_manual_hp_", "calc_manual_pp_",
                                           "calc_rng_", "calc_field_")):
                            del st.session_state[key]
                    st.rerun()
                with st.expander("Manual battle details and observed turns"):
                    status_options = [(None, "Unknown"), ("none", "No status"),
                                      ("STATUS1_SLEEP", "Sleep"), ("STATUS1_POISON", "Poison"),
                                      ("STATUS1_BURN", "Burn"), ("STATUS1_FREEZE", "Freeze"),
                                      ("STATUS1_PARALYSIS", "Paralysis"), ("STATUS1_TOXIC_POISON", "Toxic")]
                    player_status = st.selectbox(
                        "Your active status", status_options,
                        index=next((i for i, option in enumerate(status_options)
                                    if option[0] == player_active_entry.get("status")), 0),
                        key=f"calc_status_player_{player_active_row['id']}", format_func=lambda item: item[1])
                    enemy_status = st.selectbox(
                        "Opponent active status", status_options,
                        index=next((i for i, option in enumerate(status_options)
                                    if option[0] == enemy_active_entry.get("status")), 0),
                        key=f"calc_status_enemy_{trainer_id}_{enemy_active}", format_func=lambda item: item[1])
                    for side, entry, status_choice in (("player", player_active_entry, player_status),
                                                       ("enemy", enemy_active_entry, enemy_status)):
                        old_status = entry.get("status")
                        if status_choice[0] is not None:
                            entry["status"] = status_choice[0]
                        else:
                            entry.pop("status", None)
                        if old_status != status_choice[0]:
                            battle_state["state_origin"] = "manual-scenario"
                    st.caption("Current HP, PP, status, and RNG are entered state. Blank values stay unknown.")
                    for side, entry, row_key in (("player", player_active_entry, player_active_row["id"]),
                                                  ("enemy", enemy_active_entry, f"{trainer_id}_{enemy_active}")):
                        build = player_active_row if side == "player" else enemies[enemy_active]
                        state_map = battle_state.setdefault(side, {})
                        slot_key = str(row_key) if side == "player" else str(enemy_active)
                        state_entry = state_map.setdefault(slot_key, entry)
                        current_hp = _coerce_int(state_entry.get("hp"))
                        hp_text = st.text_input(
                            f"{side.title()} active current HP", value=str(current_hp) if current_hp is not None else "",
                            key=f"calc_manual_hp_{side}_{slot_key}",
                            help="Leave blank when current HP is unknown.")
                        hp_value = _coerce_int(hp_text)
                        maximum = _coerce_int(state_entry.get("max_hp"))
                        if hp_text.strip() and (hp_value is None or hp_value < 0
                                                or (maximum is not None and hp_value > maximum)):
                            st.caption("Current HP must be a whole number within the displayed maximum.")
                        elif not hp_text.strip():
                            if "hp" in state_entry:
                                state_entry.pop("hp", None)
                                battle_state["state_origin"] = "manual-scenario"
                        else:
                            if current_hp != hp_value:
                                battle_state["state_origin"] = "manual-scenario"
                            state_entry["hp"] = hp_value
                        moves = known_moves(build, data) if side == "player" else list(build.get("moves") or [])[:4]
                        for move_id in moves[:4]:
                            current_pp = _coerce_int((state_entry.get("pp") or {}).get(move_id))
                            pp_text = st.text_input(
                                f"{side.title()} PP · {human(move_id)}",
                                value=str(current_pp) if current_pp is not None else "",
                                key=f"calc_manual_pp_{side}_{slot_key}_{move_id}",
                                help="Leave blank when current PP is unknown.")
                            pp = _coerce_int(pp_text)
                            if pp_text.strip() and (pp is None or not 0 <= pp <= 999):
                                st.caption(f"{human(move_id)} PP must be an integer from 0 to 999.")
                            elif pp is None:
                                state_entry.get("pp", {}).pop(move_id, None)
                            else:
                                prior_pp = (state_entry.get("pp") or {}).get(move_id)
                                state_entry.setdefault("pp", {})[move_id] = pp
                                if prior_pp != pp:
                                    battle_state["state_origin"] = "manual-scenario"
                    rng = battle_state.setdefault("rng", {"main": None, "secondary": None})
                    for stream, label in (("main", "Main RNG state"), ("secondary", "Secondary RNG state")):
                        current = parse_rng_words(rng.get(stream))
                        raw = st.text_input(label, value=" ".join(map(str, current)) if current else "",
                                            key=f"calc_rng_{stream}",
                                            help="Enter four unsigned 32-bit words, or leave blank when unknown.")
                        previous_words = parse_rng_words(rng.get(stream))
                        set_rng_stream(battle_state, stream, raw)
                        if parse_rng_words(raw) != previous_words:
                            battle_state["state_origin"] = "manual-scenario"
                        if raw.strip() and parse_rng_words(raw) is None:
                            st.caption(f"{label} is invalid and will be treated as unknown.")
                    field = battle_state.setdefault("field", {})
                    weather_options = [None, "WEATHER_SUN", "WEATHER_RAIN", "WEATHER_SANDSTORM",
                                       "WEATHER_HAIL", "WEATHER_SNOW"]
                    old_weather = field.get("weather")
                    weather = st.selectbox(
                        "Weather", weather_options,
                        index=weather_options.index(field.get("weather"))
                        if field.get("weather") in weather_options else 0,
                        key="calc_field_weather", format_func=lambda item: "Unknown" if item is None else human(item))
                    if weather != old_weather:
                        battle_state["state_origin"] = "manual-scenario"
                    field["weather"] = weather
                    field_options = [(None, "Unknown"), (True, "Trick Room")]
                    trick_room = st.selectbox(
                        "Trick Room", field_options,
                        index=next((i for i, option in enumerate(field_options)
                                    if option[0] == field.get("trick_room")), 0),
                        key="calc_field_trick_room", format_func=lambda item: item[1])
                    if field.get("trick_room") != trick_room[0]:
                        battle_state["state_origin"] = "manual-scenario"
                    field["trick_room"] = trick_room[0]
                    for field_key, label, options in (
                            ("player_screens", "Your screens", ["SCREEN_REFLECT", "SCREEN_LIGHT_SCREEN", "SCREEN_AURORA_VEIL"]),
                            ("enemy_screens", "Opponent screens", ["SCREEN_REFLECT", "SCREEN_LIGHT_SCREEN", "SCREEN_AURORA_VEIL"]),
                            ("player_hazards", "Hazards on your side", ["HAZARD_SPIKES", "HAZARD_TOXIC_SPIKES", "HAZARD_STEALTH_ROCK", "HAZARD_STICKY_WEB"]),
                            ("enemy_hazards", "Hazards on opponent side", ["HAZARD_SPIKES", "HAZARD_TOXIC_SPIKES", "HAZARD_STEALTH_ROCK", "HAZARD_STICKY_WEB"])):
                        choices = field.get(field_key) if isinstance(field.get(field_key), list) else []
                        selected_field = st.multiselect(
                            label, options, default=[item for item in choices if item in options],
                            key=f"calc_field_{field_key}", format_func=human)
                        if selected_field != choices:
                            battle_state["state_origin"] = "manual-scenario"
                        field[field_key] = selected_field
                    st.markdown("**Observed history**")
                    log_turn = st.number_input("Turn", min_value=1, value=1, step=1,
                                               key="calc_history_turn")
                    log_side = st.selectbox("Side", ["player", "enemy"], key="calc_history_side")
                    log_kind = st.selectbox("Event", ["move", "switch", "damage", "status",
                                                       "item", "ability", "field"],
                                            key="calc_history_kind")
                    log_id = st.text_input("Move or event ID", key="calc_history_id")
                    log_value_text = st.text_input("Value or switch slot", key="calc_history_value",
                                                   help="Optional; switch entries need a target slot.")
                    log_value = _coerce_int(log_value_text)
                    history_turns = calc_history(battle_state)
                    if st.button("Add observed event", key="calc_history_add"):
                        rows = history_to_log_rows(history_turns)
                        rows.append({"turn": int(log_turn), "side": log_side, "kind": log_kind,
                                     "id": log_id.strip(), "value": log_value})
                        battle_state["history"] = {"turns": history_from_log(rows)}
                        if battle_state.get("state_origin") != "battle-start":
                            battle_state["state_origin"] = "manual-scenario"
                        st.rerun()
                    if history_turns:
                        st.caption(f"{len(history_turns)} observed turn(s) recorded.")
                        if st.button("Clear observed history", key="calc_history_clear"):
                            battle_state["history"] = {"turns": []}
                            st.rerun()
            if player_active_row:
                moves = known_moves(player_active_row, data)[:4] if player_active_row else []
                choices = [None] + [("move", move) for move in moves]
                choices += [("switch", slot) for slot, row in enumerate(party_slots)
                            if row and slot != player_active]
                planned = calc_planned_action(battle_state)
                current_choice = ((planned["kind"], planned.get("move_id", planned.get("slot")))
                                  if planned else None)
                if current_choice not in choices:
                    current_choice = None
                seed_choice("calc_state_planned_action", choices, current_choice)
                def action_label(value):
                    if value is None:
                        return "Choose an action"
                    if value[0] == "move":
                        return data.get("moves", {}).get(value[1], {}).get("name") or human(value[1])
                    return f"Switch to {species_name(party_slots[value[1]]['species'], data)}"

                choice = st.selectbox(
                    "Planned action", choices, index=choices.index(current_choice),
                    key="calc_state_planned_action", format_func=action_label,
                    help="Enemy move probabilities are conditioned on this action.")
                battle_state["planned_action"] = (
                    {"kind": "move", "move_id": choice[1], "target_index": enemy_active}
                    if choice and choice[0] == "move" else
                    {"kind": "switch", "slot": choice[1]} if choice else None)

        item_flags = st.session_state.get("calc_item_flags")
        snapshot = build_calc_snapshot(
            data, party_slots, enemies, battle_state, trainer_id=trainer_id,
            difficulty=st.session_state.get("calc_difficulty") or "Normal",
            level_cap_id=level_cap_id, player_item_flags=item_flags, enemy_item_flags=item_flags,
            enemy_items=calc_trainer_items(trainer), ai_flags=trainer.get("ai_flags"),
            enemy_levels=enemy_levels)
        result = calc_service_fetch(snapshot, data) if snapshot else None
        pending = snapshot is not None and result is None
        with result_area:
            if snapshot and snapshot.get("state_origin") == "battle-start":
                st.caption("ROM prediction starts from your explicit full HP battle setup and recorded turns.")
            elif snapshot:
                st.caption("Manual scenario: results use only the battle state you entered.")
            else:
                st.caption("Choose both active Pokémon to calculate a battle result.")
            names = {"player_moves": [], "enemy_moves": []}
            active_slot = _coerce_int(battle_state.get("player_active_slot"))
            active_player = (party_slots[active_slot]
                             if active_slot is not None and 0 <= active_slot < 6 else None)
            if active_player:
                names["player_moves"] = [data.get("moves", {}).get(move_id, {}).get("name") or human(move_id)
                                         for move_id in known_moves(active_player, data)]
            active_enemy = _coerce_int(battle_state.get("enemy_active_index"))
            if active_enemy is not None and 0 <= active_enemy < len(enemies):
                names["enemy_moves"] = [data.get("moves", {}).get(move_id, {}).get("name") or human(move_id)
                                        for move_id in (enemies[active_enemy].get("moves") or [])[:4]]
            for column, (kind, title) in zip(st.columns(4, gap="small"), CALC_CARDS):
                with column:
                    st.html(calc_result_card(title, kind, card_result(result, kind),
                                             names.get(kind), pending))
            if pending:
                calc_wait_for_result(snapshot)

        with grid_area:
            with own_col:
                with player_trainer_area:
                    st.html(calc_trainer_header("You", "Player", trainer_icon("brendan"), len(party)))
                own_slots = [calc_slot(row, data, focused=bool(row and row["id"] == player_focus))
                             for row in party_slots]
                own_options = [{"value": row["id"],
                                "label": f"{species_name(row['species'], data)} · Lv {row['level'] or '?'} · #{row['id']}",
                                "icon_url": species_icon(row["species"])}
                               for row in rows if row["status"] == "alive"]
                own_result = calc_roster(slots=own_slots, options=own_options, key="calc-own-roster")
                focus_event = own_result.focus
                if focus_event and focus_event.get("token") != st.session_state.get("calc_own_focus_token"):
                    st.session_state["calc_own_focus_token"] = focus_event["token"]
                    slot = focus_event.get("slot")
                    if isinstance(slot, int) and 0 <= slot < 6 and party_slots[slot]:
                        st.session_state["calc_player_focus_id"] = party_slots[slot]["id"]
                        st.rerun()
                replace = own_result.replace
                if replace and replace.get("token") != st.session_state.get("calc_own_replace_token"):
                    st.session_state["calc_own_replace_token"] = replace["token"]
                    slot, value = replace.get("slot"), replace.get("value")
                    if isinstance(slot, int) and 0 <= slot < 6 and value in {option["value"] for option in own_options}:
                        next_ids = [row["id"] if row else None for row in party_slots]
                        prior = next_ids.index(value) if value in next_ids else None
                        displaced = next_ids[slot]
                        next_ids[slot] = value
                        if prior is not None and prior != slot:
                            next_ids[prior] = displaced
                        try:
                            apply_party_slots(db, next_ids)
                            if player_focus == displaced:
                                st.session_state["calc_player_focus_id"] = value
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))
                with st.container(key="pc-box-card"):
                    box_no = st.session_state.setdefault("pc_box_number", 1)
                    box_grid([row for row in rows if not row["in_party"] and row["box_no"] == box_no],
                             None, data, box_no, db, move_box_slot, select_on_click=False, compact=True)
                    if st.button("Add Pokemon", key="add_box"):
                        st.session_state["new_pokemon_box_target"] = box_no
                        st.session_state["new_pokemon_dialog_id"] = st.session_state.get("new_pokemon_dialog_id", 0) + 1
                        edit_pokemon(connect, data, save_entry, default_box=box_no)
            with own_focus_col:
                with player_panel_area:
                    st.html('<div class="sd-calc-panel-heading">Your Pokémon</div>')
                    if selected:
                        with st.container(horizontal=True, wrap=False, vertical_alignment="center", key="calc_player_heading"):
                            st.html(f'<div class="sd-calc-focused-title">{pokemon_icon(selected["species"], data, "sd-focused-sprite")}<span class="sd-calc-name-types"><strong>{esc(species_name(selected["species"], data))}</strong><span class="sd-calc-meta-types">{badges((data.get("species", {}).get(selected["species"]) or {}).get("types"))}</span></span></div>')
                            calc_hp_badge(battle_state["player"][str(selected["id"])], "player", selected["id"], battle_state)
                        flags = st.session_state.setdefault("calc_item_flags", {})
                        item_key = f"calc_item_player_{selected['id']}"
                        with st.container(horizontal=True, key="calc_player_item_row"):
                            st.html(f'<span class="sd-calc-item-badge">{esc(human(selected["ability"]))}</span>')
                            focused_fields(selected, data, connect, save_entry)
                            active = st.toggle("Use Item", value=flags.get(item_key, True),
                                               key=item_key, persist_state="session",
                                               help="Off simulates the held item being absent in the calculation.")
                        flags[item_key] = active
                        st.html('<div class="sd-detail-divider"></div>')
                        focused_profile_fields(selected, data, connect, save_entry)
                    else:
                        st.info("Choose an alive Pokémon for a party slot.")
            with enemy_focus_col:
                with st.container(key="calc_enemy_panel"):
                    st.html(f'<div class="sd-calc-panel-heading">{esc(trainer_name)}&#39;s Pokémon</div>')
                    if enemy:
                        with st.container(horizontal=True, wrap=False, vertical_alignment="center", key="calc_enemy_heading"):
                            st.html(f'<div class="sd-calc-focused-title">{pokemon_icon(enemy.get("species"), data, "sd-focused-sprite")}<span class="sd-calc-name-types"><strong>{esc(species_name(enemy.get("species"), data))}</strong><span class="sd-calc-meta-types">{badges((data.get("species", {}).get(enemy.get("species")) or {}).get("types"))}</span></span></div>')
                            calc_hp_badge(battle_state["enemy"][str(enemy_focus)], "enemy", f"{trainer_id}_{enemy_focus}", battle_state)
                        flags = st.session_state.setdefault("calc_item_flags", {})
                        item_key = f"calc_item_enemy_{trainer_id}_{enemy_focus}"
                        with st.container(horizontal=True, key="calc_enemy_item_row"):
                            st.html(f'<span class="sd-calc-item-badge">Level {esc(enemy_level)}</span>')
                            st.html(f'<span class="sd-calc-item-badge">{esc(human(enemy.get("ability")))}</span>')
                            st.html(f'<span class="sd-calc-item-badge">{esc(human(enemy.get("nature")) if enemy.get("nature") else "Unknown")}</span>')
                            held_item = enemy.get("item")
                            held_item_icon = item_icon(held_item) if held_item else None
                            if held_item_icon:
                                st.html(f'<img class="sd-calc-held-item" src="{esc(held_item_icon)}" alt="{esc(human(held_item))}" title="{esc(human(held_item))}">')
                            active = st.toggle("Use Item", value=flags.get(item_key, True),
                                               key=item_key, persist_state="session",
                                               help="Off simulates the held item being absent in the calculation.")
                        flags[item_key] = active
                        st.html('<div class="sd-detail-divider"></div>')
                        st.html(enemy_profile_markup(enemy, data, enemy_level))
                    else:
                        st.info("This trainer has no Pokémon to show.")
            with enemy_roster_area:
                st.html(calc_trainer_header(trainer.get("name") or human(trainer_id), trainer_group,
                                            trainer_icon(trainer_id, trainer.get("trainer_pic")), len(enemies)))
                enemy_slots = []
                for index in range(6):
                    if index < len(order):
                        source_index = order[index]
                        member = enemies[source_index]
                        level = resolve_level(member.get("raw_level", member.get("level")), highest_level)
                        enemy_slots.append(calc_slot(member, data, level, source_index == enemy_focus))
                    else:
                        enemy_slots.append(None)
                enemy_result = calc_roster(slots=enemy_slots, options=[],
                                           key="calc-enemy-roster", editable_empty=False)
                focus_event = enemy_result.focus
                if focus_event and focus_event.get("token") != st.session_state.get("calc_enemy_focus_token"):
                    st.session_state["calc_enemy_focus_token"] = focus_event["token"]
                    slot = focus_event.get("slot")
                    if isinstance(slot, int) and 0 <= slot < len(order):
                        st.session_state["calc_enemy_focus"] = order[slot]
                        st.rerun()
def render_page(db, data, save_entry, resolve_level, connect, move_box_slot, apply_party_slots):
    active = st.session_state.setdefault("active_tab", "Calc")
    with st.container(key="sd_header"):
        brand_col, nav_col = st.columns([2, 1], vertical_alignment="center")
        with brand_col:
            st.html('<div class="sd-wordmark">Sidedex</div>')
        with nav_col:
            with st.container(horizontal=True, horizontal_alignment="right"):
                if st.button("Pokedex", key=f"nav_pokedex_{active.lower()}"):
                    st.session_state["active_tab"] = "Pokedex"
                    st.rerun()
                if st.button("Calc", key=f"nav_calc_{active.lower()}"):
                    st.session_state["active_tab"] = "Calc"
                    st.rerun()
    if active == "Calc":
        render_calc(db, data, save_entry, resolve_level, connect, move_box_slot, apply_party_slots)
    else:
        render_pokedex(db, data, save_entry, resolve_level, connect, move_box_slot)


if __name__ == "__main__":
    nature_stats_self_check()
    focused_profile_self_check()
    print("nature stats and focused profile checks passed")
