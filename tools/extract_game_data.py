#!/usr/bin/env python3
"""Extract the small, read-only battle-data snapshot from the pinned Imperium checkout."""

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "data/source_lock.json").read_text(encoding="utf-8"))
NON_SPECIES_IDS = {"SPECIES_NONE", "SPECIES_EGG", "SPECIES_SHINY_TAG"}


def read(path):
    return path.read_text(encoding="utf-8")


def strip_comments(text):
    return re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S)


def blocks(text, pattern):
    """Yield each designated initializer name and its balanced body."""
    for match in re.finditer(pattern, text):
        start = text.find("{", match.end() - 1)
        depth, quote, escaped = 0, False, False
        for end in range(start, len(text)):
            char = text[end]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quote = False
                continue
            if char == '"':
                quote = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield match.group(1), text[start + 1:end]
                    break


def top_level_blocks(text):
    """Yield brace-delimited entries that are direct children of an array body."""
    depth, start, quote, escaped = 0, None, False, False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
            continue
        if char == '"':
            quote = True
        elif char == "{":
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start:index]
                start = None


def fields(body):
    return {name: value.strip() for name, value in re.findall(r"\.([A-Za-z_]\w*)\s*=\s*(.*?)(?=,\s*\.[A-Za-z_]\w*\s*=|,?\s*$)", body, re.S)}


def integer(raw):
    return int(raw, 0) if raw and re.fullmatch(r"\s*(?:0[xX][\da-fA-F]+|\d+)\s*", raw) else None


def clean_c_string(raw):
    found = re.search(r'(?:_|COMPOUND_STRING)\s*\(\s*"((?:\\.|[^"\\])*)"', raw or "")
    return bytes(found.group(1), "utf-8").decode("unicode_escape") if found else None


def clean_c_strings(raw):
    """Decode adjacent C string literals used by COMPOUND_STRING descriptions."""
    literals = re.findall(r'"((?:\\.|[^"\\])*)"', raw or "")
    return "".join(bytes(value, "utf-8").decode("unicode_escape") for value in literals) if literals else None


def ability_descriptions(text):
    descriptions = {}
    for key, body in blocks(strip_comments(text), r"\[\s*(ABILITY_\w+)\s*\]\s*=\s*\{"):
        # The pinned build uses the first branch for Snow Warning (Gen 9 Snow).
        match = re.search(r'\.description\s*=\s*COMPOUND_STRING\s*\(\s*((?:"(?:\\.|[^"\\])*"\s*)+)\)', body, re.S)
        if match:
            descriptions[key] = "".join(ast.literal_eval(literal) for literal in re.findall(r'"(?:\\.|[^"\\])*"', match.group(1)))
    return descriptions


def learnset_moves(text, level_up=False):
    """Index source learnset arrays by their C symbol."""
    result = {}
    pattern = r"static const struct LevelUpMove\s+(\w+)\[\]\s*=\s*\{(.*?)\n\};" if level_up else r"static const u16\s+(\w+)\[\]\s*=\s*\{(.*?)\n\};"
    for name, body in re.findall(pattern, strip_comments(text), re.S):
        moves = re.findall(r"LEVEL_UP_MOVE\s*\(\s*\d+\s*,\s*(MOVE_\w+)\s*\)" if level_up else r"\b(MOVE_\w+)\b", body)
        result[name] = list(dict.fromkeys(move for move in moves if move not in {"MOVE_NONE", "MOVE_UNAVAILABLE"}))
    return result


def level_up_moves(text):
    """Keep level and order so omitted trainer moves match GiveBoxMonInitialMoveset."""
    return {name: [(int(level), move) for level, move in re.findall(
                r"LEVEL_UP_MOVE\s*\(\s*(\d+)\s*,\s*(MOVE_\w+)\s*\)", body)]
            for name, body in re.findall(
                r"static const struct LevelUpMove\s+(\w+)\[\]\s*=\s*\{(.*?)\n\};", text, re.S)}


def initial_moves(learnset, level):
    moves = []
    for learned_at, move in learnset:
        if learned_at > level:
            break
        if learned_at and move not in moves:
            moves.append(move)
            moves = moves[-4:]
    return moves


# Trainer party stat key orders. IVs follow the packed TRAINER_PARTY_IVS bit order
# (include/data.h), EVs the u8[6] HP,ATK,DEF,SPATK,SPDEF,SPEED order applied in
# src/battle_main.c:2100-2108.
IV_STATS = ("hp", "attack", "defense", "speed", "sp_attack", "sp_defense")
EV_STATS = ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")


def trainer_ivs(raw):
    order = IV_STATS
    match = re.fullmatch(r"TRAINER_PARTY_IVS\s*\(([^()]*)\)", raw or "")
    if match:
        values = [integer(value.strip()) for value in split_args(match.group(1))]
        return dict(zip(order, values)) if len(values) == 6 and all(value is not None for value in values) else None
    packed = integer(raw)
    return {key: (packed >> (5 * index)) & 31 for index, key in enumerate(order)} if packed is not None else None


def trainer_evs(raw, presets):
    order = EV_STATS
    match = re.fullmatch(r"(TRAINER_PARTY_EVS\w*)\s*\(([^()]*)\)", raw or "")
    if not match:
        return None
    arguments = match.group(2) if match.group(1) == "TRAINER_PARTY_EVS" else presets.get(match.group(1), "")
    values = [integer(value.strip()) for value in split_args(arguments)]
    return dict(zip(order, values)) if len(values) == 6 and all(value is not None for value in values) else None


def expand_info_macro(call, macros):
    name, arguments = call.group(1), call.group(2)
    if name not in macros:
        return call.group(0)
    parameters, body = macros[name]
    for parameter, value in zip(parameters, split_args(arguments)):
        body = re.sub(r"\b" + re.escape(parameter) + r"\b", value, body)
    return body


def expand_info_macros(body, macros):
    for _ in range(8):
        expanded = re.sub(r"\b(\w+)\s*\(([^()]*)\)", lambda call: expand_info_macro(call, macros), body)
        if expanded == body:
            break
        body = expanded
    return body


def constants(path, prefix):
    out = {}
    for name, value in re.findall(r"^#define\s+(" + prefix + r"\w+)\s+(\d+)\b", read(path), re.M):
        out[name] = int(value)
    return out


def enum_labels(path, prefix):
    return {value: name for name, value in constants(path, prefix).items()}


def macro_aliases(path, prefix):
    return {name: value.strip() for name, value in re.findall(r"^#define\s+(" + prefix + r"\w+)\s+([^/\n]+)", read(path), re.M)
            if not re.fullmatch(r"\d+", value.strip())}


