# mGBA worker

This directory contains a small persistent Windows process that embeds libmGBA
0.10.5 (source commit 26b7884bc25a5933960f3cdcd98bac1ae14d42e2). CMake fetches
that exact commit into the ignored build directory. It builds the GBA core only,
without a GUI or battle-specific code.

The process loads a ROM or a pinned instrumented ELF passed by path, resets the
core, and prints a JSONL status record. Requests use **protocol version 2**.

## Requests

    {"protocol":2,"op":"status"}
    {"protocol":2,"op":"run_frames","frames":10}
    {"protocol":2,"op":"run_frames","frames":10,"keys":8}
    {"protocol":2,"op":"set_keys","keys":16}
    {"protocol":2,"op":"reset"}
    {"protocol":2,"op":"read_memory","address":33554432,"width":4}
    {"protocol":2,"op":"read_memory_block","address":33554432,"length":64}
    {"protocol":2,"op":"write_memory","address":33554432,"data":"01020304"}
    {"protocol":2,"op":"save_state","name":"root","artifact_sha256":"<64 hex>"}
    {"protocol":2,"op":"load_state","handle":"<32 hex>","artifact_sha256":"<64 hex>"}
    {"protocol":2,"op":"drop_state","handle":"<32 hex>"}
    {"protocol":2,"op":"list_states"}
    {"protocol":2,"op":"export_state","handle":"<32 hex>","artifact_sha256":"<64 hex>"}
    {"protocol":2,"op":"import_state","blob":"<base64>","name":"ported","artifact_sha256":"<64 hex>"}
    {"protocol":2,"op":"load_state_blob","blob":"<base64>","artifact_sha256":"<64 hex>"}
    {"protocol":2,"op":"batch_turns", ...}
    {"protocol":2,"op":"quit"}

Every reply carries `"protocol":2` and a boolean `ok`. Failures return
`{"protocol":2,"ok":false,"error":"<code>"}`. Error codes are hyphenated and use
the frozen contract section 9 names where one applies: `stale-handle`,
`identity-mismatch`, `deadline-exceeded`, `invalid-request`,
`invalid-frame-count`, `invalid-keys`, `invalid-memory-write`,
`invalid-memory-block`, `unmapped-memory`, `invalid-state-name`,
`state-limit-exceeded`, `state-memory-exceeded`, `save-state-failed`,
`load-state-failed`, `incomplete-state`, `unknown-operation`,
`request-too-large`, and the `batch-*` transport codes.

## Memory

`read_memory` reads any mapped GBA memory and accepts widths 1, 2, or 4.
`read_memory_block` reads 1 to **4096** bytes in one call and returns lowercase
hex `data`; the requested range must stay inside EWRAM
(`0x02000000`-`0x0203ffff`) or IWRAM (`0x03000000`-`0x03007fff`), so a block may
not cross the boundary. `write_memory` accepts 1-2048 bytes encoded as lowercase
hex and writes only to EWRAM or IWRAM; its address and full byte range must
remain inside one of those regions. Requests are handled between frames, so the
core is idle while the bytes are copied. A successful write returns a status
record. The worker does not interpret game memory as a battle state and returns
no damage, AI, probability, or recommendation results.

## Input scripting

`set_keys` sets the active key mask (bit `i` held means `GBA_KEY_* == i`: A=0,
B=1, SELECT=2, START=3, RIGHT=4, LEFT=5, UP=6, DOWN=7, R=8, L=9; the mask is
bounded to `0x3ff`). The mask persists until changed. `run_frames` accepts an
optional `keys` mask that is applied before the run. `reset`, `load_state`,
`load_state_blob`, and `batch_turns` do not clear a previously set mask.

## State slots

`save_state` gains an optional `name` (default `default`) and returns
`{"slot":{"handle","name","bytes","sha256","identity":{...}}}`. A slot is an
immutable in-memory savestate; re-saving a name replaces that slot and mints a
new handle, so the previous handle becomes stale. `load_state` takes an optional
`handle` (default slot when omitted), `drop_state` frees one, and `list_states`
returns the live slots plus `slot_limit` and `memory_limit`.

Bounds are enforced and fail closed:

* at most **3** live slots per worker (`state-limit-exceeded`);
* at most **64 MiB** of total slot bytes per worker (`state-memory-exceeded`);
  for the ~528 KiB savestates we ship, the slot count is the binding limit;
* `SIDEDEX_WORKER_STATE_BYTES_LIMIT` overrides the byte cap at startup for limit
  testing only; the production default is unchanged.

A slot records the identity it was minted under: worker version, mGBA version,
artifact (ROM/ELF) SHA-256, wire protocol, and save-format id
(`mgba-savestate-savedata-rtc-v1`). Restoring under any mismatched identity is
`identity-mismatch`; an unknown or dropped handle is `stale-handle`.

## Portable blobs

`export_state` returns a base64 blob (`blob`, plus `blob_bytes`/`base64_bytes`).
The blob is a 216-byte self-describing header (magic `SDXSTT01`, header and wire
protocol versions, artifact and payload SHA-256, save-format id, worker and mGBA
versions) followed by the raw savestate. Header identity and payload digest are
verified on import, so a corrupt or foreign blob fails closed (`incomplete-state`
or `identity-mismatch`). `import_state` installs a blob as a new named slot
without restoring it; `load_state_blob` restores it directly.

