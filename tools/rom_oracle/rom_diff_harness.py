#!/usr/bin/env python3
"""ELF-pair differential harness for the ROM transition contract (package E).

Runs each fixture's decision-checkpoint recipe with ``mode=normal`` and
``mode=logic-only`` from the *same* saved boundary node and compares the
contract section 2.2 decoded projection, the mechanical event list, the
boundary/terminal record, both RNG streams and the raw state bytes.

This module is self-contained on purpose: it does not import
``tools/rom_oracle/run_dynamic.py`` (owned by another package and edited
concurrently) so the mailbox layout it writes is frozen against contract
Appendix A.  A cross-check against the canonical encoder is recorded as
evidence in ``work/`` rather than baked into the frozen test.

Fixtures are JSON under ``tools/rom_oracle/fixtures/``.  Generated binaries and
savestate blobs never enter Git.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import struct
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
FIXTURES_DIR = TOOLS / "fixtures"
ID_MAP_PATH = TOOLS / "numeric_id_map.json"

MINGW = ROOT / "native" / "rom_worker" / "build" / "mingw"
DEFAULT_V8_ELF = MINGW / "pokeemerald-test-v8.elf"
V8B_ELF = MINGW / "pokeemerald-test-v8b.elf"
HEADLESS_ELF = MINGW / "pokeemerald-test-headless.elf"
DEFAULT_WORKER = MINGW / "rom_worker.exe"

MAGIC = 0x53445831  # SDX1
VERSION_V8 = 8
SUBMITTED = 1
V7_BYTES = 856
MAILBOX_BYTES = 864

OPERATIONS = {"simulate-turn": 0, "query-damage": 1, "query-ai-action": 2}
MODES = {"normal": 0, "logic-only": 1}
POLICIES = {"normal": 0, "item-ability-only": 1}

MAX_FRAMES = 24_000
MAX_SLOTS = 3

# mGBA savestate payload layout (RELEASE_HARNESS_PLAN.md finding 4).
SAVESTATE_EWRAM_OFFSET = 0x21000
SAVESTATE_IWRAM_OFFSET = 0x19000
EWRAM_START = 0x02000000
IWRAM_START = 0x03000000

# EWRAM battle-state windows (gBattleMons/gBattleStruct/gBattleResources/weather and the
# two parties).  Cross-checked at run time against the decoded export.
EWRAM_WINDOWS = (
    (0x02000000, 0x1000),
    (0x02037000, 0x1000),
)
GBATTLEMONS = 0x0200035C
IWRAM_START = 0x03000000
IWRAM_END = 0x03008000

# Mechanical event markers that make up the ordered event list (contract
# section 4 ``events``).  State/projection records are compared separately.
EVENT_MARKERS = (
    "ROMTRACE|action|",
    "ROMTRACE|hp|",
    "ROMTRACE|faint|",
    "ROMTRACE|status|",
    "ROMTRACE|ability|",
    "ROMTRACE|switch-settled|",
    "ROMTRACE|field|",
    "ROMTRACE|crit-policy-change|",
    "ROMTRACE|critical-policy|",
)
PROJECTION_MARKERS = (
    "ROMTRACE|phase|",  # decoded state export + boundary/terminal mirror
    "ROMTRACE|rng|",
    "ROMTRACE|unrepresentable|",
)
# Named presentation/metadata exclusions.  Anything not listed here fails.
EXCLUDE_LIST = (
    {
        "record": "phase|<turn>|state|battle|<...>|requestId",
        "reason": "caller-chosen boundary-hold release token; differs by construction between "
                  "the two mode runs and is not a contract section 2.2 state field",
    },
    {
        "record": "mGBA savestate payload bytes outside the documented RAM windows or the mailbox",
        "reason": "the mGBA savestate serialiser also carries core/CPU timing registers, which "
                  "differ because logic-only advances the battle pump several steps per emulated "
                  "frame; measured 348 differing bytes confined to payload offsets < 0x3000 with "
                  "zero differing bytes in the game-state RAM windows, so the raw/opaque state "
                  "comparison uses the documented RAM windows (gBattleMons/gBattleStruct/parties/"
                  "gRngValue+gRng2Value) and the mailbox-masked payload digest is recorded only",
    },
    {
        "record": "terminal-row post-terminal raw RAM windows",
        "reason": "after a terminal record the machine keeps running presentation/controller "
                  "teardown, so the post-terminal RAM windows are not the decision node and differ "
                  "between modes by construction (measured 2 bytes in the battle window and 128 "
                  "bytes just below gPlayerParty for P27); terminal rows compare the decoded "
                  "projection, terminal result and both RNG streams",
    },
)


class HarnessError(RuntimeError):
    """A differential run or comparison failed."""


class IdentityMismatch(HarnessError):
    """A state token was used under a different identity than it was minted with."""


class InvalidId(ValueError):
    pass


ID_MAP = json.loads(ID_MAP_PATH.read_text(encoding="utf-8"))


def numeric_id(kind: str, source_id):
    values = ID_MAP["ids"][kind]
    if type(source_id) is int and source_id in values.values():
        return source_id
    if isinstance(source_id, str) and source_id in values:
        return values[source_id]
    raise InvalidId(f"invalid-id: unknown {kind} ID {source_id!r}")


# ----------------------------------------------------------------- encoding

def _ints(values, count, maximum):
    if not isinstance(values, (list, tuple)) or len(values) != count or any(
        type(v) is not int or not 0 <= v <= maximum for v in values
    ):
        raise ValueError(f"expected {count} integers from 0 through {maximum}")
    return list(values)


def _action(action):
    if not isinstance(action, dict):
        raise ValueError("invalid-state: expected an action object")
    if action.get("kind") == "move":
        return 0, numeric_id("moves", action["move_id"]), 0
    if action.get("kind") == "switch":
        return 1, 0, _ints([action.get("slot")], 1, 5)[0]
    raise ValueError("invalid-state: action must be a move or switch")


def _ai_flags(request):
    flags = request.get("ai_flags", 0)
    if isinstance(flags, list):
        mapping = ID_MAP["ids"]["ai_flags"]
        unknown = [f for f in flags if f not in mapping]
        if unknown:
            raise InvalidId(f"invalid-id: unknown AI flag {unknown[0]!r}")
        value = 0
        for name in flags:
            value |= mapping[name]
        return value
    return _ints([flags], 1, 0xFFFFFFFF)[0]


def _encode_mon(mon):
    hp, max_hp = mon["hp"], mon["max_hp"]
    if type(hp) is not int or type(max_hp) is not int or not 0 < hp <= max_hp <= 0xFFFF:
        raise ValueError("incomplete-state: HP and MaxHP must be valid positive values")
    core = [numeric_id("species", mon["species"]), mon["level"],
            numeric_id("abilities", mon["ability"]), numeric_id("items", mon["item"]),
            numeric_id("natures", mon["nature"]), hp, max_hp, 0]
    status = numeric_id("statuses", mon["status1"])
    moves = _ints([numeric_id("moves", m) for m in mon["moves"]], 4, 0xFFFF)
    pp = _ints(mon["pp"], 4, 255)
    ivs = _ints(mon["ivs"], 6, 31)
    evs = _ints(mon["evs"], 6, 252)
    if sum(evs) > 510:
        raise ValueError("invalid-state: EV total exceeds 510")
    held = mon.get("held_item_enabled", True)
    if type(held) is not bool:
        raise ValueError("invalid-state: held_item_enabled must be a boolean")
    stages = _ints(mon.get("stat_stages", [6] * 7), 7, 12)
    return struct.pack("<8HI4H4B12HB7B", *core, status, *moves, *pp, *ivs, *evs,
                       int(held), *stages)


def _validate_clear_field(field):
    expected = {"weather": "WEATHER_NONE", "terrain": "TERRAIN_NONE",
                "trick_room": False, "player_screens": 0, "enemy_screens": 0,
                "player_hazards": 0, "enemy_hazards": 0}
    if field is not None and field != expected:
        raise ValueError("incomplete-state: only a verified clear battle-start field is supported")


def encode_request(request, *, operation="simulate-turn", mode="normal",
                   policy="normal", request_id=0):
    """Encode the v8 864-byte mailbox document (contract Appendix A)."""
    if operation not in OPERATIONS:
        raise ValueError(f"invalid-id: unknown operation {operation!r}")
    if mode not in MODES:
        raise ValueError(f"unsupported-mechanic: unknown execution mode {mode!r}")
    if policy not in POLICIES:
        raise ValueError(f"unsupported-policy: unknown critical policy {policy!r}")
    request_id = _ints([request_id], 1, 0xFFFF)[0]
    for side in ("player", "opponent"):
        active = request.get(f"{side}_active_index", 0)
        if type(active) is not int or active != 0:
            raise ValueError(f"incomplete-state: {side} active index must be slot 0")
    _validate_clear_field(request.get("field"))
    kind, move, slot = _action(request["candidate_action"])
    replay = request.get("replay_turns", [])
    if not isinstance(replay, list) or len(replay) > 1:
        raise ValueError("incomplete-state: oracle supports at most one replayed turn")
    player_replay = opponent_replay = (0, 0, 0)
    if replay:
        entry = replay[0]
        player_replay = _action(entry["player_action"])
        opponent_replay = _action(entry["enemy_action"])
    rng = _ints(request["rng_main"], 4, 0xFFFFFFFF) + _ints(request["rng_secondary"], 4, 0xFFFFFFFF)
    sides, counts = [], []
    for side in ("player_party", "opponent_party"):
        party = request[side]
        if not isinstance(party, list) or not 1 <= len(party) <= 6:
            raise ValueError("incomplete-state: oracle supports one to six Pokemon per side")
        counts.append(len(party))
        sides.append(b"".join(_encode_mon(m) for m in party) + b"\0" * ((6 - len(party)) * 64))
    if kind == 1 and slot >= counts[0]:
        raise ValueError("no-legal-actions: candidate switch slot is outside the player party")
    active_slot = player_replay[2] if replay and player_replay[0] == 1 else 0
    if kind == 0 and move not in [numeric_id("moves", m)
                                  for m in request["player_party"][active_slot]["moves"]]:
        raise ValueError("no-legal-actions: candidate move is not in the active player moves")
    second = request.get("second_action")
    if second is None:
        second_kind, second_move, second_slot = 0, 0, 0
    else:
        second_kind, second_move, second_slot = _action(second)
    header = struct.pack("<IHHI4H8I2H", MAGIC, VERSION_V8, SUBMITTED, _ai_flags(request),
                         kind, move, slot, len(replay), *rng, *counts)
    payload = header + b"".join(sides)
    payload += struct.pack("<6H", *player_replay, *opponent_replay)
    payload += struct.pack("<4H", second_kind, second_move, second_slot, 0)
    item_ids = []
    for item_id, count in (request.get("trainer_items") or {}).items():
        if type(count) is not int or count < 0:
            raise ValueError("incomplete-state: trainer item counts must be non-negative integers")
        item_ids.extend([numeric_id("items", item_id)] * count)
    if len(item_ids) > 4:
        raise ValueError("incomplete-state: a trainer can use at most four item slots")
    item_ids += [0] * (4 - len(item_ids))
    payload += struct.pack("<6H", *item_ids, sum(1 for v in item_ids if v), 0)
    payload += struct.pack("<4H", OPERATIONS[operation], MODES[mode], POLICIES[policy], request_id)
    if len(payload) != MAILBOX_BYTES:
        raise HarnessError(f"encoder produced {len(payload)} bytes, expected {MAILBOX_BYTES}")
    return payload


# ------------------------------------------------------------------- session

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DiffSession:
    """One live worker plus a fresh battle brought to its opening boundary."""

    def __init__(self, elf=DEFAULT_V8_ELF, worker=DEFAULT_WORKER):
        import rom_bridge as rb  # local import: tests import this module lazily

        self.elf = Path(elf)
        self.worker_path = Path(worker)
        if not self.elf.is_file():
            raise HarnessError(f"ELF is missing: {self.elf}")
        if not self.worker_path.is_file():
            raise HarnessError(f"worker is missing: {self.worker_path}")
        self.elf_sha = sha256_bytes(self.elf.read_bytes())
        self.worker_sha = sha256_bytes(self.worker_path.read_bytes())
        self.client = rb.RomWorkerClient(
            rom_path=self.elf, expected_artifact_sha256=self.elf_sha,
            worker_path=self.worker_path, expected_worker_sha256=self.worker_sha,
            timeout=120.0)
        self.client.__enter__()
        self.address = None
        self.root_slot = None
        self.root_records = []
        self._rid = 0
        ev, self.ready_frames = self._run_until(["ROMTRACE|ready|"], step=60, max_frames=20_000)
        ready = [e for e in ev if "ROMTRACE|ready|" in e][-1]
        self.address = int(ready.split("|")[2])
        self.mailbox_bytes = int(ready.split("|")[3])

    @classmethod
    def open(cls, **kwargs):
        return cls(**kwargs)

    def reset(self):
        """Reset the core and re-run to the battle opening (new root recipe)."""
        self.client.reset()
        self.root_slot = None
        self.root_records = []
        self.rng_address = None
        self.rng2_address = None
        ev, frames = self._run_until(["ROMTRACE|ready|"], step=60, max_frames=20_000)
        ready = [e for e in ev if "ROMTRACE|ready|" in e][-1]
        self.address = int(ready.split("|")[2])
        return frames

    def close(self):
        self.client.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _run_until(self, prefixes, final=None, step=5, max_frames=MAX_FRAMES):
        events, frames = [], 0
        while frames < max_frames:
            reply = self.client.run_frames(step)
            frames += step
            events.extend(reply["trace_events"])
            if any(e.startswith(p) for p in prefixes for e in events):
                if final is None:
                    return events, frames
                finals = (final,) if isinstance(final, str) else tuple(final)
                if any(f in e for f in finals for e in events):
                    return events, frames
        raise HarnessError(f"no stop marker {prefixes} after {frames} frames")

    def _submit(self, request, *, operation, mode, policy, want, final=None, step=5,
                max_frames=MAX_FRAMES):
        self._rid += 1
        payload = encode_request(request, operation=operation, mode=mode, policy=policy,
                                 request_id=self._rid)
        self.client.write_memory(self.address, payload)
        started = time.monotonic()
        records, frames = self._run_until(want, final=final, step=step, max_frames=max_frames)
        return records, frames, (time.monotonic() - started) * 1000.0

    def build_root(self, request):
        """Run the fixture recipe once and pin the opening boundary node.

        Also performs the mapping self-check: the raw gBattleMons species byte must
        equal the decoded export, and both gRngValue / gRng2Value are located in
        IWRAM by matching the boundary's own RNG record, so a build with a shifted
        IWRAM layout cannot silently skip the injected RNG.
        """
        records, frames, ms = self._submit(
            request, operation="simulate-turn", mode="normal",
            policy=request.get("critical_policy", "normal"),
            want=["ROMTRACE|boundary|"], final="|state|restore-source|")
        self.root_records = records
        self.root_slot = self.client.save_checkpoint(name="diff-root")
        self.root_frames = frames
        self.root_ms = ms
        self._locate_rng(records)
        decoded = _decoded_species(records, 0)
        raw = self.client.read_memory(GBATTLEMONS, 2)
        if decoded is None or raw != decoded:
            raise HarnessError(
                f"mapping self-check failed: gBattleMons[{GBATTLEMONS:#x}]={raw} but decoded "
                f"active species is {decoded}")
        return {"records": records, "frames": frames, "ms": ms,
                "boundary": stop_record(records), "rng": last_rng(records),
                "rng_addresses": [hex(self.rng_address), hex(self.rng2_address)],
                "battlemons_species": raw,
                "slot": {k: self.root_slot[k] for k in ("bytes", "sha256")}}

    def _locate_rng(self, records):
        rng = last_rng(records)
        if rng is None:
            raise HarnessError("boundary did not emit an RNG record")
        fields = [int(v) & 0xFFFFFFFF for v in rng.split("|")[3:11]]
        self.rng_address = self._locate(struct.pack("<4I", *fields[:4]))
        self.rng2_address = self._locate(struct.pack("<4I", *fields[4:]))
        if self.rng_address is None or self.rng2_address is None:
            raise HarnessError(f"could not locate the RNG states in IWRAM for {rng!r}")

    def _locate(self, pattern, block=0x1000):
        for base in range(IWRAM_START, IWRAM_END, block):
            data = self.client.read_memory_block(base, block)
            index = data.find(pattern)
            if index >= 0:
                return base + index
        return None

    def transition(self, request, *, mode, policy, want=None, final=None, inject_rng=None):
        if self.root_slot is None:
            raise HarnessError("build_root must run before transition")
        self.client.restore_state(self.root_slot["handle"])
        if inject_rng is not None:
            if not getattr(self, "rng_address", None):
                raise HarnessError("RNG addresses were not located (build_root first)")
            # The v8 hold-release path does not re-seed from the mailbox (verified), so
            # the contract section 6 RNG injection is performed through the worker's
            # IWRAM write op, exactly as a real token restore + inject would.
            self.client.write_memory(self.rng_address, struct.pack("<4I", *inject_rng[0]))
            self.client.write_memory(self.rng2_address, struct.pack("<4I", *inject_rng[1]))
        stop = want or ["ROMTRACE|boundary|", "ROMTRACE|terminal|"]
        fin = final or ("|state|restore-source|", "ROMTRACE|complete|")
        records, frames, ms = self._submit(request, operation="simulate-turn", mode=mode,
                                           policy=policy, want=stop, final=fin)
        return {"records": records, "frames": frames, "ms": ms,
                "stop": stop_record(records), "rng": last_rng(records)}

    def slot_digest(self):
        """Save the current boundary into a slot, return payload sha + metadata."""
        slot = self.client.save_checkpoint(name="diff-branch")
        exported = self.client.export_state(slot["handle"])
        self.client.drop_state(slot["handle"])
        payload = _blob_payload(exported["blob"])
        masked = _mask_mailbox(payload, self.address)
        return {
            "bytes": slot["bytes"],
            "slot_sha256": slot["sha256"],
            "blob_sha256": exported["sha256"],
            "masked_sha256": sha256_bytes(masked),
            "ram_windows_sha256": self.ram_windows_sha256(),
        }

    def ram_windows_sha256(self):
        digest = hashlib.sha256()
        for address, length in self.ram_windows():
            digest.update(self.client.read_memory_block(address, length))
        return digest.hexdigest()

    def ram_windows(self):
        windows = list(EWRAM_WINDOWS)
        if getattr(self, "rng_address", None):
            windows.append((self.rng_address & ~0xF, 0x40))
        return tuple(windows)

    def raw_state(self):
        return {"ram_windows_sha256": self.ram_windows_sha256(),
                "rng_address": hex(getattr(self, "rng_address", 0)),
                "battlemons_species": self.client.read_memory(GBATTLEMONS, 2)}


def _blob_payload(blob_b64):
    import base64
    raw = base64.b64decode(blob_b64, validate=True)
    # Worker blob header (native/rom_worker/worker.c BLOB_HEADER_BYTES = 216):
    # magic[8] "SDXSTT01", header_version, wire_protocol, payload_bytes,
    # artifact[32], payload_sha256[32], save_format[64], worker[32], mgba[32].
    if raw[:8] != b"SDXSTT01":
        raise HarnessError("exported state blob has an unexpected header magic")
    return raw[216:]


def _mask_mailbox(payload, mailbox_address):
    offset = SAVESTATE_EWRAM_OFFSET + (mailbox_address - EWRAM_START)
    masked = bytearray(payload)
    end = offset + MAILBOX_BYTES
    if end > len(masked):
        raise HarnessError("mailbox range falls outside the savestate payload")
    masked[offset:end] = b"\0" * MAILBOX_BYTES
    return bytes(masked)


# --------------------------------------------------------------- comparison

def stop_record(records):
    for record in records:
        if record.startswith("ROMTRACE|boundary|") or record.startswith("ROMTRACE|terminal|"):
            return record
    return None


def last_rng(records):
    rng = [r for r in records if r.startswith("ROMTRACE|rng|")]
    return rng[-1] if rng else None


def _decoded_species(records, battler):
    for record in records:
        if "|state|active|" in record:
            fields = record.split("|")
            if fields[5] == str(battler):
                return int(fields[7])
    return None


def event_list(records):
    return [r for r in records if any(r.startswith(m) for m in EVENT_MARKERS)]


def projection(records):
    """Decoded projection + RNG records; requestId is excluded by name."""
    out = []
    for r in records:
        if not any(r.startswith(m) for m in PROJECTION_MARKERS):
            continue
        if "|state|battle|" in r:
            fields = r.split("|")
            fields[-1] = "<requestId>"
            r = "|".join(fields)
        out.append(r)
    return out


def compare(normal, logic, *, label="", assert_raw=True):
    """Return the list of differences between two mode runs ([] means equal).

    ``assert_raw`` is False for terminal rows, where post-terminal frames keep
    advancing presentation/controller teardown that is not a decision node; the
    raw window digests are then recorded but not asserted (named in EXCLUDE_LIST).
    """
    diffs = []
    for name, getter in (("projection", projection), ("events", event_list)):
        left, right = getter(normal["records"]), getter(logic["records"])
        if left != right:
            for i, (a, b) in enumerate(zip(left, right)):
                if a != b:
                    diffs.append(f"{label}{name}[{i}]: normal={a!r} logic={b!r}")
            if len(left) != len(right):
                diffs.append(f"{label}{name}: length {len(left)} vs {len(right)}")
    if normal["stop"] != logic["stop"]:
        diffs.append(f"{label}stop: normal={normal['stop']!r} logic={logic['stop']!r}")
    if normal["rng"] != logic["rng"]:
        diffs.append(f"{label}rng: normal={normal['rng']!r} logic={logic['rng']!r}")
    if assert_raw and normal["state"].get("ram_windows_sha256") != logic["state"].get("ram_windows_sha256"):
        diffs.append(f"{label}state.ram_windows_sha256: normal="
                     f"{normal['state'].get('ram_windows_sha256')} logic="
                     f"{logic['state'].get('ram_windows_sha256')}")
    return diffs


# ------------------------------------------------------------- fixture runs

def load_fixtures(only=None):
    fixtures = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") == "unsupported":
            fixtures.append((path, data))
            continue
        if only and data.get("id") not in only:
            continue
        fixtures.append((path, data))
    return fixtures


def run_fixture(session, fixture):
    """Run one fixture's root + every action under both modes and compare.

    The caller must pass a session that has just been opened (a fresh worker
    process); cross-root reuse through ``reset`` is not relied on because the
    in-ROM test runner keeps process-lifetime statics.
    """
    root_info = session.build_root(copy.deepcopy(fixture["root"]))
    actions = fixture.get("actions") or [{"action": fixture.get("action"),
                                          "label": fixture.get("id", "action")}]
    policy = fixture.get("critical_policy", "normal")
    inject = fixture.get("inject_rng")
    inject = ([tuple(inject["main"]), tuple(inject["secondary"])] if inject else None)
    results = []
    for entry in actions:
        branch = copy.deepcopy(fixture["root"])
        branch["candidate_action"] = entry["action"]
        for key in ("rng_main", "rng_secondary"):
            if key in fixture:
                branch[key] = fixture[key]
        entry_inject = inject
        if entry.get("inject_rng"):
            item = entry["inject_rng"]
            entry_inject = [tuple(item["main"]), tuple(item["secondary"])]
        normal = session.transition(branch, mode="normal", policy=policy, inject_rng=entry_inject)
        normal["state"] = session.slot_digest()
        logic = session.transition(branch, mode="logic-only", policy=policy, inject_rng=entry_inject)
        logic["state"] = session.slot_digest()
        diffs = compare(normal, logic, label=f"{entry['label']}: ",
                        assert_raw=fixture.get("assert_raw_state", True))
        results.append({
            "label": entry["label"],
            "action": entry["action"],
            "expected": entry.get("expect"),
            "normal": normal,
            "logic": logic,
            "diffs": diffs,
        })
    return {"id": fixture.get("id"), "policy": policy, "root": root_info, "actions": results}


def evidence(fixture, run, *, elf_sha, worker_sha):
    """Contract evidence block for a green row."""
    blocks = []
    for result in run["actions"]:
        normal, logic = result["normal"], result["logic"]
        blocks.append({
            "label": result["label"],
            "player_action": result["action"],
            "mode_pair": ["normal", "logic-only"],
            "critical_policy": run["policy"],
            "projection_digest": sha256_bytes("\n".join(projection(normal["records"])).encode()),
            "event_list": event_list(normal["records"]),
            "rng": {"before": last_rng(run["root"]["records"]), "after": normal["rng"]},
            "stop": normal["stop"],
            "frames": {"normal": normal["frames"], "logic_only": logic["frames"]},
            "elapsed_ms": {"normal": round(normal["ms"], 1), "logic_only": round(logic["ms"], 1)},
            "raw_state_sha256": normal["state"]["masked_sha256"],
            "ram_windows_sha256": normal["state"]["ram_windows_sha256"],
            "gate": {"elf_sha256": elf_sha, "worker_sha256": worker_sha,
                     "wire_protocol": 2, "state_schema": "sidedex-state-v1",
                     "rom_hook_protocol": 8, "mode": "normal|logic-only",
                     "critical_policy": run["policy"]},
            "regenerate": f"python -m pytest test_rom_diff_elf.py -q -k {fixture['id']}",
        })
    return {"fixture": fixture["id"], "root_recipe": run["root"], "rows": blocks,
            "exclusions": list(EXCLUDE_LIST)}


def resolve_artifacts():
    elf = Path(os.environ.get("SIDEDEX_DIFF_ELF", DEFAULT_V8_ELF))
    worker = Path(os.environ.get("SIDEDEX_ROM_WORKER_EXE", DEFAULT_WORKER))
    return elf, worker


# ----------------------------------------------------- raw worker + identity

class RawWorker:
    """Minimal raw JSONL client used to probe worker-level identity checks."""

    def __init__(self, elf=DEFAULT_V8_ELF, worker=DEFAULT_WORKER):
        import subprocess
        self.proc = subprocess.Popen(
            [str(worker), "--rom", str(elf)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.status = json.loads(self.proc.stdout.readline())

    def send(self, **fields):
        request = {"protocol": fields.pop("protocol", 2), **fields}
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def close(self):
        try:
            self.proc.stdin.write(json.dumps({"protocol": 2, "op": "quit"}) + "\n")
            self.proc.stdin.flush()
            self.proc.stdout.readline()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


TOKEN_IDENTITY_FIELDS = ("source_sha", "artifact_sha256", "worker_sha256", "wire_protocol",
                         "rom_hook_protocol", "state_schema", "mode", "critical_policy")


def mint_token_identity(**fields):
    """The contract section 1 TransitionIdentity subset carried by a token."""
    missing = [name for name in TOKEN_IDENTITY_FIELDS if name not in fields]
    if missing:
        raise ValueError(f"missing token identity fields: {missing}")
    return {name: fields[name] for name in TOKEN_IDENTITY_FIELDS}


def check_token_identity(identity, **requested):
    """Fail closed when a token is used under a different identity."""
    for name, value in requested.items():
        if name not in identity:
            raise ValueError(f"unknown token identity field {name!r}")
        if identity[name] != value:
            raise IdentityMismatch(
                f"identity-mismatch: token minted with {name}={identity[name]!r}, requested {value!r}")
    return identity