def resolve_types(raw, aliases):
    if not raw:
        return None
    mon_types = re.search(r"MON_TYPES\s*\((.*)\)", raw)
    inner = mon_types.group(1) if mon_types else aliases.get(raw.strip(), raw)
    found = []
    for part in inner.split(","):
        part = aliases.get(part.strip(), part)
        # The pinned source uses GEN_LATEST for P_UPDATED_TYPES, so take the true branch.
        part = re.sub(r"\([^()]*\?\s*(TYPE_\w+)\s*:\s*(TYPE_\w+)\)", r"\1", part)
        part = re.sub(r"(?:P_UPDATED_TYPES\s*>=\s*GEN_\d+\s*\?\s*)?(TYPE_\w+)\s*:\s*TYPE_\w+", r"\1", part)
        found.extend(re.findall(r"TYPE_\w+", part))
    return found[:2] if found else None


def species_details(row, body=None):
    stat_fields = {
        "hp": "baseHP", "attack": "baseAttack", "defense": "baseDefense",
        "sp_attack": "baseSpAttack", "sp_defense": "baseSpDefense", "speed": "baseSpeed",
    }
    stats = {name: integer((row.get(field) or "").split("#", 1)[0].strip().rstrip(",").strip())
             for name, field in stat_fields.items()}
    stats = stats if all(value is not None for value in stats.values()) else None
    raw_abilities = (re.search(r"\.abilities\s*=\s*\{([^}]*)\}", body or "") or [None, row.get("abilities", "")])[1]
    abilities = list(dict.fromkeys(re.findall(r"ABILITY_\w+", raw_abilities)))
    abilities = [ability for ability in abilities if ability != "ABILITY_NONE"] or None
    return {"stats": stats, "abilities": abilities}


def split_args(raw):
    parts, start, depth = [], 0, 0
    for index, char in enumerate(raw):
        depth += char in "([{"
        depth -= char in ")] }".replace(" ", "")
        if char == "," and depth == 0:
            parts.append(raw[start:index].strip())
            start = index + 1
    parts.append(raw[start:].strip())
    return parts


def split_top_level(text, separator=","):
    """Split on separators that are not nested in braces/brackets/parens or string literals."""
    parts, start, depth, quote, escaped = [], 0, 0, False, False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
            continue
        if char == '"':
            quote = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def designated_fields(body):
    """Fields of a designated initializer, ignoring commas nested in braces or strings."""
    out = {}
    for part in split_top_level(body):
        match = re.match(r"\s*\.([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", part, re.S)
        if match:
            out[match.group(1)] = match.group(2).strip()
    return out


def top_level_char(text, needle):
    depth, quote, escaped = 0, False, False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
            continue
        if char == '"':
            quote = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == needle and depth == 0:
            return index
    return None


_ALLOWED_C_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load,
                    ast.Constant, ast.Compare, ast.BoolOp, ast.Call, ast.Add, ast.Sub,
                    ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.LShift, ast.RShift,
                    ast.BitOr, ast.BitAnd, ast.BitXor, ast.USub, ast.UAdd, ast.Invert,
                    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.And, ast.Or,
                    ast.keyword)


def _c_division(tree):
    """C integer division truncates toward zero; Python's ast.Div is float division."""
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            node.op = ast.FloorDiv()
    return tree


def c_eval(expression, symbols, functions=None):
    """Evaluate a C integer constant expression against the pinned symbol table. None if unresolved."""
    expression = (expression or "").strip().rstrip(",").strip()
    if not expression:
        return None
    question = top_level_char(expression, "?")
    if question is not None:
        rest = expression[question + 1:]
        colon = top_level_char(rest, ":")
        if colon is None:
            return None
        condition = c_eval(expression[:question], symbols, functions)
        if condition is None:
            return None
        branch = rest[:colon] if condition else rest[colon + 1:]
        return c_eval(branch, symbols, functions)
    translated = re.sub(r"!(?!=)", " not ", expression.replace("&&", " and ").replace("||", " or "))
    try:
        tree = _c_division(ast.parse(translated, mode="eval"))
    except SyntaxError:
        return None
    if any(not isinstance(node, _ALLOWED_C_NODES) for node in ast.walk(tree)):
        return None
    environment = {"__builtins__": {}} if not functions else {"__builtins__": {}, **functions}
    try:
        value = eval(compile(tree, "<c-constant>", "eval"), environment, dict(symbols))
    except Exception:
        return None
    if isinstance(value, bool):
        return int(value)
    return value if isinstance(value, int) else None


def resolve_constant_name(expression, pattern, symbols, functions=None):
    """Resolve a conditional constant expression to the single identifier it selects."""
    expression = (expression or "").strip().rstrip(",").strip()
    question = top_level_char(expression, "?")
    if question is not None:
        rest = expression[question + 1:]
        colon = top_level_char(rest, ":")
        if colon is None:
            return None
        condition = c_eval(expression[:question], symbols, functions)
        if condition is None:
            return None
        branch = rest[:colon] if condition else rest[colon + 1:]
        return resolve_constant_name(branch, pattern, symbols, functions)
    match = re.fullmatch(pattern, expression)
    return match.group(0) if match else None


def constant_symbols(source):
    """Resolve plain integer #define macros from the pinned config and constant headers."""
    # TWO_TURN_ARG packs a string id with an optional << 16 argument; COMPRESS_BITS is identity
    # for the 0..7 status-bit encodings used by .argument (src/data/moves_info.h, include/metaprogram.h).
    functions = {
        "COMPRESS_BITS": lambda value: value,
        "TWO_TURN_ARG": lambda string_id, *extra: string_id | ((extra[0] << 16) if extra else 0),
    }
    raw = {}
    headers = (sorted((source / "include/constants").glob("*.h"))
               + sorted((source / "include/config").glob("*.h"))
               + [source / "include/data.h", source / "include/metaprogram.h"])
    for path in headers:
        if not path.exists():
            continue
        for name, value in re.findall(r"^#define\s+([A-Za-z_]\w*)\s+(.+)$", strip_comments(read(path)), re.M):
            raw.setdefault(name, value.strip())
    symbols = {"TRUE": 1, "FALSE": 0}
    for _ in range(16):
        changed = False
        for name, value in raw.items():
            if name in symbols:
                continue
            resolved = c_eval(value, symbols, functions)
            if resolved is not None:
                symbols[name] = resolved
                changed = True
        if not changed:
            break
    return symbols, functions


def move_flag_names(source):
    """Boolean struct MoveInfo fields are the move flags (include/pokemon.h)."""
    struct = re.search(r"struct MoveInfo\s*\{(.*?)\n\};", strip_comments(read(source / "include/pokemon.h")), re.S)
    return re.findall(r"\bu32\s+(\w+)\s*:\s*1\s*;", struct.group(1)) if struct else []