Blobs must fit the existing 1 MiB response ceiling: export rejects a blob whose
base64 form exceeds 900000 characters (`state-limit-exceeded`). A measured
528448-byte savestate exports as a 528664-byte blob / 704888 base64 characters.

**Cost of moving a node:** export reads the slot, hashes ~0.5 MiB, base64-encodes
it, and writes ~0.7 MiB to the pipe; import decodes, hashes, and copies it into a
new memory slot. Measured on this machine: export ~12 ms, import ~9 ms, restore
~0.2 ms. Transfer is therefore ~50x a local restore; route a node to its owner
when the same worker stays alive, and move blobs only across identical workers
(process restart, rebalancing).

## batch_turns

`batch_turns` is the bounded oracle transport. After `save_state` captures the
default ready checkpoint, it restores that checkpoint for each of up to eight
opaque, equal-length mailbox payloads, writes each into EWRAM, and runs until a
completion marker, the frame cap, or the supplied time budget. The reply echoes
the request and artifact identities for bridge validation and contains ordered
trace events per run.

A turn's frame loop stops on, in addition to the existing after-turn + `ready`
path:

* `ROMTRACE|boundary|<kind>|<turn>|<legal-action-count>` -> `stop: "boundary"`;
* `ROMTRACE|terminal|<result>|<reason>` -> `stop: "terminal"`;
* `ROMTRACE|complete|` / after-turn `ROMTRACE|ready|` -> `stop: "turn-complete"`.

Each turn reports `stop` (`boundary`/`terminal`/`turn-complete`, or null when the
frame cap was hit) and the raw `stop_marker`. The slim-trace filter keeps
`boundary|`, `terminal|`, `unrepresentable|`, and `instruction-budget-exceeded|`
records alongside the existing query/action/party/active/RNG/phase/status/faint/
complete/ready records. Mailboxes are capped at 2048 bytes; request, trace,
batch, and deadline limits are checked before the bridge accepts results.

Its Python surface is:

    RomWorkerClient.batch_turns(address, payloads, *, mailbox_bytes,
        engine_elf_sha256, deadline_ms, request_id, max_frames=24000)

It returns `protocol_version: 3`, the request/source/ELF/worker identities,
`turns` with `complete`, `stop`, `stop_marker`, `frames`, and ordered `events`,
plus `elapsed_ms` and `mean_turn_ms`. `deadline_ms` is an absolute
`time.monotonic()` deadline in milliseconds. The bridge enforces it while
waiting; the worker also checks the remaining millisecond budget between frames.
Each call must use the same artifact hash as the ELF loaded into that worker.

## Python client

`rom_bridge.py` exposes `save_checkpoint(name)`, `restore_state(handle)`,
`drop_state(handle)`, `list_states()`, `export_state(handle)`,
`import_state(blob, name)`, `load_state_blob(blob)`,
`read_memory_block(address, length)`, `set_keys(keys)`,
`run_frames(frames, keys)`, and the boundary-aware `batch_turns`. The legacy
`save_state()`/`load_state()` remain for the query path and use the default
slot. Limits are explicit module constants (`MAX_STATE_SLOTS`,
`MAX_STATE_TOTAL_BYTES`, `MAX_MEMORY_BLOCK_BYTES`, `MAX_KEYS_MASK`,
`MAX_BLOB_BASE64_BYTES`, `MAX_BATCH_TURNS`).

## Build

Build the pinned worker with `./native/rom_worker/build.ps1`. Its executable
SHA-256 is pinned in `rom_bridge.WORKER_SHA256`; a changed binary is rejected at
startup. On worker exit or timeout, the bridge returns a bounded error and
terminates the process. Recovery is to construct a new `RomWorkerClient`, run
the fixture's normal boot/mailbox discovery again, and save a new checkpoint;
in-memory slots (including exported-then-imported blobs) do not survive process
exit.

Build on Windows through MSYS2 MinGW64. Install MSYS2, open its shell once, and
run:

    pacman -Syu
    pacman -S --needed mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake mingw-w64-x86_64-ninja mingw-w64-x86_64-libelf

Then build from PowerShell:

    ./native/rom_worker/build.ps1

The output is a native Windows executable at
native/rom_worker/build/mingw/rom_worker.exe. The smoke test used MSYS2 GCC
16.2.0; the linked executable depended only on Windows system DLLs.

Set SIDEDEX_ROM_PATH and SIDEDEX_ROM_WORKER_EXE to run the smoke test against
an existing ROM and compiled worker:

    $env:SIDEDEX_ROM_PATH = 'C:\path\to\game.gba'
    $env:SIDEDEX_ROM_WORKER_EXE = 'C:\path\to\rom_worker.exe'
    python -m unittest test_rom_worker_smoke -v

For the protocol-2 live tests, also set `SIDEDEX_ROM_WORKER_V2_EXE` and
`SIDEDEX_V2_ARTIFACT_PATH` so `test_rom_bridge.py` can exercise named slots,
blob portability, and the error taxonomy against the new binary. The smoke test
skips when the artifacts are absent. It is an emulator lifecycle check only;
passing it does not establish battle-engine parity.

The worker can also load a hash-pinned, instrumented test ELF when given that
artifact explicitly. `run_frames` returns raw `ROMTRACE|` lines emitted through
mGBA's `GBA Debug` logger. This supports comparing fixture transport with the
oracle test; it does not initialize battles in the release ROM or establish
release-ROM battle parity.
