# Release-ROM differential harness — design decision (phase 4)

Orchestrator decision record after reconnaissance. Full reconnaissance transcript:
`C:\Users\Abj1\.pi\agent\sessions\--C--Users-Abj1-Documents-GitHub-Sidedex--\2026-09-28T18-03-28-979Z_baf42ce3-a8e6-47b1-9bb7-d91d5a7d63fa.jsonl`.

## Findings that drive the decision

1. A release-config build of the pinned source exists at `/home/abj1/rom-release-checkout` with
   `pokeemerald.elf` and `pokeemerald.map` (539,735 symbols) from the same build session as the pinned
   `pokeemerald.gba` (`0ccaaad0…`). Symbol addresses are therefore known for the exact pinned release ROM.
2. Cross-map comparison: 48,223 common symbols between the test-runner map and the release map, **zero
   differing addresses in RAM** (`0x02000000-0x0203FFFF`, `0x03000000-0x03007FFF`). Key pins:
   `gBattleMons 0x02000360`, `gBattleStruct 0x02000830`, `gBattleResources 0x0200083c`,
   `gPlayerParty 0x020375f8`, `gEnemyParty 0x02037850`, `gBattleWeather 0x02000754`,
   `gBattleOutcome 0x020006a0`, `gChosenMove 0x020004e8`, `gMain 0x03004210`, `gRngValue 0x0300627c`,
   `gRng2Value 0x0300628c`, `gTasks 0x030063bc`, `gBattleCommunication 0x02000698`.
3. The shipped ROM contains no hook (`grep ROMTRACE` = 0). The patch's call sites compile to no-ops when
   `TESTING=0`; the mailbox lives only in the test runner. A "release + hook" build (`TEST=1`) changes
   `.text/.rodata` and shared RAM windows, so it is **not** the pinned `.gba` and cannot support a
   release-parity claim on its own.
4. mGBA savestate format is documented (`mgba/internal/gba/serialize.h`, magic `0x01000000`, version 7,
   EWRAM at blob offset `0x21000`, IWRAM at `0x19000`, 528,448 B with savedata/RTC). It is **not**
   portable between ROMs (title/id fatal) and cannot be usefully constructed offline from scratch, but
   **patching RAM inside a captured state is trivial**.
5. The worker can already read release RAM (EWRAM and IWRAM verified deterministic over 5000 frames) and
   cannot write IWRAM yet — that is a small additive worker change (package B deliverable).

## Accepted method (approach A + C)

Root capture by input script, then offline RAM patching of the captured savestate, then resolve and read
back a projection:

1. One recorded key timeline drives the release ROM to its first real battle; `save_state` that root; pin
   the root's sha256 in a fixture file (never commit the 528 KB blob — regenerate it from the script).
2. Each fixture reloads the pinned root, applies a RAM patch (party/`gBattleMons` fields, `gRngValue`,
   `gRng2Value`, `ai_flags`, stat stages), resolves one turn, reads the post-turn projection.
3. Compare the test ELF's normal-critical, normal-logic path with the release ROM projection for the
   accepted scenarios only.

Representable on the release ROM: fixed-seed single battles with 1-2 mons per side, arbitrary
species/level/IV/EV/moves/PP/HP/status/item, fixed both RNG streams, fixed stat stages, `aiFlags`; the
first-turn KO regression, normal and forced crit, accuracy miss, priority/speed tie, status/volatile,
terminal win/loss, faint -> forced replacement, and multi-turn branches re-stepped from the root.

Not representable (must stay `compiled-engine` in the label): ordered mechanical event streams,
per-hit/crit-flag records, query mode, pre-choice AI preview, trainer-item AI (ROM trainer tables),
arbitrary AI scripts beyond `aiFlags`, in-turn intermediate observation, `SIDEDEX_LOGIC_ONLY` semantics,
cross-build state transfer, and full-state byte comparison (phase 4's byte comparison holds for the ELF
pair only; the release side is a projection).

## Required verification before any label change

- **H1**: the patch is release-neutral — a scratch `TEST=0` build of the patched source hashes to
  `0ccaaad0…`. If not, attribute the difference before any comparison claim.
- **H2** (crux): a release battle resolves identically to the test ELF from the equivalent injected state
  (poke RNG + party, resolve one turn, compare HP delta and both RNG streams). Failure means stop at
  "compiled-engine".
- **H3**: the pre-choice boundary is host-detectable by polling `gMain.callback2` / `gBattleCommunication`
  with 1-4 frame steps.
- **H4**: offline blob patching round-trips (patch one EWRAM byte at blob offset `0x21000`, reload,
  read back).
- **H5**: worker IWRAM writes are safe (reads already work).
- Mapping self-check: after patch+reload every injected field reads back identical, which separates a
  mapping bug from a genuine engine difference.
- Candidate byte offsets are patchable; document the volatile-byte exclude list explicitly and fail on
  artifacts/hash mismatches instead of passing.

## Deliverables (package E)

`native/rom_worker` ops come from package B (`read_memory_block`, `set_keys`, IWRAM `write_memory`,
`load_state_blob`); no separate repin. E adds `tools/rom_oracle/release_harness.py` with
`release_scenarios.json` and `release_input_script.json`, plus `test_rom_release_harness.py` (offline) and
`test_rom_release_live.py` (skips without artifacts). `pinned_source.json` gains a `release_evidence`
block; no binaries in Git. Live release tests must skip cleanly when artifacts are absent and must not
change the production path.