def parse_additional_effects(body, symbols, functions, move_id, warnings):
    """Parse .additionalEffects = ADDITIONAL_EFFECTS({...}, ...) into contract secondary effects."""
    marker = re.search(r"\.additionalEffects\s*=\s*ADDITIONAL_EFFECTS\s*\(", body)
    if not marker:
        # No additional effects in the source table is a resolved empty list, not an unknown.
        return []
    start = marker.end() - 1
    depth = 0
    for end in range(start, len(body)):
        if body[end] == "(":
            depth += 1
        elif body[end] == ")":
            depth -= 1
            if depth == 0:
                inner = body[start + 1:end]
                break
    else:
        warnings.append({"move": move_id, "field": "secondary_effects", "reason": "unbalanced ADDITIONAL_EFFECTS parentheses"})
        return None
    effects = []
    for element in split_top_level(inner):
        element = element.strip()
        if not element.startswith("{"):
            continue
        entry = designated_fields(element[1:element.rfind("}")])
        effect = resolve_constant_name(entry.get("moveEffect"), r"(?:MOVE_EFFECT|Z_EFFECT)_\w+", symbols, functions)
        chance = c_eval(entry.get("chance"), symbols, functions) if "chance" in entry else 0
        if effect is None or chance is None:
            warnings.append({"move": move_id, "field": "secondary_effects", "raw": entry.get("moveEffect") or entry.get("chance"),
                             "reason": "additional effect constant is not statically resolved"})
        item = {"chance": chance, "effect": effect}
        for source_key, target_key in (("self", "self"), ("onlyIfTargetRaisedStats", "only_if_target_raised_stats"),
                                       ("onChargeTurnOnly", "on_charge_turn_only"), ("sheerForceBoost", "sheer_force_boost")):
            if source_key in entry:
                item[target_key] = bool(c_eval(entry[source_key], symbols, functions))
        effects.append(item)
    return effects


def resolve_preprocessor(text, symbols, functions):
    """Keep the active #if/#ifdef/#elif/#else branch using the pinned config symbols."""
    kept, stack = [], []
    for line in text.splitlines():
        directive = re.match(r"\s*#\s*(\w+)\s*(.*)", line)
        if directive:
            name, rest = directive.group(1), directive.group(2).strip().rstrip("\\").strip()
            parent = all(frame["active"] for frame in stack)
            if name == "if":
                condition = parent and bool(c_eval(rest, symbols, functions))
                stack.append({"active": condition, "taken": condition})
                continue
            if name in ("ifdef", "ifndef"):
                defined = rest in symbols
                condition = parent and (defined if name == "ifdef" else not defined)
                stack.append({"active": condition, "taken": condition})
                continue
            if name == "elif" and stack:
                frame = stack[-1]
                condition = parent and not frame["taken"] and bool(c_eval(rest, symbols, functions))
                frame["active"], frame["taken"] = condition, frame["taken"] or condition
                continue
            if name == "else" and stack:
                frame = stack[-1]
                frame["active"], frame["taken"] = parent and not frame["taken"], True
                continue
            if name == "endif":
                if stack:
                    stack.pop()
                continue
        if all(frame["active"] for frame in stack):
            kept.append(line)
    return "\n".join(kept)


def humanize_constant(name):
    return " ".join(word.capitalize() for word in re.sub(r"^(?:FLAG_|CAP_)", "", name).split("_") if word)


def config_constant(source, name):
    match = re.search(r"^#define\s+" + re.escape(name) + r"\s+([^\s/]+)", read(source / "include/config/caps.h"), re.M)
    return match.group(1) if match else None


def flag_map(text, symbol):
    block = re.search(symbol + r"\[\]\[2\]\s*=\s*\{(.*?)\n\s*\};", text, re.S)
    if not block:
        return None
    return [(flag, int(level)) for flag, level in re.findall(r"\{\s*(FLAG_\w+)\s*,\s*(\d+)\s*\}", block.group(1))]


def detect_oracle(source):
    """Dev-only oracle probe. Records the exact checks; never copies ROM or build products."""
    tools = ["make", "gcc", "cc", "clang", "arm-none-eabi-gcc", "agbcc",
             "mgba", "mgba-qt", "visualboyadvance", "vbam", "no$gba"]
    found, detail = {}, []
    for tool in tools:
        path = shutil.which(tool)
        found[tool] = path
        detail.append(f"python -c \"import shutil;print(shutil.which('{tool}'))\" -> {path!r}")
    candidates = [Path("C:/devkitPro"), Path("C:/Program Files/devkitPro"), Path("C:/Program Files (x86)/devkitPro"),
                  Path(os.environ.get("LOCALAPPDATA", "MISSING")) / "devkitPro"]
    install_hits = []
    for root in (Path("C:/Program Files"), Path("C:/Program Files (x86)"), Path(os.environ.get("LOCALAPPDATA", "MISSING"))):
        if root.is_dir():
            install_hits.extend(str(path) for path in root.glob("*[Dd]evkit*"))
            install_hits.extend(str(path) for path in root.glob("*[mM][gG][bB][aA]*"))
    install_hits.extend(str(path) for path in candidates if path.exists())
    install_hits = sorted(set(install_hits))
    detail.append("ls 'C:/Program Files', '%LOCALAPPDATA%' for *devkit*/*mGBA* -> " + (json.dumps(install_hits) if install_hits else "no matches"))
    test_dir = (source / "test").is_dir()
    detail.append(f"python -c \"from pathlib import Path;print(Path(r'{source / 'test'}').is_dir())\" -> {test_dir}")
    available = False
    if test_dir:
        reason = ("the pinned checkout has a test/ harness, but this extractor did not build or run it; "
                  "oracle-verified evidence is not claimed")
    elif any(found.values()) or install_hits:
        reason = ("a C/ARM toolchain or GBA emulator was detected, but no ROM build or emulator run was performed; "
                  "oracle-verified evidence is not claimed")
    else:
        reason = ("no C/ARM toolchain, no GBA emulator, and no ../pokeemerald-expansion-ref/test directory; "
                  "a ROM build oracle cannot be established locally without a multi-hour toolchain install")
    return {"available": available, "reason": reason, "detail": "\n".join(detail),
            "toolchain": {"path_tools": found, "install_dir_hits": install_hits, "test_dir": test_dir,
                          "rom_products_copied_into_sidedex": False}}


