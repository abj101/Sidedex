# mGBA worker feasibility spike

This directory contains a small persistent Windows process that embeds libmGBA
0.10.5 (source commit 26b7884bc25a5933960f3cdcd98bac1ae14d42e). CMake fetches
that exact commit into the ignored build directory. It builds the GBA core only,
without a GUI or battle-specific code.

The process loads a ROM passed by path, resets the core, and prints a JSONL
status record. Requests use protocol version 1:

    {"protocol":1,"op":"status"}
    {"protocol":1,"op":"run_frames","frames":10}
    {"protocol":1,"op":"reset"}
    {"protocol":1,"op":"save_state"}
    {"protocol":1,"op":"load_state"}
    {"protocol":1,"op":"read_memory","address":33554432,"width":4}
    {"protocol":1,"op":"write_memory","address":33554432,"data":"01020304"}
    {"protocol":1,"op":"quit"}

read_memory reads only mapped GBA memory and accepts widths 1, 2, or 4. State
save/load uses mGBA's state serializer and remains in worker memory.
`write_memory` accepts 1–2048 bytes encoded as lowercase hexadecimal and writes
only to EWRAM (`0x02000000` through `0x0203ffff`); its address and full byte
range must remain inside that region. Requests are handled between frames, so
the core is idle while the bytes are copied. A successful write returns a
status record. The worker does not interpret game memory as a battle state and returns no damage, AI,
probability, or recommendation results.

`batch_turns` is the bounded oracle transport. After `save_state` captures the
ready checkpoint before a battle request, it restores that checkpoint for each of up to eight
opaque, equal-length mailbox payloads, writes each into EWRAM, and runs until
`ROMTRACE|complete|`, the frame cap, or the supplied time budget. The reply
contains ordered trace events for each run and echoes the request and artifact
identities for bridge validation. Mailboxes are capped at 2048 bytes; request,
trace, batch, and deadline limits are checked before the bridge accepts results.
Version 0.1.4 keeps only query, action, party, active, RNG, phase, status,
faint, completion, and ready records in batch replies. Direct `run_frames`
still returns the full trace for diagnostics.
Its Python surface is:

    RomWorkerClient.batch_turns(address, payloads, *, mailbox_bytes,
        engine_elf_sha256, deadline_ms, request_id, max_frames=24000)

It returns `protocol_version: 3`, the request/source/ELF/worker identities,
`turns` with `complete`, `frames`, and ordered `events`, plus `elapsed_ms` and
`mean_turn_ms`. `deadline_ms` is an absolute `time.monotonic()` deadline in
milliseconds. The bridge enforces it while waiting; the worker also checks the
remaining millisecond budget between frames. Each call must use the same
artifact hash as the ELF loaded into that worker.

Build the pinned worker with `./native/rom_worker/build.ps1`. Its executable
SHA-256 is pinned in `rom_bridge.WORKER_SHA256`; a changed binary is rejected
at startup. On worker exit or timeout, the bridge returns a bounded error and
terminates the process. Recovery is to construct a new `RomWorkerClient`, run
the fixture's normal boot/mailbox discovery again, and save a new checkpoint;
the old in-memory checkpoint cannot survive process exit.

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

The smoke test skips when either artifact is absent. It is an emulator lifecycle
check only; passing it does not establish battle-engine parity.

The worker can also load a hash-pinned, instrumented test ELF when given that
artifact explicitly. `run_frames` returns raw `ROMTRACE|` lines emitted through
mGBA's `GBA Debug` logger. This supports comparing fixture transport with the
oracle test; it does not initialize battles in the release ROM or establish
release-ROM battle parity.
