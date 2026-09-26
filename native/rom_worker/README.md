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
    {"protocol":1,"op":"quit"}

read_memory reads only mapped GBA memory and accepts widths 1, 2, or 4. State
save/load uses mGBA's state serializer and remains in worker memory. The worker
does not interpret game memory as a battle state and returns no damage, AI,
probability, or recommendation results.

Build on Windows through MSYS2 MinGW64. Install MSYS2, open its shell once, and
run:

    pacman -Syu
    pacman -S --needed mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake mingw-w64-x86_64-ninja

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