def extract(source):
    source = Path(source).resolve()
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if commit != LOCK["commit"]:
        raise SystemExit(f"source checkout is {commit}, expected pinned {LOCK['commit']}")
    if not re.search(r"^#define\s+B_TRAINER_MON_RANDOM_ABILITY\s+FALSE\b",
                     read(source / "include/config/battle.h"), re.M):
        raise SystemExit("trainer default ability rule changed; update extraction before regenerating")
    if not re.search(r"^#define\s+NATURE_HARDY\s+0\b",
                     read(source / "include/constants/pokemon.h"), re.M):
        raise SystemExit("trainer default nature rule changed; update extraction before regenerating")
    if not re.search(r"^#define\s+P_UPDATED_TYPES\s+GEN_LATEST\b",
                     read(source / "include/config/pokemon.h"), re.M):
        raise SystemExit("updated type rule changed; update extraction before regenerating")

    warnings = []
    unresolved_moves = {}
    opponents = constants(source / "include/constants/opponents.h", "TRAINER_")
    species_header = source / "include/constants/species.h"
    species_ids = constants(species_header, "SPECIES_")
    species_aliases = macro_aliases(species_header, "SPECIES_")
    move_ids = constants(source / "include/constants/moves.h", "MOVE_")
    move_aliases = macro_aliases(source / "include/constants/moves.h", "MOVE_")
    type_ids = constants(source / "include/constants/pokemon.h", "TYPE_")
    type_labels = {name: name.removeprefix("TYPE_").title() for name in type_ids}

    # C data tables are keyed by constants, so parse fields only from the entry body.
    trainer_text = strip_comments(read(source / "src/data/trainers.h"))
    trainer_entries = dict(blocks(trainer_text, r"\[\s*(TRAINER_\w+)\s*\]\s*=\s*\{"))
    ev_presets = dict(re.findall(r"#define\s+(TRAINER_PARTY_EVS_\w+)\(\)\s+\(\(const u8\[6\]\)\{([^}]*)\}\)",
                                 read(source / "include/data.h")))
    party_text = strip_comments(read(source / "src/data/trainer_parties.h"))
    party_arrays = {}
    for name, body in re.findall(r"static\s+const\s+struct\s+TrainerMon\s+(sParty_\w+)\[\]\s*=\s*\{(.*?)\n\};", party_text, re.S):
        party_arrays[name] = list(top_level_blocks(body))

    trainers = []
    used_species, used_moves = set(), set()
    for trainer_id, body in trainer_entries.items():
        row = fields(body)
        party_ref = re.search(r"TRAINER_PARTY\s*\(\s*(sParty_\w+)\s*\)", row.get("party", ""))
        party_name = party_ref.group(1) if party_ref else None
        members = []
        context_required = set()
        if row.get("isBossTrainer") == "TRUE":
            context_required.add("difficulty")
        if re.search(r"#\s*(?:if|ifdef|ifndef|elif|else)", body):
            warnings.append({"trainer": trainer_id, "reason": "trainer fields contain preprocessor branches; raw initializer retained only where supported"})
            context_required.add("source_condition")
        if party_name:
            party_source = party_arrays.get(party_name, [])
            if re.search(r"#\s*(?:if|ifdef|ifndef|elif|else)", "\n".join(party_source)):
                context_required.add("source_condition")
                warnings.append({"trainer": trainer_id, "party": party_name, "reason": "party array contains preprocessor branches; static extraction may include conditional alternatives"})
            for raw_member in party_source:
                mf = fields(raw_member)
                raw_species = mf.get("species")
                species = re.search(r"SPECIES_\w+", raw_species or "")
                raw_level = mf.get("lvl")
                level = integer(raw_level)
                if raw_level and (level is None or level >= 200):
                    context_required.add("highest_party_level")
                raw_moves = mf.get("moves", "")
                moves = re.findall(r"MOVE_\w+", raw_moves)
                if "DEFAULT" in raw_moves or "LEVEL_UP" in raw_moves:
                    moves = []
                used_moves.update(moves)
                species_id = species.group(0) if species else None
                if species_id:
                    used_species.add(species_id)
                unknowns = [key for key in ("species", "lvl", "moves", "heldItem", "nature", "ability") if key not in mf]
                for key, raw in mf.items():
                    if re.search(r"\b(DEFAULT|IF_|P_|B_\w+)\b", raw):
                        unknowns.append(key)
                members.append({
                    "species": species_id,
                    "raw_level": raw_level,
                    "level": level if level is None or level < 200 else None,
                    "moves": moves or None,
                    "item": mf.get("heldItem") or "ITEM_NONE",
                    "nature": mf.get("nature") or "NATURE_HARDY",
                    "ability": mf.get("ability"),
                    # src/battle_main.c:2100 sets the packed .iv unconditionally and the struct
                    # TrainerMon is zero-initialised, so an omitted .iv is fixed IV 0 (not rolled).
                    "ivs": trainer_ivs(mf["iv"]) if "iv" in mf else dict.fromkeys(IV_STATS, 0),
                    "ivs_encoding": mf.get("iv"),
                    "ivs_source": "table-encoding" if "iv" in mf else "table-default-0",
                    # .ev is only applied when non-NULL (src/battle_main.c:2101), so omitted EVs keep
                    # CreateMon's zeroes.
                    "evs": trainer_evs(mf["ev"], ev_presets) if "ev" in mf else dict.fromkeys(EV_STATS, 0),
                    "evs_encoding": mf.get("ev"),
                    "evs_source": "table-encoding" if "ev" in mf else "table-default-0",
                    "unknowns": sorted(set(unknowns)),
                })
        elif row.get("party") not in (None, "NULL"):
            warnings.append({"trainer": trainer_id, "field": "party", "raw": row["party"], "reason": "party selector or expression not statically resolved"})
            context_required.add("alternate_party")
        if re.search(r"\.partyPickerFunction\s*=\s*\w+", body) or ".additionalParties" in body:
            context_required.add("alternate_party")
        name = clean_c_string(row.get("trainerName")) or trainer_id.removeprefix("TRAINER_").replace("_", " ").title()
        raw_ai = row.get("aiFlags")
        if raw_ai is None:
            ai_flags = None
        elif integer(raw_ai) == 0:
            ai_flags = []
        else:
            ai_flags = list(dict.fromkeys(re.findall(r"AI_FLAG_\w+", raw_ai))) or None
            if ai_flags is None:
                warnings.append({"trainer": trainer_id, "field": "aiFlags", "raw": raw_ai,
                                 "reason": "AI flag expression is not statically resolved"})
        if row.get("party") == "NULL":
            party_size = 0
        elif party_name in party_arrays:
            party_size = len(party_arrays[party_name])
        else:
            party_size = integer(row.get("partySize"))
        trainers.append({
            "id": trainer_id,
            "numeric_id": opponents.get(trainer_id),
            "name": name,
            "trainer_pic": (re.search(r"TRAINER_PIC_\w+", row.get("trainerPic", "")) or [None])[0],
            "trainer_class": (re.search(r"TRAINER_CLASS_\w+", row.get("trainerClass", "")) or [None])[0],
            "flags": [flag for flag in re.findall(r"AI_FLAG_\w+", row.get("aiFlags", ""))],
            "ai_flags": ai_flags,
            "party_size": party_size,
            "items": re.findall(r"ITEM_\w+", row.get("items", "")),
            "double_battle": row.get("doubleBattle"),
            "is_boss": row.get("isBossTrainer") == "TRUE",
            "conditional_party": bool(context_required),
            "context_required": sorted(context_required),
            "party": members,
        })

    # All active species entries are parsed for the Box picker; include only ID, display name, and types.
    species = {}
    species_files = sorted((source / "src/data/pokemon/species_info").glob("*.h")) + [source / "src/data/pokemon/species_info.h"]
    species_info_texts = []
    for file in species_files:
        text = strip_comments(read(file))
        species_info_texts.append(text)
    type_macros, species_info_macros, species_learnset_macros, info_macros, species_learnsets, species_dex = {}, {}, {}, {}, {}, {}
    for text in species_info_texts:
        for name, value in re.findall(r"^\s*#define\s+([A-Z_]\w*)\s+(.+)$", text, re.M):
            if "TYPE_" in value:
                type_macros.setdefault(name, value.strip())
        expanded = text.replace("\\\n", " ")
        for name, parameters, body in re.findall(r"^#define\s+(\w+)\(([^)]*)\)\s*(.*)$", expanded, re.M):
            info_macros[name] = ([part.strip() for part in parameters.split(",")], body)
            if ".types" in body:
                species_info_macros[name] = ([part.strip() for part in parameters.split(",")], body)
            if re.search(r"\.(?:levelUpLearnset|teachableLearnset|eggMoveLearnset)\s*=", body):
                species_learnset_macros[name] = ([part.strip() for part in parameters.split(",")], body)
    relevant_macros = species_info_macros | species_learnset_macros
    while True:
        nested = {name: macro for name, macro in info_macros.items()
                  if name not in relevant_macros and any(re.search(r"\b" + re.escape(used) + r"\s*\(", macro[1]) for used in relevant_macros)}
        if not nested:
            break
        species_learnset_macros.update(nested)
        relevant_macros.update(nested)
    for file, text in zip(species_files, species_info_texts):
        for key, body in blocks(text, r"\[\s*(SPECIES_\w+)\s*\]\s*=\s*\{"):
            if key in NON_SPECIES_IDS:
                continue
            f = fields(body)
            expanded_body = expand_info_macros(body, species_info_macros | species_learnset_macros)
            expanded_fields = fields(expanded_body)
            if expanded_fields.get("natDexNum"):
                species_dex[key] = expanded_fields["natDexNum"]
            species_learnsets[key] = {field: match.group(1) for field in ("levelUpLearnset", "teachableLearnset", "eggMoveLearnset")
                                      if (match := re.search(r"\." + field + r"\s*=\s*(s\w+Learnset)", expanded_body))}
            raw_types = expanded_fields.get("types") or f.get("types", "")
            types = resolve_types(raw_types, type_macros)
            if not types:
                updated_branch = re.search(r"#if\s+P_UPDATED_TYPES\s*>=\s*GEN_6\b(.*?)#else", expanded_body, re.S)
                if updated_branch:
                    active_type = re.search(r"\.types\s*=\s*(MON_TYPES\([^)]*\))", updated_branch.group(1))
                    if active_type:
                        types = resolve_types(active_type.group(1), type_macros)
            details = species_details(expanded_fields, expanded_body)
            species[key] = {"name": clean_c_string(f.get("speciesName")) or key.removeprefix("SPECIES_").replace("_", " ").title(),
                            "types": types, **details,
                            "unknowns": [field for field, value in (("types", types), ("stats", details["stats"])) if value is None]}
            if not types:
                warnings.append({"species": key, "field": "types", "raw": raw_types or None, "reason": "type macro is not statically resolved"})
        for key, macro, arguments in re.findall(r"\[\s*(SPECIES_\w+)\s*\]\s*=\s*(\w+)\s*\(([^;\n]*)\)", text):
            if key in NON_SPECIES_IDS:
                continue
            if key in species:
                continue
            types = None
            details = {"stats": None, "abilities": None}
            if macro in species_info_macros or macro in species_learnset_macros:
                parameters, template = (species_info_macros | species_learnset_macros)[macro]
                values = split_args(arguments)
                for parameter, value in zip(parameters, values):
                    template = re.sub(r"\b" + re.escape(parameter) + r"\b", value, template)
                template = expand_info_macros(template, species_info_macros | species_learnset_macros)
                expanded_fields = fields(template)
                if expanded_fields.get("natDexNum"):
                    species_dex[key] = expanded_fields["natDexNum"]
                types = resolve_types(expanded_fields.get("types"), type_macros)
                details = species_details(expanded_fields, template)
                species_learnsets[key] = {field: match.group(1) for field in ("levelUpLearnset", "teachableLearnset", "eggMoveLearnset")
                                          if (match := re.search(r"\." + field + r"\s*=\s*(s\w+Learnset)", template))}
            species[key] = {"name": key.removeprefix("SPECIES_").replace("_", " ").title(), "types": types or None, **details,
                            "unknowns": [field for field, value in (("types", types), ("stats", details["stats"])) if value is None]}
            if not types:
                warnings.append({"species": key, "field": "types", "raw": f"{macro}({arguments})", "reason": "species macro type is unresolved"})
    for alias in species_aliases:
        if alias in NON_SPECIES_IDS:
            continue
        target, seen = species_aliases[alias], {alias}
        while target in species_aliases and target not in seen:
            seen.add(target)
            target = species_aliases[target]
        if alias in species and target not in species and target in species_ids:
            species[target] = {**species[alias], "alias_of": alias}
        if alias not in species:
            if target in species:
                species[alias] = {**species[target], "alias_of": target}
            else:
                species[alias] = {"name": alias.removeprefix("SPECIES_").replace("_", " ").title(), "types": None, "stats": None, "abilities": None,
                                  "alias_of": target, "unknowns": ["types", "stats"]}
                warnings.append({"species": alias, "field": "types", "raw": target, "reason": "species alias target has no statically parsed C entry"})
    for key in species_ids:
        if key in NON_SPECIES_IDS:
            continue
        if key not in species and key in used_species:
            species[key] = {"name": key.removeprefix("SPECIES_").replace("_", " ").title(), "types": None, "stats": None, "abilities": None, "unknowns": ["types", "stats"]}
            warnings.append({"species": key, "field": "types", "reason": "no direct species_info initializer; type remains unknown"})

    level_up_text = read(source / "src/data/pokemon/level_up_learnsets.h")
    ordered_level_up = level_up_moves(level_up_text)
    learnset_sources = {
        "levelUpLearnset": learnset_moves(level_up_text, level_up=True),
        "teachableLearnset": learnset_moves(read(source / "src/data/pokemon/teachable_learnsets.h")),
        "eggMoveLearnset": learnset_moves(read(source / "src/data/pokemon/egg_moves.h")),
    }
    moves_by_dex, level_up_by_dex = {}, {}
    for species_id, record in species.items():
        refs = species_learnsets.get(species_id) or species_learnsets.get(record.get("alias_of"), {})
        learnable = []
        for field, arrays in learnset_sources.items():
            learnable.extend(arrays.get(refs.get(field), []))
        learnable = list(dict.fromkeys(learnable))
        if learnable and species_dex.get(species_id):
            moves_by_dex.setdefault(species_dex[species_id], learnable)
        record["learnable_moves"] = learnable
        record["level_up_moves"] = ordered_level_up.get(refs.get("levelUpLearnset"), [])
        if record["level_up_moves"] and species_dex.get(species_id):
            level_up_by_dex.setdefault(species_dex[species_id], record["level_up_moves"])
    for species_id, record in species.items():
        if not record["learnable_moves"] and species_dex.get(species_id) in moves_by_dex:
            record["learnable_moves"] = moves_by_dex[species_dex[species_id]]
        if not record["level_up_moves"] and species_dex.get(species_id) in level_up_by_dex:
            record["level_up_moves"] = level_up_by_dex[species_dex[species_id]]

    for trainer in trainers:
        for member in trainer["party"]:
            record = species.get(member["species"], {})
            if not member["moves"] and member["level"] is not None and record.get("level_up_moves"):
                member["moves"] = initial_moves(record["level_up_moves"], member["level"]) or None
            if member["ability"] in (None, "ABILITY_NONE"):
                member["ability"] = (record.get("abilities") or [None])[0]
            resolved = {"heldItem", "nature"}
            if member["moves"]:
                resolved.add("moves")
            if member["ability"]:
                resolved.add("ability")
            member["unknowns"] = [field for field in member["unknowns"] if field not in resolved]
        if any(member["unknowns"] for member in trainer["party"]):
            warnings.append({"trainer": trainer["id"], "reason": "one or more party fields remain unresolved"})

    # Move data is compact and lets the UI resolve move types for arbitrary Box moves.
    symbols, functions = constant_symbols(source)
    flag_names = move_flag_names(source)
    move_text = resolve_preprocessor(strip_comments(read(source / "src/data/moves_info.h")), symbols, functions)
    shared_descriptions = {
        name: clean_c_strings(value)
        for name, value in re.findall(
            r'(?:static\s+)?const\s+u8\s+(\w+)\[\]\s*=\s*_\s*\(\s*((?:"(?:\\.|[^"\\])*"\s*)+)\);',
            move_text, re.S)
    }
    moves = {}
    for key, body in blocks(move_text, r"\[\s*(MOVE_\w+)\s*\]\s*=\s*\{"):
        if key not in move_ids:
            continue
        f = designated_fields(body)
        category_name = resolve_constant_name(f.get("category"), r"DAMAGE_CATEGORY_\w+", symbols, functions)
        category = {"DAMAGE_CATEGORY_PHYSICAL": "physical", "DAMAGE_CATEGORY_SPECIAL": "special",
                    "DAMAGE_CATEGORY_STATUS": "status"}.get(category_name)
        effect = resolve_constant_name(f.get("effect"), r"EFFECT_\w+", symbols, functions)
        target = resolve_constant_name(f.get("target"), r"MOVE_TARGET_\w+", symbols, functions)
        priority = c_eval(f.get("priority"), symbols, functions) if "priority" in f else None
        always_critical = bool(c_eval(f.get("alwaysCriticalHit"), symbols, functions)) if "alwaysCriticalHit" in f else False
        crit_ratio = c_eval(f.get("criticalHitStage"), symbols, functions) if "criticalHitStage" in f else 0
        # include/pokemon.h:512: strikeCount "Defaults to 1 if not set".
        strike_count = c_eval(f.get("strikeCount"), symbols, functions) if "strikeCount" in f else 1
        # include/pokemon.h:511 recoil is a 7-bit percentage of damage dealt; an omitted entry is 0
        # (src/battle_script_commands.c:6012 divides by 100).
        recoil = c_eval(f.get("recoil"), symbols, functions) if "recoil" in f else 0
        flags, flags_resolved = [], True
        for flag in flag_names:
            if flag in f:
                value = c_eval(f[flag], symbols, functions)
                if value is None:
                    flags_resolved = False
                elif value:
                    flags.append(flag)
        argument = f.get("argument")
        effect_argument = {"raw": argument, "value": c_eval(argument, symbols, functions)} if argument is not None else None
        if effect_argument is not None and effect_argument["value"] is None:
            warnings.append({"move": key, "field": "effect_argument", "raw": argument,
                             "reason": "move argument expression is not statically resolved"})
            effect_argument = None
        moves[key] = {"name": clean_c_string(f.get("name")) or key.removeprefix("MOVE_").replace("_", " ").title(),
                      "type": resolve_constant_name(f.get("type"), r"TYPE_\w+", symbols, functions),
                      "power": c_eval(f.get("power"), symbols, functions),
                      "accuracy": c_eval(f.get("accuracy"), symbols, functions),
                      "pp": c_eval(f.get("pp"), symbols, functions),
                      "description": (shared_descriptions.get(f.get("description"))
                                      if f.get("description") in shared_descriptions
                                      else clean_c_strings(f.get("description"))),
                      "category": category,
                      "effect": effect,
                      "effect_argument": effect_argument,
                      "priority": priority,
                      "target": target,
                      "secondary_effects": parse_additional_effects(body, symbols, functions, key, warnings),
                      "flags": flags if flags_resolved else None,
                      "always_critical": always_critical,
                      "crit_ratio": crit_ratio,
                      "strike_count": strike_count,
                      "recoil": recoil}
        for field, value in (("category", category), ("effect", effect), ("target", target), ("priority", priority)):
            if value is None:
                warnings.append({"move": key, "field": field, "raw": f.get(field),
                                 "reason": "move field is not statically resolved"})
        if not flags_resolved:
            warnings.append({"move": key, "field": "flags", "reason": "a move flag expression is not statically resolved"})
        if moves[key]["description"] is None:
            warnings.append({"move": key, "field": "description", "raw": f.get("description"),
                             "reason": "move description is not statically resolved"})
    for alias, target_name in move_aliases.items():
        if target_name in moves and alias not in moves:
            moves[alias] = {**moves[target_name], "alias_of": target_name}

    abilities = ability_descriptions(read(source / "src/data/abilities.h"))

    # Evaluate the pinned build's type chart, including its generation-dependent macros.
    type_text = strip_comments(read(source / "src/data/types_info.h"))
    chart_body = re.search(r"gTypeEffectivenessTable\[[^]]+\]\[[^]]+\]\s*=\s*\{(.*?)\n\};", type_text, re.S).group(1)
    raw_chart = dict(blocks(chart_body, r"\[\s*(TYPE_\w+)\s*\]\s*=\s*\{"))
    rows = {}
    alias = {"______": "1", "STL_RS": "1", "PSN_RS": "0.5", "BUG_RS": "1", "PSY_RS": "2", "FIR_RS": "0.5"}
    # These toggles are from the pinned config: GEN_LATEST=GEN_9, GEN_6=5.
    for atk, body in raw_chart.items():
        cells = re.findall(r"X\(([^)]+)\)|\b([A-Z_]+)\b", body)
        vals = []
        for number, token in cells:
            token = token or number
            if token == "STL_RS":
                value = 0.5
            elif token == "PSN_RS":
                value = 0.5
            elif token == "BUG_RS":
                value = 1.0
            elif token == "PSY_RS":
                value = 2.0
            elif token == "FIR_RS":
                value = 0.5
            else:
                value = float(number) if number else 1.0
            vals.append(value)
        rows[atk] = vals
    type_chart = {}
    for atk, vals in rows.items():
        type_chart[atk] = {defense: vals[index] for defense, index in type_ids.items() if index < len(vals)}
        if len(vals) != len(type_ids):
            warnings.append({"field": "type_chart", "type": atk, "reason": f"expected {len(type_ids)} entries, parsed {len(vals)}"})

    caps_text = strip_comments(read(source / "src/caps.c"))
    max_level = constants(source / "include/constants/pokemon.h", "MAX_").get("MAX_LEVEL")
    cap_entries = flag_map(caps_text, "sLevelCapFlagMap")
    if cap_entries is None or max_level is None:
        level_caps = None
        warnings.append({"field": "level_caps", "reason": "sLevelCapFlagMap or MAX_LEVEL was not parsed from src/caps.c"})
    else:
        level_caps = [{"id": f"CAP_{index + 1}", "flag": flag, "level": level, "label": humanize_constant(flag)}
                      for index, (flag, level) in enumerate(cap_entries)]
        level_caps.append({"id": "CAP_MAX", "flag": None, "level": max_level, "label": "Maximum level"})
    ev_entries = flag_map(caps_text, "sEvCapFlagMap")
    ev_caps = None if ev_entries is None else {
        "cap_type": config_constant(source, "B_EV_CAP_TYPE"),
        "caps": [{"id": f"EVCAP_{index + 1}", "flag": flag, "value": value, "label": humanize_constant(flag)}
                 for index, (flag, value) in enumerate(ev_entries)]}

    battle_main = strip_comments(read(source / "src/battle_main.c"))
    easy_delta = -2 if re.search(r"finalLevel -=2;", battle_main) and re.search(r"partyData\[i\]\.lvl-2", battle_main) else None
    normal_delta = 0 if re.search(r"CreateMon\(&party\[i\], species, partyData\[i\]\.lvl,", battle_main) else None
    boss_note = "post-champion Elite Four/Champion parties are fixed at level 100 (src/battle_main.c:2063)"
    difficulty_boss_levels = {
        "Easy": {"boss_level_delta": easy_delta, "applies_to": "isBossTrainer", "notes": boss_note,
                 "provenance": "src/battle_main.c:2075"} if easy_delta is not None else None,
        "Normal": {"boss_level_delta": normal_delta, "applies_to": "isBossTrainer", "notes": boss_note,
                   "provenance": "src/battle_main.c:2087"} if normal_delta is not None else None,
        "Hard": None,
    }

    return {
        "schema_version": 2,
        "source": {"repo": LOCK["repo"], "commit": commit, "config": LOCK["active_config"], "license_status": LOCK["license_status"]},
        "warnings": warnings,
        "trainers": trainers,
        "species": species,
        "abilities": abilities,
        "moves": moves,
        "types": type_labels,
        "type_chart": type_chart,
        "level_caps": level_caps,
        "level_cap_type": config_constant(source, "B_LEVEL_CAP_TYPE"),
        "ev_caps": ev_caps,
        "difficulty_boss_levels": difficulty_boss_levels,
        "oracle": detect_oracle(source),
    }


