# Pinned pokeemerald AI oracle

This directory pins the ROM source and carries a deterministic smart-AI battle fixture with test-only trace hooks. The release ROM hash is a build identity. The trace is emitted by the separate upstream test ELF, `pokeemerald-test-headless.elf`; no release-ROM trace parity is claimed. The hook patch compiles only for the test build.

## Pin and build

The source pin is recorded in `pinned_source.json`. Create a full checkout on a Linux filesystem (WSL ext4 is recommended; `/mnt/c` made the upstream suite I/O bound):

```sh
git clone https://github.com/iriv24/pokeemerald-expansion.git /home/$USER/pokeemerald-oracle
git -C /home/$USER/pokeemerald-oracle checkout 2576d0f3f4c861c760f0c55198e37e6d7b91683e
```

Install the upstream build tools required by that commit, including `arm-none-eabi-gcc`, `arm-none-eabi-binutils`, `make`, `git`, `libpng-dev`, `pkg-config`, Go, and the project's native build dependencies. Poryscript is a build-time-only tool. Build the pinned compatible release and install it into this disposable source checkout:

```sh
git clone https://github.com/huderlem/poryscript.git /tmp/poryscript
git -C /tmp/poryscript checkout d9bc56bc84db479dc14c872a5b65e1e773fce69b
cd /tmp/poryscript
go build -o poryscript .
./install.sh /home/$USER/pokeemerald-oracle
```

Build the release ROM before applying the test overlay, then verify its identity:

```sh
cd /home/$USER/pokeemerald-oracle
make -j2
sha256sum pokeemerald.gba
# Expected: 0ccaaad0878a9cbfb36fbefff800e9b6dd66055924b7595038ede1e8771ec5a2
```

The verified build used `arm-none-eabi-gcc 13.2.1` on Ubuntu 24.04. The source Makefile selects its modern build by default. Do not commit generated ROM or ELF files.

## Run the targeted oracle

Run under WSL/Linux with Python 3 and `pytest` available for the parser tests:

```sh
python3 tools/rom_oracle/run_trace.py --source /home/$USER/pokeemerald-oracle
```

The runner requires the exact source commit, refuses unrelated dirty paths, and verifies the source diff against `trace_hooks.patch` before building. It installs the fixture into the disposable checkout, runs upstream `make check` with the single test selected, checks both generated test ELF hashes, executes mGBA headlessly, byte-compares the raw records with `expected_trace.raw.txt`, then compares parsed state with `expected_trace.json`. The selected test must report exactly one pass.

An existing upstream smart-switch test is also a useful regression gate after the overlay is applied:

```sh
cd /home/$USER/pokeemerald-oracle
make -j4 check TESTS="AI_FLAG_SMART_SWITCHING: AI will switch out if it can't deal damage to a mon with Wonder Guard"
```

At the recorded pin this test passes its 2 parametrizations. The broad upstream `make check` was started but stopped after roughly 2,604 status lines; it had unrelated failures and no aggregate result. It is not represented as green by this gate.

## Trace contents and boundary

The fixture runs two turns with Bulbasaur, Charmander, and bench Heatran. Its turn-indexed events include Intimidate, the player's Will-O-Wisp, Charmander's Tackle and HP loss, the AI selecting opponent party slot 1, the player's next move selection, the settled switch-in, and Heatran's subsequent Tackle. The `switch-settled` hook runs immediately after upstream `SwitchInClearSetData`; it records battler 1 as species 485 (Heatran). The upstream party-order mapping reports that active battler as internal party index 0 after selecting slot 1, so the settled species and full party snapshot are included to disambiguate it.

At switch settlement and after the next turn, the trace records all six party slots per side (readable battle fields plus chunked raw `struct Pokemon` bytes), active `BattlePokemon` state (readable combat fields plus raw bytes), `DisableStruct`, `ProtectStruct`, `SpecialStatus`, side timers, field/weather values, and both RNG streams. Raw records are pinned-build byte layouts intended for exact comparisons; their meaning depends on the source commit and toolchain pin. `run_trace.py` compares all 170 newline-delimited records byte-for-byte with the raw capture (SHA-256 `e4051f1eb74a2dc61089c0bf15cf98a5472ad3b985a19f178235dfb98ae38034`) and compares their parsed state with the JSON capture. The generated test ELF and headless ELF hashes are also pinned in `pinned_source.json`.

The trace does not snapshot every global byte or prove that the release ROM follows the same path. It is a deterministic, broad state capture for this bounded test scenario, not a full RAM snapshot, universal replay format, or the complete calculation contract.
