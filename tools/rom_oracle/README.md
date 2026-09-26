# Pinned pokeemerald AI oracle

This directory pins the ROM source and carries a one-turn smart-AI trace fixture plus a small overlay for the upstream battle test runner. The overlay emits test-only trace events; it does not change the release ROM or Sidedex runtime dependencies. The checked-in release ROM hash is a build identity, not evidence that the release ROM itself emits this trace. Trace capture currently uses `pokeemerald-test-headless.elf`.

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

From a Python 3 environment with `pytest` for the unit test, run the oracle under WSL/Linux. The runner refuses any source revision other than the pin, overlays the fixture and trace hooks, runs the selected ROM test, executes mGBA headlessly, and checks the captured trace against `expected_trace.json`:

```sh
python3 tools/rom_oracle/run_trace.py --source /home/$USER/pokeemerald-oracle
```

The generated trace ELF is built by upstream `make check`. The selected test must report exactly 1 passing test. The test-only instrumentation is applied by `trace_hooks.patch` and reports ability popups, chosen move/switch callbacks, HP/status callbacks, a two-active-battler snapshot after the turn, and the main RNG state. No fixed RAM address is exposed by this source hook.

An existing upstream smart-switch test is also a useful regression gate after the overlay is applied:

```sh
cd /home/$USER/pokeemerald-oracle
make -j4 check TESTS="AI_FLAG_SMART_SWITCHING: AI will switch out if it can't deal damage to a mon with Wonder Guard"
```

At the recorded pin this test passes its 2 parametrizations. The broad upstream `make check` was started but stopped after roughly 2,604 status lines; it had unrelated failures and no aggregate result. It is not represented as green by this gate.

## Trace scope

The initial fixed-seed trace uses Bulbasaur versus Charmander with Heatran on the opponent's bench. It records Intimidate, Will-O-Wisp, Tackle, the HP change, and the AI's selected switch to opponent party slot 1. The test runner's post-turn snapshot still shows Charmander active even though the switch callback selected slot 1; the switch selection is a decision event, not proof that the bench member finished entering. The snapshot contains only both active battlers' species, HP, status, ability, Attack stage, moves, and PP, plus the main RNG tuple. It omits full party state, volatile battle state, secondary RNG streams, and post-switch state. This is a deterministic integration slice, not the complete calculation contract or a full replay surface.
