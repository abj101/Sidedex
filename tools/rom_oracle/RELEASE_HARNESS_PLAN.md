# Release-ROM differential harness — design decision (phase 4)

Orchestrator decision record after reconnaissance. Full reconnaissance transcript:
`C:\Users\Abj1\.pi\agent\sessions\--C--Users-Abj1-Documents-GitHub-Sidedex--\2026-09-28T18-03-28-979Z_baf42ce3-a8e6-47b1-9bb7-d91d5a7d63fa.jsonl`.

## Findings that drive the decision

1. A release-config build of the pinned source exists at `/home/abj1/rom-release-checkout` with
   `pokeemerald.elf` and `pokeemerald.map` (539,735 symbols) from the same build session as the pinned
   `pokeemerald.gba` (`0ccaaad0…`). Symbol addresses are therefore known for the exact pinned release ROM.
2. Cross-map comparison first reported 48,223 common symbols with equal RAM addresses; **package E corrected this in wave 1**: only the EWRAM battle pins coincide across builds. IWRAM addresses are build-specific (`gMain` 0x03004210 release / 0x030055C4 v8 / 0x03006504 v8b; `gRngValue` 0x0300627C / 0x030062A8 / 0x030071E8) — use the release map's own values for the release ROM and never carry an ELF address across builds. EWRAM pins that do coincide:
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

- **H1 — FAILED (package E wave 1), fix in flight.** A scratch `TEST=0` build of the pinned source plus the patch hashed to `f8746c7bd825…`, not the pinned `0ccaaad0…`: 22 bytes differed at ROM offsets 0x5C092-0x5C0C4 inside `SetControllerToRecordedPlayer` (unguarded recorded-player injection call sites). The patch was therefore NOT release-neutral as delivered; package A is guarding those call sites. No release-parity claim is possible until this passes. (Original hypothesis:) the patch is release-neutral — a scratch `TEST=0` build of the patched source hashes to
  `0ccaaad0…`. If not, attribute the difference before any comparison claim.
- **H2** (crux): a release battle resolves identically to the test ELF from the equivalent injected state
  (poke RNG + party, resolve one turn, compare HP delta and both RNG streams). Failure means stop at
  "compiled-engine".
- **H3 — partial (package E wave 1).** On the test ELF the boundary IS host-detectable: 1-frame polling reaches it at frame 751, `gBattleCommunication[0:8]` flips `0000000000010000` -> `0000000000000000` exactly at the hold, while `gMain.callback2` stays constant (`BattleMainCB2`). On the RELEASE ROM the boundary is **blocked**: no battle-entry script/savestate exists yet and cross-ROM blobs are rejected `identity-mismatch`. (Original hypothesis:) the pre-choice boundary is host-detectable by polling `gMain.callback2` / `gBattleCommunication`
  with 1-4 frame steps.
- **H4**: offline blob patching round-trips (patch one EWRAM byte at blob offset `0x21000`, reload,
  read back).
- **H5**: worker IWRAM writes are safe (reads already work).
- Mapping self-check: after patch+reload every injected field reads back identical, which separates a
  mapping bug from a genuine engine difference.
- **RNG injection caveat (package E wave 1)**: the v8 hold-release path does NOT re-seed `gRngValue`/`gRng2Value` from the mailbox (two different mailbox pairs give identical results), which violates contract §6. Package A is fixing the ROM. Until then the harness injects through the worker's IWRAM write op — an accepted alternative for release-side work.
- Candidate byte offsets are patchable; document the volatile-byte exclude list explicitly and fail on
  artifacts/hash mismatches instead of passing.

## Deliverables (package E)

`native/rom_worker` ops come from package B (`read_memory_block`, `set_keys`, IWRAM `write_memory`,
`load_state_blob`); no separate repin. E adds `tools/rom_oracle/release_harness.py` with
`release_scenarios.json` and `release_input_script.json`, plus `test_rom_release_harness.py` (offline) and
`test_rom_release_live.py` (skips without artifacts). `pinned_source.json` gains a `release_evidence`
block; no binaries in Git. Live release tests must skip cleanly when artifacts are absent and must not
change the production path.

## Wave 1 C result (2026-09-28)

The pinned ROM (`0ccaaad0…`) and worker (`0601297e…`) identities were verified
before live probing. The script now drives the worker through `set_keys`, but
the cold-boot route is still blocked. The reproducible script reaches
`CB2_NamingScreen` (`0x081855a5`, frame 5096), then `CB2_MainMenu`
(`0x0816ead9`, frame 6098), and ends at `CB2_Overworld` (`0x08188e15`, frame
15528). At the end, `gBattleTypeFlags=0`, `gBattleCommunication=0`,
`gPlayerPartyCount=0`, player position is `(2, 2)`, saved warp bytes are
`19 28 ff 00 ff ff` (group 25/map 40), and `gMapHeader.mapLayoutId=0x00ed`.
This is not a battle root and cannot be used for a release comparison.

No compatible release `.sav` or mGBA state was found in the repository,
worktrees, or pinned release/source checkouts. The pinned source notes contain
no alternate save recipe. The root SHA is therefore unset; every release
scenario remains `release_ready: false`, H2 remains `blocked`, and the runtime
label remains `compiled-engine`. H3 release boundary detection also remains
blocked because no battle can be entered. No H2 turn comparison was run.

## Follow-up: external save fallback probe (2026-09-28)

The worker's optional `--save <path>` startup path was used with a 131072-byte
fixture generated from the pinned source's flash layout: sectors 0–27 carry
the correct IDs, signatures (`0x08012025`), checksums, and counters; sectors
28–31 are erased. The worker reported the exact loaded byte count and SHA-256,
and the ROM reported `gSaveFileStatus=SAVE_STATUS_OK` (1). This proves that the
raw `.sav` fallback reaches the unmodified release ROM's save reader.

The fixture's SaveBlock payloads are all zero. The ROM therefore has no
initialized player or party (`gPlayerPartyCount=0`) and cannot serve as a
playable save or battle root. We found no actual compatible `.sav` or
savestate in local worktrees/source checkouts, and public fixture search found
only unrelated stock Emerald or other expansion forks without matching source
revision/ROM provenance. Populating SaveBlock1/2 and party/story state by hand
would manufacture game progress, so the fallback is rejected for H2. The
release root hash stays unset; H2 and release H3 remain blocked, and no turn
comparison was run.
