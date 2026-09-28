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
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent.parent

# --------------------------------------------------------------- pinned identity

PINNED_SOURCE_SHA = "2576d0f3f4c861c760f0c55198e37e6d7b91683e"
PINNED_ROM_SHA256 = "0CCAAAD0878A9CBFB36FBEFFF800E9B6DD66055924B7595038EDE1E8771EC5A2"
PINNED_ELF_SHA256 = "422ade3e159916f9cc8bc1350ad274201b6af6cb9b9d38c14d073f85e982bc77"
PINNED_WORKER_SHA256 = "0601297E8BED7CA1319DC9BE86A547AA01B16624CE0DE5431093E6B59D7E5122"

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

def load_input_script(path=INPUT_SCRIPT_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_scenarios(path=SCENARIOS_PATH) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
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

    # When the route ended on battle entry, give the battle a bounded chance to
    # finish loading (battlers, and then the action menu) before snapshotting.
    if reached_battle and script.get("root_settle"):
        settle = script["root_settle"]
        condition = {k: v for k, v in settle.items() if k != "frames"}
        condition["kind"] = "mem"
        if hasattr(client, "set_keys"):
            client.set_keys(0)
        remaining = int(settle["frames"])
        while remaining > 0:
            if eval_condition(client, condition, read_state(client)):
                break
            chunk = min(remaining, poll_every)
            if hasattr(client, "set_keys"):
                client.run_frames(chunk)
            else:
                client.run_frames(chunk, keys=0)
            frames += chunk
            remaining -= chunk

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
