"""Release-ROM differential harness (package E, phase 4).

Two halves, both fail-closed:

* **capture** -- :func:`run_input_script` drives the pinned release ROM with a
  recorded key timeline and polls the release map's ``gBattleTypeFlags`` /
  ``gBattleCommunication`` until the first real battle is entered.  When the
  timeline is not yet good enough it raises :class:`ReleaseRootUnavailable`
  with the last observed state; it never fabricates a battle.
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
}

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


def _capture_diagnostics(client) -> dict:
    """Read release-only progress fields for a bounded root-capture report."""
    last = {
        "callback2": hex(client.read_memory(RELEASE_MAP["gMain"] + 4, 4)),
        "battle_type_flags": hex(client.read_memory(RELEASE_MAP["gBattleTypeFlags"], 2)),
        "battle_communication": hex(client.read_memory(RELEASE_MAP["gBattleCommunication"], 4)),
        "map_layout_id": hex(client.read_memory(RELEASE_MAP["gMapHeader"] + 0x12, 2)),
    }
    try:
        last["player_party_count"] = client.read_memory(RELEASE_MAP["gPlayerPartyCount"], 1)
        ptr = int.from_bytes(client.read_memory_block(RELEASE_MAP["gSaveBlock1Ptr"], 4), "little")
        if 0x02000000 <= ptr < 0x02040000:
            block = client.read_memory_block(ptr, 10)
            last["player_position"] = [
                int.from_bytes(block[0:2], "little", signed=True),
                int.from_bytes(block[2:4], "little", signed=True),
            ]
            last["saved_warp"] = list(block[4:10])
    except (AttributeError, ValueError):
        # Small test doubles need not model all release RAM.
        pass
    return last


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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


def run_input_script(client, script, *, poll_every=4, progress=None):
    """Run the key timeline; return the frame count when a battle is entered.

    ``client`` exposes the worker's ``set_keys`` / ``run_frames`` operations and
    memory reads. Test doubles can use the older ``run_frames(..., keys=...)``
    shortcut. Raises :class:`ReleaseRootUnavailable` when the timeline ends
    without ``gBattleTypeFlags != 0``.
    """
    flags_addr = int(script["detect"]["gBattleTypeFlags"], 16)
    frames = 0
    steps = []
    for step in script["steps"]:
        if "repeat" in step:
            steps.extend(step["steps"] * int(step["repeat"]))
        else:
            steps.append(step)
    for step in steps:
        remaining = int(step["frames"])
        keys = int(step["keys"])
        if hasattr(client, "set_keys"):
            client.set_keys(keys)
        while remaining > 0:
            chunk = min(remaining, poll_every)
            if hasattr(client, "set_keys"):
                client.run_frames(chunk)
            else:
                client.run_frames(chunk, keys=keys)
            frames += chunk
            remaining -= chunk
            if client.read_memory(flags_addr, 2):
                return frames
    last = _capture_diagnostics(client)
    if progress is not None:
        progress(frames, last)
    raise ReleaseRootUnavailable(frames, last, script.get("name", "?"))


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
