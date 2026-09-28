"""Release-ROM differential harness (package C, phase 4).

Two halves, both fail-closed:

* **capture** -- :func:`run_route` drives the pinned release ROM from cold boot
  through the fixed Emerald opening route (truck -> clock -> Littleroot ->
  Route 101 -> Birch's bag -> starter -> first wild battle) with *condition
  driven* steps.  Each step holds/taps a key mask and runs a bounded number of
  frames until a condition holds (callback address, map group/num, player x/y,
  a flag/var, or ``gBattleTypeFlags``).  On success each milestone step saves a
  hashed savestate OUTSIDE Git under the caller-provided milestones directory;
  the loader verifies ROM / worker / script identity hashes and rejects any
  mismatch.  A step that never meets its condition fails closed and reports the
  step name, frame, callback, map and position.
* **patch + resolve + compare** -- :func:`patch_payload` / :func:`make_blob`
  rewrite RAM inside a captured mGBA savestate and fix the header digest so the
  worker's identity check still accepts it; :func:`read_projection` reads the
  ``struct BattlePokemon`` array through the release map's own ``gBattleMons``
  address; :func:`compare_h2` is the H2 comparison.

The observable projection is deliberately narrow (contract release plan): HP,
maxHP, status1, PP, level and species per battler, both RNG streams and the
battle outcome.  Ordered event streams, query mode, AI preview and trainer-item
AI are explicitly *not* representable on the release ROM.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------- pinned identity
#
# Gate V1: the ROM / ELF / worker identity is owned by rom_identity.py.  This
# package hard-codes no digest; the JSON inputs carry "<<rom_identity.NAME>>"
# placeholders that resolve_identity() resolves below.
import rom_identity  # noqa: E402

PINNED_SOURCE_SHA = rom_identity.SOURCE_SHA
PINNED_ROM_SHA256 = rom_identity.ROM_SHA256
PINNED_ELF_SHA256 = rom_identity.ENGINE_ELF_SHA256
PINNED_WORKER_SHA256 = rom_identity.WORKER_SHA256
PINNED_WIRE_PROTOCOL = rom_identity.PROTOCOL_VERSION

MINGW = ROOT / "native" / "rom_worker" / "build" / "mingw"
DEFAULT_ROM = MINGW / "pokeemerald.gba"
DEFAULT_ELF = MINGW / "pokeemerald-test-headless.elf"
DEFAULT_WORKER = MINGW / "rom_worker.exe"
INPUT_SCRIPT_PATH = TOOLS / "release_input_script.json"
SCENARIOS_PATH = TOOLS / "release_scenarios.json"

# Release map pins (RELEASE_HARNESS_PLAN.md finding 2).  EWRAM battle pins coincide
# across builds; IWRAM pins do not and must always come from this table.
RELEASE_MAP = {
    "gBattleMons": 0x02000360,
    "gPlayerParty": 0x020375F8,
    "gEnemyParty": 0x02037850,
    "gBattleCommunication": 0x02000698,
    "gBattleOutcome": 0x020006A0,
    "gBattleWeather": 0x02000754,
    "gBattleStruct": 0x02000830,
    "gBattleTypeFlags": 0x020002C0,
    "gChosenMove": 0x020004E8,
    "gMapHeader": 0x0200ABD8,
    "gMain": 0x03004210,
    "gRngValue": 0x0300627C,
    "gRng2Value": 0x0300628C,
    "gTasks": 0x030063BC,
    "gSaveBlock1Ptr": 0x03006210,
    "gPlayerPartyCount": 0x020375F5,
    "gPlayerParty": 0x020375F8,
    # H3 boundary detection (C2).  Sources: pokeemerald.map in the pinned WSL
    # release checkout; every IWRAM pin is build-specific and must be re-read
    # from the ELF for a new build.
    "gBattleControllerExecFlags": 0x02000340,
    "gBattlersCount": 0x02000344,
    "gBattlerPartyIndexes": 0x02000348,
    "gCurrentTurnActionNumber": 0x0200035C,
    "gSideStatuses": 0x0200058C,
    "gDisableStructs": 0x020005F4,
    "gProtectStructs": 0x020006A4,
    "gEnemyPartyCount": 0x020375F6,
    "gFieldStatuses": 0x02000874,
    "gBattlerControllerFuncs": 0x03004F54,
    "gBattleMainFunc": 0x03004F80,
    "gBattleResults": 0x03004F84,
}

# nm-resolved release ELF function pointers (pokeemerald.elf, pinned release
# checkout).  Thumb addresses carry bit 0; comparisons always mask it.  These
# name the controller/main function pointers that define the H3 boundaries.
RELEASE_FUNCS = {
    "HandleInputChooseAction": 0x0805648C,          # src/battle_controller_player.c
    "PlayerHandleChooseAction": 0x08059734,
    "PlayerHandleChoosePokemon": 0x08059ABC,        # forced-switch party menu
    "HandleTurnActionSelectionState": 0x0807DDD8,   # src/battle_main.c, choice boundary
    "HandleFaintedMonActions": 0x080BA0D0,          # src/battle_util.c
    "BattleTurnPassed": 0x0807D980,
    "RunTurnActionsFunctions": 0x08080298,
    "DoBattleIntro": 0x0807CCD4,                    # intro, NOT a decision boundary
    "Controller_WaitForString": 0x0805FE04,
    "Controller_WaitForHealthBar": 0x0805FCC0,
}

# Release SaveBlock1 field offsets.
#
# The include/global.h comments (POS 0x00, LOCATION 0x04) hold for the early
# fields, but this Emerald Imperium build sets FREE_EXTRA_SEEN_FLAGS_SAVEBLOCK1
# = TRUE and has extra fields, so the *source comments* for flags/vars are
# stale.  The authoritative offsets below are read straight out of the pinned
# release ELF's FlagGet / VarGet disassembly (the compiler bakes in the struct
# offset), and the flag offsets were cross-checked live against the truck intro
# script's six setflag calls (0x2DF, 0x2F7, 0x2FA, 0x310, 0x331 all landed on
# the predicted byte/bit).
SB1_POS = 0x0000          # Coords16 (s16 x, s16 y)
SB1_LOCATION = 0x0004     # WarpData (s8 group, s8 num, s8 warpId, pad, s16 x, s16 y)
SB1_FLAGS = 0x19C4        # u8 flags[FLAGS_COUNT]:  bit (id & 7) of byte id >> 3
SB1_VARS = 0x1AF0         # u16 vars[] base = var id 0x4000; VarGet(id) = base + (id - 0x4000) * 2
VARS_START = 0x4000

# struct BattlePokemon (include/pokemon.h) -- source-defined, identical across builds.
# Offsets verified empirically against the test ELF's decoded ``state|active`` record
# (2-aligned u16 hp lands at 0x2A, status1 u32 at 0x50; the struct pads to 0x60).
BATTLEMON = {"species": 0x00, "moves": 0x0C, "pp": 0x25, "hp": 0x2A, "level": 0x2C,
             "max_hp": 0x2E, "status1": 0x50}
BATTLEMON_SIZE = 0x60
MAX_BATTLERS = 4

# struct BattlePokemon fields used by the C2 projection.  The include/pokemon.h
# comments are stale for this build (they say hp 0x29 / level 0x2B / status1
# 0x4D); every offset below was read off the live release root's gBattleMons
# bytes and cross-checked against the party mon (otId/personality match).
BATTLEMON.update({
    "attack": 0x02, "defense": 0x04, "speed": 0x06, "sp_attack": 0x08, "sp_defense": 0x0A,
    "ivs": 0x14, "stat_stages": 0x18, "ability": 0x20, "types": 0x22,
    "friendship": 0x2D, "item": 0x30, "pp_bonuses": 0x3D, "personality": 0x4C,
    "status2": 0x54, "ot_id": 0x58,
})

# struct Pokemon (party mon, stride 0x64): the 0x50-byte BoxPokemon is followed
# by plaintext battle stats.  The BoxPokemon secures (species/item/moves/pp/
# IVs/EVs/nature) live in PID-scrambled substructures at +0x20..0x4F; see
# PARTY_ENCRYPTED_REASON.
POKEMON_SIZE = 0x64
POKEMON_STATUS1 = 0x50
POKEMON_LEVEL = 0x54
POKEMON_HP = 0x56
POKEMON_MAX_HP = 0x58
POKEMON_HAS_SPECIES_BIT = 0x02   # BoxPokemon +0x13, hasSpecies:1
POKEMON_BOX_FLAGS = 0x13

# struct DisableStruct stride is derived from the release map (gProtectStructs
# 0x020006A4 - gDisableStructs 0x020005F4 = 0xB0 over 4 battlers).
DISABLE_STRUCT_SIZE = 0x2C
DS_DISABLED_MOVE = 0x06
DS_SUBSTITUTE_HP = 0x0F
DS_TIMERS = 0x11          # disableTimer:4 low nibble | encoreTimer:4 high nibble
DS_TAUNT_TIMER = 0x13     # rolloutTimerStartValue:4 low | tauntTimer:4 high

# statStages[] stores stage + 6 (DEFAULT_STAT_STAGE); wire stages are the
# signed delta, so the projection subtracts the offset.
DEFAULT_STAT_STAGE = 6
STAT_ORDER = ("stage_atk", "stage_def", "stage_spe", "stage_spa", "stage_spd",
              "stage_acc", "stage_eva")

# Status words (include/constants/battle.h).
SIDE_STATUS_REFLECT, SIDE_STATUS_LIGHTSCREEN = 1 << 0, 1 << 1
SIDE_STATUS_SPIKES, SIDE_STATUS_TOXIC_SPIKES = 1 << 4, 1 << 13
SIDE_STATUS_STEALTH_ROCK, SIDE_STATUS_STICKY_WEB = 1 << 14, 1 << 2
SIDE_STATUS_AURORA_VEIL = 1 << 11
SIDE_STATUS_SAFEGUARD, SIDE_STATUS_MIST, SIDE_STATUS_TAILWIND = 1 << 5, 1 << 8, 1 << 10
SIDE_STATUS_SCREEN_ANY = SIDE_STATUS_REFLECT | SIDE_STATUS_LIGHTSCREEN | SIDE_STATUS_AURORA_VEIL
SIDE_STATUS_HAZARDS_ANY = (SIDE_STATUS_SPIKES | SIDE_STATUS_STICKY_WEB
                           | SIDE_STATUS_TOXIC_SPIKES | SIDE_STATUS_STEALTH_ROCK)
SIDE_STATUS_CONDITIONS_ANY = SIDE_STATUS_SAFEGUARD | SIDE_STATUS_MIST | SIDE_STATUS_TAILWIND
STATUS_FIELD_TRICK_ROOM = 1 << 1
STATUS_FIELD_TERRAIN_MASK = 0x7 << 4   # electric/grassy/misty/psychic terrain bits

B_OUTCOME_NAMES = {0: "none", 1: "won", 2: "lost", 3: "drew", 4: "ran", 5: "teleported",
                   6: "mon-fled", 7: "caught", 8: "no-safari-balls", 9: "forfeited",
                   10: "mon-teleported"}

# H3 boundary kinds, Appendix C.3: 0 none, 1 choice, 2 forced switch, 3 terminal.
BOUNDARY_NONE, BOUNDARY_CHOICE, BOUNDARY_FORCED_SWITCH, BOUNDARY_TERMINAL = 0, 1, 2, 3
BOUNDARY_NAMES = {BOUNDARY_NONE: "none", BOUNDARY_CHOICE: "choice",
                  BOUNDARY_FORCED_SWITCH: "forced-switch", BOUNDARY_TERMINAL: "terminal"}

SAVESTATE_EWRAM_OFFSET = 0x21000
SAVESTATE_IWRAM_OFFSET = 0x19000
EWRAM_BASE = 0x02000000
IWRAM_BASE = 0x03000000
BLOB_HEADER_BYTES = 216
BLOB_PAYLOAD_SHA_OFFSET = 56
BLOB_MAGIC = b"SDXSTT01"

# Pokémon Emerald uses 32 4 KiB flash sectors.  Sectors 0..27 are the
# checksummed save slot; 28..31 may remain erased.  This image is useful only
# for exercising mGBA's save loader.  Zeroed SaveBlock payloads are not a
# playable save and must never be used as a release battle root.
SAVE_SECTOR_BYTES = 4096
SAVE_SECTOR_DATA_BYTES = 4084
SAVE_SECTOR_COUNT = 32
SAVE_SECTOR_SIGNATURE = 0x08012025

# Volatile bytes/fields that are not part of the compared projection.  Anything not
# named here must match or the comparison fails.
EXCLUDE_LIST = (
    ("requestId", "identifies a request, not state"),
    ("savestate-non-RAM-payload", "mGBA serialization scratch outside EWRAM/IWRAM"),
    ("post-terminal-raw-RAM", "presentation/controller teardown after a terminal record"),
)


class ReleaseHarnessError(RuntimeError):
    pass


class ReleaseRootUnavailable(ReleaseHarnessError):
    """No battle root could be captured; carries the last observed machine state."""

    def __init__(self, frames, last_state, script):
        self.frames = frames
        self.last_state = last_state
        self.script = script
        super().__init__(
            f"release root capture did not reach a battle after {frames} frames "
            f"(last cb2={last_state.get('callback2')!r}, "
            f"gBattleTypeFlags={last_state.get('battle_type_flags')!r}, "
            f"state={last_state!r}); "
            f"input script {script!r} is incomplete")


class RouteStepError(ReleaseHarnessError):
    """One condition-driven route step exhausted its frame bound unmet."""

    def __init__(self, step, frame, state):
        self.step = step
        self.frame = frame
        self.state = state
        super().__init__(
            f"route step {step.get('name')!r} failed at frame {frame} "
            f"(cb2={state.get('callback2')!r}, "
            f"map={state.get('map_group')}/{state.get('map_num')}, "
            f"pos=({state.get('player_x')}, {state.get('player_y')}), "
            f"gBattleTypeFlags={state.get('battle_type_flags')!r}, "
            f"state={state!r})")


class IdentityMismatch(ReleaseHarnessError):
    """ROM, worker, or script identity does not match the pinned/recorded value."""


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _save_sector_checksum(data: bytes) -> int:
    """Pokémon Emerald ``CalculateChecksum`` over little-endian u32 words."""
    if len(data) != SAVE_SECTOR_DATA_BYTES:
        raise ReleaseHarnessError("save sector data must be exactly 4084 bytes")
    total = sum(int.from_bytes(data[i:i + 4], "little")
                for i in range(0, len(data), 4)) & 0xFFFFFFFF
    return ((total >> 16) + total) & 0xFFFF


def write_empty_valid_save(path) -> dict:
    """Write a sector-valid, empty Emerald flash image for loader diagnostics.

    Save sectors 0..27 have the source-defined sector IDs, checksums,
    signature, and counter.  This validates raw save loading only; its zeroed
    save blocks do not represent an initialized player or game and cannot be
    used to claim a battle root.
    """
    image = bytearray(b"\xff" * (SAVE_SECTOR_BYTES * SAVE_SECTOR_COUNT))
    for sector_id in range(28):
        offset = sector_id * SAVE_SECTOR_BYTES
        data = bytes(SAVE_SECTOR_DATA_BYTES)
        image[offset:offset + SAVE_SECTOR_DATA_BYTES] = data
        footer = offset + SAVE_SECTOR_DATA_BYTES
        image[footer:footer + 12] = (
            sector_id.to_bytes(2, "little")
            + _save_sector_checksum(data).to_bytes(2, "little")
            + SAVE_SECTOR_SIGNATURE.to_bytes(4, "little")
            + (1).to_bytes(4, "little")
        )
    target = Path(path)
    target.write_bytes(image)
    return {"path": str(target), "bytes": len(image),
            "sha256": hashlib.sha256(image).hexdigest()}


# ------------------------------------------------------------------ input script

def resolve_identity(value):
    """Resolve ``<<rom_identity.NAME>>`` placeholders in a loaded JSON document."""
    if isinstance(value, str):
        if value.startswith("<<rom_identity.") and value.endswith(">>"):
            name = value[len("<<rom_identity."):-2]
            if not hasattr(rom_identity, name):
                raise ReleaseHarnessError(f"rom_identity has no {name!r}")
            return getattr(rom_identity, name)
        return value
    if isinstance(value, list):
        return [resolve_identity(item) for item in value]
    if isinstance(value, dict):
        return {key: resolve_identity(item) for key, item in value.items()}
    return value


def load_input_script_raw(path=INPUT_SCRIPT_PATH) -> dict:
    """The on-disk document, identity placeholders unresolved (provenance checks)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_scenarios_raw(path=SCENARIOS_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_input_script(path=INPUT_SCRIPT_PATH) -> dict:
    return resolve_identity(load_input_script_raw(path))


def load_scenarios(path=SCENARIOS_PATH) -> dict:
    data = resolve_identity(load_scenarios_raw(path))
    if not data.get("claim"):
        raise ReleaseHarnessError("release_scenarios.json must state the narrow claim")
    ids = [s["id"] for s in data["scenarios"]]
    if len(set(ids)) != len(ids):
        raise ReleaseHarnessError("duplicate scenario ids")
    return data


def _int(value) -> int:
    """Accept a JSON int or a hex string (``0x…``)."""
    if isinstance(value, int):
        return value
    return int(str(value), 16)


def _cb(value) -> int:
    """Callback address with the Thumb bit masked off for comparison."""
    return _int(value) & ~1


def read_state(client) -> dict:
    """Read the release-only route fields used for conditions and reports."""
    gmain = RELEASE_MAP["gMain"]
    state = {
        "callback2": client.read_memory(gmain + 4, 4),
        "battle_type_flags": client.read_memory(RELEASE_MAP["gBattleTypeFlags"], 2),
        "battle_communication": client.read_memory(RELEASE_MAP["gBattleCommunication"], 4),
        "party_count": client.read_memory(RELEASE_MAP["gPlayerPartyCount"], 1),
    }
    sb1 = int.from_bytes(client.read_memory_block(RELEASE_MAP["gSaveBlock1Ptr"], 4), "little")
    state["save_block1"] = sb1
    state["save_block1_ok"] = EWRAM_BASE <= sb1 < EWRAM_BASE + 0x40000
    if EWRAM_BASE <= sb1 < EWRAM_BASE + 0x40000:
        head = client.read_memory_block(sb1, 8)
        state["map_group"] = head[SB1_LOCATION + 0]
        state["map_num"] = head[SB1_LOCATION + 1]
        state["player_x"] = int.from_bytes(head[SB1_POS:SB1_POS + 2], "little", signed=True)
        state["player_y"] = int.from_bytes(head[SB1_POS + 2:SB1_POS + 4], "little", signed=True)
    return state


def read_flag(client, sb1, flag_id) -> bool:
    return bool(client.read_memory(sb1 + SB1_FLAGS + flag_id // 8, 1) & (1 << (flag_id % 8)))


def read_var(client, sb1, var_id) -> int:
    """Return the u16 value of a release var.

    Only ids in [VARS_START, 0x8000) index SaveBlock1; VarGet treats smaller
    ids as literal immediates and larger ids as special vars, so those are
    rejected here rather than silently read from the wrong address.
    """
    if not VARS_START <= var_id < 0x8000:
        raise ReleaseHarnessError(f"var id {var_id:#06x} is not a SaveBlock1 var")
    return client.read_memory(sb1 + SB1_VARS + 2 * (var_id - VARS_START), 2)


def eval_condition(client, cond, state) -> bool:
    """Evaluate a single ``until`` condition against a :func:`read_state` snapshot."""
    kind = cond.get("kind")
    sb1 = state.get("save_block1")
    if kind == "callback":
        value = state["callback2"] & ~1
        if "any" in cond:
            return any(value == _cb(v) for v in cond["any"])
        return value == _cb(cond["value"])
    if kind == "map":
        if "group" in cond and state.get("map_group") != _int(cond["group"]):
            return False
        if "num" in cond and state.get("map_num") != _int(cond["num"]):
            return False
        return ("group" in cond or "num" in cond)
    if kind == "pos":
        if "x" in cond and state.get("player_x") != _int(cond["x"]):
            return False
        if "y" in cond and state.get("player_y") != _int(cond["y"]):
            return False
        return ("x" in cond or "y" in cond)
    if kind == "party_count":
        return state.get("party_count") == _int(cond["value"])
    if kind == "battle_type_flags":
        if "nonzero" in cond:
            return state["battle_type_flags"] != 0
        return state["battle_type_flags"] == _int(cond["value"])
    if kind == "battle_communication":
        if "nonzero" in cond:
            return state["battle_communication"] != 0
        return state["battle_communication"] == _int(cond["value"])
    if kind == "mem":
        # Read any release-map symbol + offset; used to wait for the battle to
        # finish loading its battlers before snapshotting the root.
        address = RELEASE_MAP[cond["symbol"]] + int(cond.get("offset", 0))
        value = client.read_memory(address, int(cond.get("width", 2)))
        if "nonzero" in cond:
            return value != 0
        return value == _int(cond["value"])
    if kind == "boundary":
        # H3: compare the release-side boundary kind (Appendix C.3 numbering).
        return detect_boundary_kind(client) == _int(cond["value"])
    if kind == "flag":
        return read_flag(client, sb1, _int(cond["id"])) is bool(cond.get("set", True))
    if kind == "var":
        return read_var(client, sb1, _int(cond["id"])) == _int(cond["value"])
    raise ReleaseHarnessError(f"unknown until condition kind {kind!r}")


def _flatten_steps(steps):
    """Expand ``repeat`` groups, carrying a group-level ``until`` down to each."""
    out = []
    for step in steps:
        if "repeat" in step:
            group_until = step.get("until")
            for _ in range(int(step["repeat"])):
                for inner in step["steps"]:
                    flat = dict(inner)
                    if group_until is not None and "until" not in flat:
                        flat["until"] = group_until
                    out.append(flat)
        else:
            out.append(dict(step))
    return out


def verify_identity(script, rom_path, worker_path) -> dict:
    """Verify the ROM and worker on disk against the script and the pinned pins.

    Returns the observed identities.  Raises :class:`IdentityMismatch` on any
    difference (ROM, worker, or script-declared pin).
    """
    rom_sha = sha256_file(rom_path)
    worker_sha = sha256_file(worker_path)
    declared_rom = str(script.get("rom_sha256", "")).upper()
    declared_worker = str(script.get("worker_sha256", "")).upper()
    if rom_sha.upper() != PINNED_ROM_SHA256:
        raise IdentityMismatch(f"ROM {rom_path} sha256 {rom_sha} != pinned {PINNED_ROM_SHA256}")
    if worker_sha.upper() != PINNED_WORKER_SHA256:
        raise IdentityMismatch(f"worker {worker_path} sha256 {worker_sha} != pinned {PINNED_WORKER_SHA256}")
    if declared_rom and declared_rom != PINNED_ROM_SHA256:
        raise IdentityMismatch(f"script rom_sha256 {declared_rom} != pinned {PINNED_ROM_SHA256}")
    if declared_worker and declared_worker != PINNED_WORKER_SHA256:
        raise IdentityMismatch(f"script worker_sha256 {declared_worker} != pinned {PINNED_WORKER_SHA256}")
    return {"rom_sha256": rom_sha.upper(), "worker_sha256": worker_sha.upper()}


def save_milestone(client, name, milestones_dir, manifest, frame) -> str:
    """Capture a hashed milestone savestate OUTSIDE git and record it."""
    slot = client.save_checkpoint(name)
    try:
        raw = base64.b64decode(client.export_state(slot["handle"])["blob"], validate=True)
    finally:
        # The worker caps live state slots, so free the slot once the blob is in
        # hand; the file on disk plus its sha256 is the durable record.
        if hasattr(client, "drop_state"):
            client.drop_state(slot["handle"])
    digest = hashlib.sha256(raw).hexdigest()
    milestones_dir.mkdir(parents=True, exist_ok=True)
    (milestones_dir / f"{name}.sdxstate").write_bytes(raw)
    manifest["milestones"][name] = {
        "sha256": digest, "bytes": len(raw), "frame": frame,
        "slot_sha256": slot["sha256"],
    }
    return digest


def load_milestone(client, name, milestones_dir, manifest) -> int:
    """Restore a recorded milestone blob; return its recorded frame."""
    entry = manifest["milestones"][name]
    raw = (milestones_dir / f"{name}.sdxstate").read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise IdentityMismatch(f"milestone {name!r} blob digest changed on disk")
    slot = client.import_state(base64.b64encode(raw).decode(), name=f"resume-{name}")
    client.restore_state(slot["handle"])
    return entry["frame"]


def run_route(client, script, *, milestones_dir, script_path=None, resume_from=None,
              poll_every=4, progress=None) -> dict:
    """Run a condition-driven release route to the first battle root.

    Returns ``{"root_sha256": …, "frames": …, "root_blob": <base64>}`` on
    success and raises :class:`RouteStepError` / :class:`ReleaseRootUnavailable`
    on failure.  Milestone savestates are written to ``milestones_dir`` and a
    ``manifest.json`` records each one's sha256, byte count and frame, plus the
    ROM / worker / script identity it was captured with.
    """
    milestones_dir = Path(milestones_dir)
    milestones_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = milestones_dir / "manifest.json"
    steps = _flatten_steps(script["steps"])

    script_sha = None
    if script_path is not None:
        script_sha = sha256_file(script_path)

    # Resuming: load the prior manifest, verify the script has not changed, and
    # restore the milestone's blob.  Fresh run: start from cold boot.
    manifest = {
        "package": "C",
        "script": script.get("name"),
        "script_sha256": script_sha,
        "rom_sha256": PINNED_ROM_SHA256,
        "worker_sha256": PINNED_WORKER_SHA256,
        "milestones": {},
    }
    start_index = 0
    frames = 0
    if resume_from:
        prior_path = manifest_path
        if not prior_path.is_file():
            raise IdentityMismatch(f"cannot resume {resume_from!r}: no manifest at {prior_path}")
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        if script_sha and prior.get("script_sha256") != script_sha:
            raise IdentityMismatch(
                f"script sha256 {script_sha} != recorded {prior.get('script_sha256')}")
        if resume_from not in prior["milestones"]:
            raise IdentityMismatch(f"milestone {resume_from!r} is not in {prior_path}")
        manifest = prior
        frames = load_milestone(client, resume_from, milestones_dir, prior)
        # Continue at the step AFTER the one that recorded this milestone.
        for i, step in enumerate(steps):
            if step.get("milestone") == resume_from:
                start_index = i + 1
                break
        else:
            raise IdentityMismatch(f"script has no step that records {resume_from!r}")

    reached_battle = False
    for step in steps[start_index:]:
        remaining = int(step["frames"])
        # ``keys`` is normally one mask held (with optional tap/gap mashing).  A
        # list instead advances through a key *sequence* one poll chunk at a
        # time -- needed where a menu wants UP then A on separate frames.
        spec = step["keys"]
        masks = [int(k) for k in spec] if isinstance(spec, (list, tuple)) else None
        tap = step.get("tap")
        gap = int(step.get("gap", 0))
        since_tap = 0
        chunk_index = 0
        keys = masks[0] if masks else int(spec)
        if hasattr(client, "set_keys"):
            client.set_keys(keys)
        met = False
        while remaining > 0:
            if masks is not None and chunk_index > 0:
                keys = masks[chunk_index % len(masks)]
                if hasattr(client, "set_keys"):
                    client.set_keys(keys)
            chunk = min(remaining, poll_every)
            if hasattr(client, "set_keys"):
                client.run_frames(chunk)
            else:
                client.run_frames(chunk, keys=keys)
            frames += chunk
            remaining -= chunk
            chunk_index += 1
            # Battle entry is always a valid early exit, even mid-step.
            if client.read_memory(RELEASE_MAP["gBattleTypeFlags"], 2):
                reached_battle = True
                break
            since_tap += chunk
            if masks is None and tap is not None and since_tap >= tap:
                # Release the key; a mash re-presses after ``gap`` idle frames.
                if hasattr(client, "set_keys"):
                    client.set_keys(0)
                if gap and remaining > 0:
                    idle = min(gap, remaining)
                    if hasattr(client, "set_keys"):
                        client.run_frames(idle)
                    else:
                        client.run_frames(idle, keys=0)
                    frames += idle
                    remaining -= idle
                    if remaining > 0 and hasattr(client, "set_keys"):
                        client.set_keys(keys)
                    since_tap = 0
            if step.get("until"):
                state = read_state(client)
                if eval_condition(client, step["until"], state):
                    met = True
                    break
        state = read_state(client)
        if reached_battle:
            break
        if step.get("until") and not met:
            if progress is not None:
                progress(frames, state)
            raise RouteStepError(step, frames, state)
        if step.get("milestone"):
            save_milestone(client, step["milestone"], milestones_dir, manifest, frames)
            # Persist after every milestone so a later blocked hop still leaves a
            # resumable manifest and the evidence of how far the route got.
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        if progress is not None:
            progress(frames, state)

    # A script may declare how the root settles after gBattleTypeFlags turns
    # nonzero.  `until` is any eval_condition (kind "boundary" waits for the H3
    # decision boundary); optional tap/gap keys keep mashing while it waits.
    if reached_battle and script.get("root_settle"):
        settle = dict(script["root_settle"])
        remaining = int(settle.pop("frames", 1200))
        until = settle.pop("until", None)
        hold = int(settle.pop("keys", 0) or 0)
        tap = int(settle.pop("tap", 8))
        gap = int(settle.pop("gap", 8))

        def settle_done():
            return until is not None and eval_condition(client, until, read_state(client))

        while remaining > 0 and not settle_done():
            if hold and tap:
                client.set_keys(hold)
                step = min(tap, remaining)
                client.run_frames(step)
                frames += step
                remaining -= step
                client.set_keys(0)
                step = min(gap, remaining)
                client.run_frames(step)
                frames += step
                remaining -= step
            else:
                step = min(poll_every, remaining)
                client.set_keys(0)
                client.run_frames(step)
                frames += step
                remaining -= step
        if hasattr(client, "set_keys"):
            client.set_keys(0)

    state = read_state(client)
    if state["battle_type_flags"] == 0:
        raise ReleaseRootUnavailable(frames, state, script.get("name", "?"))

    slot = client.save_checkpoint("release-root")
    raw = base64.b64decode(client.export_state(slot["handle"])["blob"], validate=True)
    root_sha = hashlib.sha256(raw).hexdigest()
    manifest["root"] = {"sha256": root_sha, "bytes": len(raw), "frame": frames,
                        "slot_sha256": slot["sha256"]}
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {"root_sha256": root_sha, "frames": frames, "state": state,
            "root_blob": base64.b64encode(raw).decode()}


def run_input_script(client, script, *, poll_every=4, progress=None):
    """Backward-compatible wrapper: run a fixed-frame or condition script.

    Kept for the small test doubles and any caller that only needs battle
    *entry* detection.  :func:`run_route` is the full milestone-capturing path.
    """
    steps = _flatten_steps(script["steps"])
    detect_addr = _int(script.get("detect", {}).get("gBattleTypeFlags",
                                                    RELEASE_MAP["gBattleTypeFlags"]))
    frames = 0
    for step in steps:
        remaining = int(step["frames"])
        spec = step["keys"]
        masks = [int(k) for k in spec] if isinstance(spec, (list, tuple)) else None
        keys = masks[0] if masks else int(spec)
        if hasattr(client, "set_keys"):
            client.set_keys(keys)
        met = False
        chunk_index = 0
        while remaining > 0:
            if masks is not None and chunk_index > 0:
                keys = masks[chunk_index % len(masks)]
                if hasattr(client, "set_keys"):
                    client.set_keys(keys)
            chunk = min(remaining, poll_every)
            if hasattr(client, "set_keys"):
                client.run_frames(chunk)
            else:
                client.run_frames(chunk, keys=keys)
            frames += chunk
            remaining -= chunk
            chunk_index += 1
            if step.get("until"):
                state = read_state(client)
                if eval_condition(client, step["until"], state):
                    met = True
                    break
            # Battle-entry is always a valid early exit.
            if client.read_memory(detect_addr, 2):
                return frames
        if step.get("until") and not met:
            state = read_state(client)
            raise RouteStepError(step, frames, state)
    state = read_state(client)
    if progress is not None:
        progress(frames, state)
    raise ReleaseRootUnavailable(frames, state, script.get("name", "?"))


def _capture_diagnostics(client) -> dict:
    """Read release-only progress fields for a bounded root-capture report."""
    state = read_state(client)
    return state


# ------------------------------------------------------------------ savestate patch

def header_payload(raw: bytes):
    """Split an exported SDXSTT01 blob into (header, payload)."""
    if raw[:8] != BLOB_MAGIC:
        raise ReleaseHarnessError("exported state blob has an unexpected header magic")
    return raw[:BLOB_HEADER_BYTES], raw[BLOB_HEADER_BYTES:]


def patch_payload(payload: bytes, writes: dict) -> bytes:
    """Patch 32-bit little-endian words at GBA addresses inside a savestate payload.

    EWRAM addresses land at ``SAVESTATE_EWRAM_OFFSET``, IWRAM at
    ``SAVESTATE_IWRAM_OFFSET``; anything else is refused so a caller cannot patch
    the ROM/palette sections by mistake.
    """
    out = bytearray(payload)
    for address, value in writes.items():
        if EWRAM_BASE <= address < EWRAM_BASE + 0x40000:
            offset = SAVESTATE_EWRAM_OFFSET + (address - EWRAM_BASE)
        elif IWRAM_BASE <= address < IWRAM_BASE + 0x8000:
            offset = SAVESTATE_IWRAM_OFFSET + (address - IWRAM_BASE)
        else:
            raise ReleaseHarnessError(f"refusing to patch {address:#010x}: not EWRAM/IWRAM")
        if offset + 4 > len(out):
            raise ReleaseHarnessError(f"patch of {address:#010x} falls outside the payload")
        out[offset:offset + 4] = int(value).to_bytes(4, "little")
    return bytes(out)


def make_blob(raw: bytes, payload: bytes) -> bytes:
    """Rebuild an SDXSTT01 blob with a recomputed payload digest."""
    header = bytearray(raw[:BLOB_HEADER_BYTES])
    header[BLOB_PAYLOAD_SHA_OFFSET:BLOB_PAYLOAD_SHA_OFFSET + 32] = hashlib.sha256(payload).digest()
    return bytes(header) + bytes(payload)


# ------------------------------------------------------------------ projection

def read_projection(client, *, base=None, battlers=2) -> dict:
    """Read the release-side observable projection through the release map."""
    base = RELEASE_MAP["gBattleMons"] if base is None else base
    mons = []
    for battler in range(battlers):
        at = base + battler * BATTLEMON_SIZE
        block = client.read_memory_block(at, BATTLEMON_SIZE)
        mons.append({
            "species": int.from_bytes(block[BATTLEMON["species"]:BATTLEMON["species"] + 2], "little"),
            "level": block[BATTLEMON["level"]],
            "hp": int.from_bytes(block[BATTLEMON["hp"]:BATTLEMON["hp"] + 2], "little"),
            "max_hp": int.from_bytes(block[BATTLEMON["max_hp"]:BATTLEMON["max_hp"] + 2], "little"),
            "status1": int.from_bytes(block[BATTLEMON["status1"]:BATTLEMON["status1"] + 4], "little"),
            "pp": list(block[BATTLEMON["pp"]:BATTLEMON["pp"] + 4]),
        })
    return {
        "mons": mons,
        "outcome": client.read_memory(RELEASE_MAP["gBattleOutcome"], 2),
        "rng_main": int.from_bytes(client.read_memory_block(RELEASE_MAP["gRngValue"], 16), "little"),
        "rng_secondary": int.from_bytes(
            client.read_memory_block(RELEASE_MAP["gRng2Value"], 16), "little"),
        "battle_communication": client.read_memory(RELEASE_MAP["gBattleCommunication"], 4),
    }


def verify_root_projection(client, *, mon_bytes=2 * BATTLEMON_SIZE, party_bytes=6 * 0x64,
                          rng_bytes=16) -> dict:
    """Task-3 release check: read, write back, and re-read at the battle root.

    Reads ``gBattleMons``, the player party, and both RNG streams through the
    release symbol map, writes a deterministic mutation back through the worker's
    memory-write op, re-reads to prove the write landed, then restores the
    original bytes.  Returns the observed round-trip result.
    """
    regions = {
        "gBattleMons": RELEASE_MAP["gBattleMons"],
        "gPlayerParty": RELEASE_MAP["gPlayerParty"],
        "gRngValue": RELEASE_MAP["gRngValue"],
        "gRng2Value": RELEASE_MAP["gRng2Value"],
    }
    sizes = {"gBattleMons": mon_bytes, "gPlayerParty": party_bytes,
             "gRngValue": rng_bytes, "gRng2Value": rng_bytes}
    result = {}
    for name, address in regions.items():
        length = sizes[name]
        original = client.read_memory_block(address, length)
        mutated = bytes((b + 0x5A) & 0xFF for b in original)
        client.write_memory(address, mutated)
        readback = client.read_memory_block(address, length)
        client.write_memory(address, original)
        restored = client.read_memory_block(address, length)
        result[name] = {
            "address": address,
            "bytes": length,
            "write_landed": readback == mutated,
            "restored": restored == original,
        }
    return result


def compare_h2(release: dict, elf: dict, *, label="H2: ") -> list:
    """Compare the release projection against the test ELF's equivalent state."""
    diffs = []
    if release["outcome"] != elf["outcome"]:
        diffs.append(f"{label}outcome release={release['outcome']} elf={elf['outcome']}")
    if release["rng_main"] != elf["rng_main"]:
        diffs.append(f"{label}rng_main release={release['rng_main']} elf={elf['rng_main']}")
    if release["rng_secondary"] != elf["rng_secondary"]:
        diffs.append(
            f"{label}rng_secondary release={release['rng_secondary']} elf={elf['rng_secondary']}")
    for i, (a, b) in enumerate(zip(release["mons"], elf["mons"])):
        for field in ("species", "level", "hp", "max_hp", "status1", "pp"):
            if a[field] != b[field]:
                diffs.append(f"{label}mon[{i}].{field} release={a[field]!r} elf={b[field]!r}")
    if len(release["mons"]) != len(elf["mons"]):
        diffs.append(f"{label}mon count {len(release['mons'])} vs {len(elf['mons'])}")
    return diffs


def identify(path, expected):
    """Return the sha256 of ``path`` and refuse a mismatch against ``expected``."""
    digest = sha256_file(path)
    if digest.upper() != expected.upper():
        raise ReleaseHarnessError(f"{path} sha256 {digest} != pinned {expected}")
    return digest


# =========================================================================== H3
# C2.1 -- release-side boundary detection from named game variables only.


def _ptr(client, address: int) -> int:
    """Read a function pointer and drop the Thumb bit."""
    return client.read_memory(address, 4) & ~1


def boundary_report(client, *, battler: int = 0) -> dict:
    """Detect the release battle's decision boundary from RAM (Appendix C.3).

    Kinds: 0 none, 1 choice, 2 forced switch, 3 terminal.  Every input is a
    named release variable:

    * ``gBattleOutcome`` (EWRAM 0x020006A0) -> terminal.
    * ``gBattleMainFunc`` (IWRAM 0x03004F80) -> ``HandleFaintedMonActions``
      (src/battle_util.c) is the faint/forced-replacement phase.
    * ``gBattlerControllerFuncs[b]`` (IWRAM 0x03004F54) -> the player controller
      is ``HandleInputChooseAction`` (src/battle_controller_player.c, the action
      menu) at a choice boundary and ``PlayerHandleChoosePokemon`` with a
      fainted active mon at a forced-switch boundary.
    * ``gBattleControllerExecFlags`` (EWRAM 0x02000340) is reported as evidence:
      a nonzero bit for the battler means its controller is waiting on input.
    """
    outcome = client.read_memory(RELEASE_MAP["gBattleOutcome"], 2)
    main = _ptr(client, RELEASE_MAP["gBattleMainFunc"])
    ctrl = [_ptr(client, RELEASE_MAP["gBattlerControllerFuncs"] + 4 * i)
            for i in range(MAX_BATTLERS)]
    exec_flags = client.read_memory(RELEASE_MAP["gBattleControllerExecFlags"], 4)
    hp = client.read_memory(
        RELEASE_MAP["gBattleMons"] + battler * BATTLEMON_SIZE + BATTLEMON["hp"], 2)

    kind, reason = BOUNDARY_NONE, "no boundary variable matched"
    if outcome != 0:
        kind, reason = BOUNDARY_TERMINAL, f"gBattleOutcome={outcome}"
    elif main == RELEASE_FUNCS["HandleFaintedMonActions"]:
        kind = BOUNDARY_FORCED_SWITCH
        reason = "gBattleMainFunc=HandleFaintedMonActions"
    elif (ctrl[battler] == RELEASE_FUNCS["PlayerHandleChoosePokemon"] and hp == 0):
        kind = BOUNDARY_FORCED_SWITCH
        reason = ("gBattlerControllerFuncs[%d]=PlayerHandleChoosePokemon and active hp==0"
                  % battler)
    elif (main == RELEASE_FUNCS["HandleTurnActionSelectionState"]
          and ctrl[battler] == RELEASE_FUNCS["HandleInputChooseAction"]):
        kind = BOUNDARY_CHOICE
        reason = ("gBattleMainFunc=HandleTurnActionSelectionState and "
                  "gBattlerControllerFuncs[%d]=HandleInputChooseAction" % battler)

    return {
        "kind": kind,
        "name": BOUNDARY_NAMES[kind],
        "reason": reason,
        "gBattleOutcome": outcome,
        "gBattleOutcomeName": B_OUTCOME_NAMES.get(outcome, f"unknown-{outcome}"),
        "gBattleMainFunc": main,
        "gBattlerControllerFuncs": ctrl,
        "gBattleControllerExecFlags": exec_flags,
        "battler_hp": hp,
    }


def detect_boundary_kind(client, *, battler: int = 0) -> int:
    return boundary_report(client, battler=battler)["kind"]


def terminal_report(client) -> dict:
    """Terminal detail for a finished battle (contract C.3 terminal table)."""
    outcome = client.read_memory(RELEASE_MAP["gBattleOutcome"], 2)
    results = client.read_memory_block(RELEASE_MAP["gBattleResults"], 0x30)
    return {
        "outcome": outcome,
        "outcome_name": B_OUTCOME_NAMES.get(outcome, f"unknown-{outcome}"),
        "player_faints": results[0],
        "opponent_faints": results[1],
        "turn_counter": results[0x13],
    }


# ==================================================================== C2.2/3
# Projection vocabulary: EXACTLY the tables.json field names.

SDR2_VECTORS = TOOLS / "sdr2_vectors"
TABLES_PATH = SDR2_VECTORS / "tables.json"
PROJECTION_GROUPS = ("party", "active", "field", "side", "battle", "rng")

PARTY_ENCRYPTED = (
    "struct Pokemon box substructures (+0x20..0x4F) are PID-scrambled and this build's "
    "PokemonSubstruct bit layout is bespoke (species:11, teraType:5, heldItem:10, "
    "nickname11/12, ivsWerePerfected). The release-side box cipher key is not the vanilla "
    "checksum, so the harness does not decode it and does not invent a value")
A1_FLAG_WORD = (
    "composite flag word packed by the A1 exporter; its bit layout is defined by the A1 "
    "README section 'v9 flag words', which is not present in this worktree")
NOT_ONE_FIELD = "no single release RAM field carries this value (derived or exporter-owned)"
TIMER_NOT_PINNED = (
    "the countdown lives in the source's status-timer storage, which is not pinned in "
    "RELEASE_MAP; adding a guessed offset would invent a value")


def load_tables(path=TABLES_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def projection_fields(group: str, path=TABLES_PATH) -> list:
    return list(load_tables(path)["fields"][group])


def _u16(raw: bytes, off: int) -> int:
    return int.from_bytes(raw[off:off + 2], "little")


def _u32(raw: bytes, off: int) -> int:
    return int.from_bytes(raw[off:off + 4], "little")


def _s8(value: int) -> int:
    return value - 256 if value >= 128 else value


def release_projection(client, *, battlers: int = 2, party_slots: int = 6) -> dict:
    """Project release RAM into the SDR2 group/field vocabulary.

    Returns ``{"party": [...], "active": [...], "field": [...], "side": [...],
    "battle": [...], "rng": [...], "excluded": {group: {field: reason}}}``.
    Every readable field is sourced from a named release address; anything the
    release RAM cannot express 1:1 lands in ``excluded`` with a reason instead
    of a fabricated zero.
    """
    fields = load_tables()["fields"]
    out = {group: [] for group in PROJECTION_GROUPS}
    excluded = {group: {} for group in PROJECTION_GROUPS}

    def record(group, values):
        rec = {}
        for name in fields[group]:
            if name in values and values[name] is not None:
                rec[name] = values[name]
            elif name not in excluded[group]:
                excluded[group][name] = NOT_ONE_FIELD
        out[group].append(rec)
        return rec

    # ----- rng: gRngValue / gRng2Value are struct Sfc32State {a,b,c,ctr}
    main = [int.from_bytes(client.read_memory_block(RELEASE_MAP["gRngValue"], 16)[i * 4:i * 4 + 4], "little")
            for i in range(4)]
    sec = [int.from_bytes(client.read_memory_block(RELEASE_MAP["gRng2Value"], 16)[i * 4:i * 4 + 4], "little")
           for i in range(4)]
    record("rng", dict(zip(fields["rng"], main + sec)))

    # ----- battle
    results = client.read_memory_block(RELEASE_MAP["gBattleResults"], 0x30)
    record("battle", {
        "turn": results[0x13],
        "outcome": client.read_memory(RELEASE_MAP["gBattleOutcome"], 2),
        "type_flags": client.read_memory(RELEASE_MAP["gBattleTypeFlags"], 2),
        "battler_count": client.read_memory(RELEASE_MAP["gBattlersCount"], 1),
    })
    for name in ("ai_flags", "item_count", "item1", "item2", "item3", "item4"):
        excluded["battle"][name] = NOT_ONE_FIELD

    # ----- field (gBattleWeather raw word; gFieldStatuses flag bits)
    field_statuses = client.read_memory(RELEASE_MAP["gFieldStatuses"], 4)
    record("field", {
        "weather": client.read_memory(RELEASE_MAP["gBattleWeather"], 2),
        "terrain": field_statuses & STATUS_FIELD_TERRAIN_MASK,
        "trick_room": (field_statuses & STATUS_FIELD_TRICK_ROOM) and 1 or 0,
    })
    for name in ("weather_timer", "terrain_timer", "trick_room_timer"):
        excluded["field"][name] = TIMER_NOT_PINNED

    # ----- side (gSideStatuses[side] is the source side-status word)
    for side in range(2):
        status = client.read_memory(RELEASE_MAP["gSideStatuses"] + 4 * side, 4)
        record("side", {
            "side": side,
            "screens": status & SIDE_STATUS_SCREEN_ANY,
            "hazards": status & SIDE_STATUS_HAZARDS_ANY,
            "conditions": status & SIDE_STATUS_CONDITIONS_ANY,
        })
    for name in ("hazard_layers", "reflect_timer", "light_screen_timer", "aurora_veil_timer",
                 "mist_timer", "safeguard_timer", "tailwind_timer", "follow_me_timer"):
        excluded["side"][name] = TIMER_NOT_PINNED if name.endswith("_timer") else NOT_ONE_FIELD

    # ----- active (gBattleMons / gBattlerPartyIndexes / gDisableStructs)
    for b in range(battlers):
        base = RELEASE_MAP["gBattleMons"] + b * BATTLEMON_SIZE
        mon = client.read_memory_block(base, BATTLEMON_SIZE)
        disable = client.read_memory_block(RELEASE_MAP["gDisableStructs"] + b * DISABLE_STRUCT_SIZE,
                                           DISABLE_STRUCT_SIZE)
        values = {
            "battler": b,
            "party_slot": client.read_memory(RELEASE_MAP["gBattlerPartyIndexes"] + b, 1),
            "species": _u16(mon, BATTLEMON["species"]),
            "hp": _u16(mon, BATTLEMON["hp"]),
            "max_hp": _u16(mon, BATTLEMON["max_hp"]),
            "status1": _u32(mon, BATTLEMON["status1"]),
            "status2": _u32(mon, BATTLEMON["status2"]),
            "ability": _u16(mon, BATTLEMON["ability"]),
            "item": _u16(mon, BATTLEMON["item"]),
            "type1": mon[BATTLEMON["types"]],
            "type2": mon[BATTLEMON["types"] + 1],
            "type3": mon[BATTLEMON["types"] + 2],
            "move1": _u16(mon, BATTLEMON["moves"]),
            "move2": _u16(mon, BATTLEMON["moves"] + 2),
            "move3": _u16(mon, BATTLEMON["moves"] + 4),
            "move4": _u16(mon, BATTLEMON["moves"] + 6),
            "disabled_move": _u16(disable, DS_DISABLED_MOVE),
            "disable_timer": disable[DS_TIMERS] & 0x0F,
            "encore_timer": disable[DS_TIMERS] >> 4,
            "taunt_timer": disable[DS_TAUNT_TIMER] >> 4,
            "substitute_hp": disable[DS_SUBSTITUTE_HP],
        }
        for i, name in enumerate(STAT_ORDER):
            values[name] = _s8(mon[BATTLEMON["stat_stages"] + i]) - DEFAULT_STAT_STAGE
        for i in range(4):
            values[f"pp{i + 1}"] = mon[BATTLEMON["pp"] + i]
        record("active", values)
    for name in ("form", "status3", "volatile_flags", "protect_flags", "special_status_flags"):
        excluded["active"][name] = A1_FLAG_WORD if name.endswith("_flags") else NOT_ONE_FIELD

    # ----- party (struct Pokemon plaintext tail only)
    for side, (party_sym, count_sym) in enumerate((("gPlayerParty", "gPlayerPartyCount"),
                                                   ("gEnemyParty", "gEnemyPartyCount"))):
        count = client.read_memory(RELEASE_MAP[count_sym], 1)
        for slot in range(party_slots):
            raw = client.read_memory_block(RELEASE_MAP[party_sym] + slot * POKEMON_SIZE,
                                           POKEMON_SIZE)
            present = (raw[POKEMON_BOX_FLAGS] & POKEMON_HAS_SPECIES_BIT) and slot < count
            record("party", {
                "side": side,
                "slot": slot,
                "level": raw[POKEMON_LEVEL] if present else 0,
                "hp": _u16(raw, POKEMON_HP) if present else 0,
                "max_hp": _u16(raw, POKEMON_MAX_HP) if present else 0,
                "status1": _u32(raw, POKEMON_STATUS1) if present else 0,
                "present": 1 if present else 0,
            })
    for name in fields["party"]:
        if name not in ("side", "slot", "level", "hp", "max_hp", "status1", "present"):
            excluded["party"][name] = PARTY_ENCRYPTED

    return {**out, "excluded": excluded}


def compare_projections(release: dict, normal: dict) -> dict:
    """Per-field diff between two projections plus the excluded-field list.

    ``normal`` is a decoded SDR2 record set (e.g. a golden vector's ``records``
    or the test ELF's decode).  Returns ``{"diffs": [...], "excluded": [...]}``
    where each diff names group/index/field and both values, and each excluded
    entry names group/field/reason.  An identical pair yields no diffs.
    """
    diffs = []
    for group in PROJECTION_GROUPS:
        left = release.get(group) or []
        right = normal.get(group) or []
        for index, rec in enumerate(left):
            if index >= len(right):
                diffs.append({"group": group, "index": index, "field": None,
                              "release": "present", "normal": "missing"})
                continue
            other = right[index]
            for field_name, value in rec.items():
                if field_name not in other:
                    diffs.append({"group": group, "index": index, "field": field_name,
                                  "release": value, "normal": "missing"})
                elif other[field_name] != value:
                    diffs.append({"group": group, "index": index, "field": field_name,
                                  "release": value, "normal": other[field_name]})
        for index in range(len(left), len(right)):
            diffs.append({"group": group, "index": index, "field": None,
                          "release": "missing", "normal": "present"})
    excluded = [{"group": group, "field": field_name, "reason": reason}
                for group, entries in sorted((release.get("excluded") or {}).items())
                for field_name, reason in sorted(entries.items())]
    return {"diffs": diffs, "excluded": excluded}


# ------------------------------------------------------------------ C2.2 inject

# scenario mon field -> (BattlePokemon offset, byte width)
INJECT_ACTIVE_FIELDS = (
    ("species", "species", 2), ("level", "level", 1), ("hp", "hp", 2),
    ("max_hp", "max_hp", 2), ("status1", "status1", 4), ("ability", "ability", 2),
    ("item", "item", 2),
)
# scenario "moves" / "pp" arrays -> BattlePokemon move/pp slots
INJECT_ARRAY_FIELDS = (("moves", "moves", 2, 4), ("pp", "pp", 1, 4))

INJECT_REFUSALS = {
    "nature": PARTY_ENCRYPTED,
    "evs": PARTY_ENCRYPTED,
    "ivs": ("BattlePokemon packs the six IVs into 5-bit fields the A1 exporter owns, and the "
            "party substructure that holds them is PID-scrambled; writing a guessed packing "
            "would invent a value"),
    "form": "form is derived from species/personality and has no release RAM field of its own",
    "nickname": "nickname is encoded text in the encrypted box; not written",
    "candidate_action": ("the legal-action / replay record is produced by the ROM hook and is "
                         "not RAM state the harness can write"),
    "replay_turns": "replay turns are root construction, not RAM state",
    "ai_flags": NOT_ONE_FIELD,
}


def _word(value: int, width: int) -> bytes:
    return int(value).to_bytes(width, "little")


def inject_scenario(client, scenario: dict, *, battlers: int = 2) -> dict:
    """Write a release_scenarios row's representable RAM state, then read it back.

    Returns ``{"written": [...], "refused": {field: reason}, "readback": {...},
    "verified": bool}``.  Only the fields the release RAM stores 1:1 (active
    battlers, the plaintext party tail and both RNG streams) are written;
    everything else is refused with a per-field reason rather than invented.
    """
    root = scenario.get("root", scenario)
    written, refused = [], {}

    def refuse(field_name, reason):
        refused[field_name] = reason

    sides = (("player_party", "gPlayerParty", "gPlayerPartyCount"),
             ("opponent_party", "gEnemyParty", "gEnemyPartyCount"))
    actives = [root.get("player_party") or [], root.get("opponent_party") or []]
    for b in range(min(battlers, len(actives))):
        party = actives[b]
        base = RELEASE_MAP["gBattleMons"] + b * BATTLEMON_SIZE
        if not party:
            continue
        mon = party[0]
        for field_name, offset_name, width in INJECT_ACTIVE_FIELDS:
            if field_name not in mon:
                refuse(field_name, "scenario row does not carry this field")
                continue
            client.write_memory(base + BATTLEMON[offset_name], _word(mon[field_name], width))
            written.append({"address": base + BATTLEMON[offset_name], "field": field_name,
                            "value": mon[field_name]})
        for field_name, offset_name, width, slots in INJECT_ARRAY_FIELDS:
            for i in range(slots):
                source = (mon.get(field_name) or [])
                if i >= len(source):
                    continue
                address = base + BATTLEMON[offset_name] + i * width
                client.write_memory(address, _word(source[i], width))
                written.append({"address": address, "field": f"{field_name}{i + 1}",
                                "value": source[i]})
        for field_name in ("nature", "evs", "ivs", "form", "nickname"):
            if field_name in mon:
                refuse(field_name, INJECT_REFUSALS[field_name])
        # plaintext party tail for the matching slot; also mark the slot present so the
        # party group reads it back (the hasSpecies bit and the party count drive it)
        party_sym, count_sym = sides[b][1], sides[b][2]
        raw = bytearray(client.read_memory_block(RELEASE_MAP[party_sym], POKEMON_SIZE))
        raw[POKEMON_BOX_FLAGS] |= POKEMON_HAS_SPECIES_BIT
        raw[POKEMON_LEVEL] = mon.get("level", raw[POKEMON_LEVEL])
        raw[POKEMON_HP:POKEMON_HP + 2] = _word(mon.get("hp", _u16(raw, POKEMON_HP)), 2)
        raw[POKEMON_MAX_HP:POKEMON_MAX_HP + 2] = _word(mon.get("max_hp", _u16(raw, POKEMON_MAX_HP)), 2)
        raw[POKEMON_STATUS1:POKEMON_STATUS1 + 4] = _word(mon.get("status1", _u32(raw, POKEMON_STATUS1)), 4)
        client.write_memory(RELEASE_MAP[party_sym], bytes(raw))
        client.write_memory(RELEASE_MAP[count_sym], bytes([max(1, client.read_memory(RELEASE_MAP[count_sym], 1))]))
        written.append({"address": RELEASE_MAP[party_sym], "field": f"party[{b}] plaintext tail",
                        "value": {"level": raw[POKEMON_LEVEL], "hp": _u16(raw, POKEMON_HP)}})

    for key, symbol in (("rng_main", "gRngValue"), ("rng_secondary", "gRng2Value")):
        words = root.get(key)
        if words is None:
            refuse(key, "scenario row does not carry this field")
            continue
        if len(words) != 4:
            refuse(key, f"expected 4 rng words, got {len(words)}")
            continue
        payload = b"".join(_word(word, 4) for word in words)
        client.write_memory(RELEASE_MAP[symbol], payload)
        written.append({"address": RELEASE_MAP[symbol], "field": key, "value": list(words)})

    for field_name in ("candidate_action", "replay_turns", "ai_flags"):
        if field_name in root:
            refuse(field_name, INJECT_REFUSALS.get(field_name, NOT_ONE_FIELD))

    readback = release_projection(client, battlers=battlers)
    verified = True
    for b in range(min(battlers, len(actives))):
        party = actives[b]
        if not party:
            continue
        mon, got = party[0], readback["active"][b]
        for field_name, _, _ in INJECT_ACTIVE_FIELDS:
            if field_name in mon and field_name in got and got[field_name] != mon[field_name]:
                verified = False
        if "level" in mon:
            # `level` is a party-group field; verify it on the party record's source
            raw = client.read_memory_block(RELEASE_MAP[sides[b][1]], POKEMON_SIZE)
            if raw[POKEMON_LEVEL] != mon["level"]:
                verified = False
        for i in range(4):
            for field_name, wire_prefix in (("moves", "move"), ("pp", "pp")):
                source = (mon.get(field_name) or [])
                if i < len(source) and got.get(f"{wire_prefix}{i + 1}") != source[i]:
                    verified = False
    rng_rec = readback["rng"][0]
    for key, prefix in (("rng_main", "main_"), ("rng_secondary", "sec_")):
        words = root.get(key)
        if not words:
            continue
        for suffix, word in zip(("a", "b", "c", "ctr"), words):
            if rng_rec.get(prefix + suffix) != word:
                verified = False
    for side, entry in enumerate(sides):
        raw = client.read_memory_block(RELEASE_MAP[entry[1]], POKEMON_SIZE)
        rec = readback["party"][side * 6]
        if rec.get("level") != raw[POKEMON_LEVEL] or rec.get("hp") != _u16(raw, POKEMON_HP):
            verified = False
    return {"written": written, "refused": refused, "readback": readback, "verified": verified}