def self_check(data, source):
    trainers = {t["id"]: t for t in data["trainers"]}
    assert trainers["TRAINER_SAWYER_1"]["party"][0]["species"] == "SPECIES_GRAVELER_ALOLA"
    assert trainers["TRAINER_SAWYER_1"]["party"][0]["level"] == 40
    inactive = read(Path(source) / "src/data/trainers.party")
    sawyer = re.search(r"===\s*TRAINER_SAWYER_1\s*===(.*?)(?=\n===|\Z)", inactive, re.S)
    assert sawyer and re.search(r"\bGeodude\b", sawyer.group(1), re.I) and re.search(r"Level:\s*21\b", sawyer.group(1))
    assert trainers["TRAINER_ROXANNE_1"]["is_boss"] is True
    assert trainers["TRAINER_ROXANNE_1"]["party"][0]["species"] == "SPECIES_RHYHORN"
    assert trainers["TRAINER_ROXANNE_1"]["party"][0]["moves"] == ["MOVE_ROCK_TOMB", "MOVE_HORN_ATTACK", "MOVE_BULLDOZE", "MOVE_ROCK_SMASH"]
    assert trainers["TRAINER_ROXANNE_1"]["party"][0]["item"] == "ITEM_RINDO_BERRY"
    assert trainers["TRAINER_ROXANNE_1"]["party"][0]["nature"] == "NATURE_ADAMANT"
    assert trainers["TRAINER_ROXANNE_1"]["party"][0]["ability"] == "ABILITY_LIGHTNING_ROD"
    assert "AI_FLAG_OMNISCIENT" in trainers["TRAINER_ROXANNE_1"]["flags"]
    assert data["species"]["SPECIES_GRAVELER_ALOLA"]["types"] == ["TYPE_ROCK", "TYPE_ELECTRIC"]
    assert data["species"]["SPECIES_BULBASAUR"]["stats"] == {
        "hp": 45, "attack": 49, "defense": 49, "sp_attack": 65, "sp_defense": 65, "speed": 45,
    }
    assert data["species"]["SPECIES_BULBASAUR"]["abilities"] == ["ABILITY_OVERGROW", "ABILITY_CHLOROPHYLL"]
    assert data["abilities"]["ABILITY_CHLOROPHYLL"] == "Doubles Speed in Sun."
    assert data["abilities"]["ABILITY_SNOW_WARNING"] == "Summons Snow for 5 turns."
    assert data["species"]["SPECIES_AZURILL"]["stats"]["sp_defense"] == 40
    assert data["species"]["SPECIES_OGERPON_HEARTHFLAME"]["stats"]["attack"] == 120
    assert data["species"]["SPECIES_OGERPON_HEARTHFLAME"]["abilities"] == ["ABILITY_MOLD_BREAKER"]
    assert data["species"]["SPECIES_OGERPON_HEARTHFLAME"]["types"] == ["TYPE_GRASS", "TYPE_FIRE"]
    assert data["species"]["SPECIES_DARMANITAN_GALAR_ZEN_MODE_TWO"]["types"] == ["TYPE_ICE", "TYPE_FIRE"]
    assert data["type_chart"]["TYPE_ELECTRIC"]["TYPE_GROUND"] == 0.0
    assert data["moves"]["MOVE_TACKLE"] == {
        "name": "Tackle", "type": "TYPE_NORMAL", "power": 40,
        "accuracy": 100, "pp": 35,
        "description": "Charges the foe with a full-\nbody tackle.",
        "category": "physical", "effect": "EFFECT_HIT", "effect_argument": None,
        "priority": 0, "target": "MOVE_TARGET_SELECTED", "secondary_effects": [],
        "flags": ["makesContact"], "always_critical": False, "crit_ratio": 0, "strike_count": 1,
        "recoil": 0,
    }
    assert data["moves"]["MOVE_THUNDERBOLT"]["category"] == "special"
    assert data["moves"]["MOVE_THUNDERBOLT"]["secondary_effects"] == [{"chance": 10, "effect": "MOVE_EFFECT_PARALYSIS"}]
    assert data["moves"]["MOVE_ROCK_SLIDE"]["target"] == "MOVE_TARGET_BOTH"
    assert data["moves"]["MOVE_DOUBLE_KICK"]["strike_count"] == 2
    assert data["moves"]["MOVE_STORM_THROW"]["always_critical"] is True
    assert data["moves"]["MOVE_LEAF_BLADE"]["crit_ratio"] == 1
    assert data["moves"]["MOVE_QUICK_ATTACK"]["priority"] == 1
    assert data["moves"]["MOVE_TAKE_DOWN"]["recoil"] == 25
    assert data["moves"]["MOVE_QUICK_ATTACK"]["recoil"] == 0
    assert data["moves"]["MOVE_PROTECT"]["category"] == "status"
    assert data["moves"]["MOVE_HYPER_BEAM"]["description"] == "Powerful, but leaves the\nuser immobile the next turn."
    assert data["moves"]["MOVE_PSYSHOCK"]["description"] == "Attacks with a psychic wave\nthat does physical damage."
    assert data["moves"]["MOVE_PSYCHIC"]["description"] == "A powerful psychic attack\nthat may lower Sp. Def."
    assert data["level_caps"][0] == {"id": "CAP_1", "flag": "FLAG_BADGE01_GET", "level": 15, "label": "Badge01 Get"}
    assert data["level_caps"][-1]["id"] == "CAP_MAX" and data["level_caps"][-1]["level"] == 100
    assert data["level_cap_type"] == "LEVEL_CAP_FLAG_LIST"
    assert data["difficulty_boss_levels"]["Easy"]["boss_level_delta"] == -2
    assert data["difficulty_boss_levels"]["Normal"]["boss_level_delta"] == 0
    assert data["difficulty_boss_levels"]["Hard"] is None
    assert "AI_FLAG_OMNISCIENT" in trainers["TRAINER_ROXANNE_1"]["ai_flags"]
    assert trainers["TRAINER_SAWYER_1"]["ai_flags"] == ["AI_FLAG_CHECK_BAD_MOVE", "AI_FLAG_TRY_TO_FAINT", "AI_FLAG_CHECK_VIABILITY"]
    assert trainers["TRAINER_SAWYER_1"]["party_size"] == 1
    assert trainers["TRAINER_SAWYER_1"]["party"][0]["ivs"] == dict.fromkeys(IV_STATS, 0)
    assert trainers["TRAINER_SAWYER_1"]["party"][0]["evs"] == dict.fromkeys(EV_STATS, 0)
    assert trainers["TRAINER_SAWYER_1"]["party"][0]["ivs_encoding"] is None
    assert trainers["TRAINER_SAWYER_1"]["party"][0]["ivs_source"] == "table-default-0"
    assert trainers["TRAINER_SAWYER_1"]["party"][0]["evs_source"] == "table-default-0", "Sawyer's table omits .ev"
    assert trainers["TRAINER_ROXANNE_1"]["party"][0]["ivs_encoding"] == "TRAINER_PARTY_IVS(31, 31, 31, 31, 31, 31)"
    assert trainers["TRAINER_ROXANNE_1"]["party"][0]["ivs_source"] == "table-encoding"
    assert all(value == 31 for value in trainers["TRAINER_ROXANNE_1"]["party"][0]["ivs"].values())
    assert data["oracle"]["available"] is False
    assert all(p["species"] in data["species"] for t in data["trainers"] for p in t["party"] if p["species"])
    assert all(m in data["moves"] for t in data["trainers"] for p in t["party"] for m in (p["moves"] or []))
    assert "MOVE_THUNDERBOLT" in data["species"]["SPECIES_PIKACHU"]["learnable_moves"]
    assert "MOVE_SOLAR_BEAM" in data["species"]["SPECIES_BULBASAUR"]["learnable_moves"]
    assert "MOVE_ROLLOUT" in data["species"]["SPECIES_MINIOR_METEOR_RED"]["learnable_moves"]
    assert "MOVE_HYPER_BEAM" not in data["species"]["SPECIES_MAGIKARP"]["learnable_moves"]
    assert all(move in data["moves"] for record in data["species"].values() for move in record["learnable_moves"])
    return ["TRAINER_SAWYER_1 active C table resolves to Alolan Graveler level 40; inactive .party syntax says Geodude level 21",
            "TRAINER_ROXANNE_1 active C table resolves to a boss party with moves, items, natures, and abilities"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, nargs="?", default=ROOT.parent / "pokeemerald-expansion-ref",
                        help="local checkout of the pinned upstream repository")
    parser.add_argument("--output", type=Path, default=ROOT / "data/game_data.json")
    args = parser.parse_args()
    data = extract(args.source)
    data["regression_checks"] = self_check(data, args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.output}: {len(data['trainers'])} trainers, {len(data['species'])} species, {len(data['moves'])} moves")


if __name__ == "__main__":
    main()
