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

## Wave 2 C result (release root captured)

The cold-boot route is no longer blocked. Every hop after `CB2_Overworld` is now
**condition-driven** (`until` on callback / map group+num / player x,y / flag /
var / `gBattleTypeFlags` / a release-map `mem` read), each hop is frame-bounded,
and every milestone writes a hashed savestate outside Git under
`C:\Users\Abj1\.codex\artifacts\C\milestones\` with its sha256 recorded in
`manifest.json`. `run_route` stops as soon as `gBattleTypeFlags != 0`, then waits
(bounded) for the first battler to load so the root carries live `gBattleMons`.

Result on the pinned ROM (`0ccaaad0…`) and the in-worktree worker
(`0601297e…`):

- root savestate sha256 `55324a88c4d682a62259e7e7acc1a1f94b86282b85e1b7dc9cfc67811db69840`
  at frame **13056**, on Route 101 (map 0/16), `gBattleTypeFlags=4`,
  `gBattleMons[0]=species 4 (Charmander), hp 20, level 5` vs `species 659, hp 16, level 3`.
- **Two fresh cold-boot runs produced the identical root SHA-256.**
- Task 3 round-trip at the root: `gBattleMons`, `gPlayerParty`, `gRngValue` and
  `gRng2Value` were each read, written back, re-read (write landed) and restored.

Route (milestone -> frame): truck-overworld 3036, truck-trigger 4812, littleroot
4852, house-1f 5612, clock-set 6060, tv-done 7292, littleroot-after-tv 7436,
rival-house-1f 7744, rival-house-2f 8172, birch-unhidden 8832,
littleroot-after-rival 9120, lab 9600, birch-touched 10228, starter-obtained
11620, route101 12716, root 13056.

Notes for the next package:
- This Emerald Imperium build diverges from vanilla: `Route101_EventScript_StartBirchRescue`
  and `Route101_EventScript_BirchsBag` are unreferenced dead code. The starter is
  chosen in `NewBirchsLab`, and the north exit is gated on
  `VAR_LITTLEROOT_TOWN_STATE != 0`, which only the lab starter sets.
- `FLAG_HIDE_LITTLEROOT_TOWN_BIRCHS_LAB_BIRCH` is cleared by meeting the rival in
  their bedroom; Birch's object script opens a custom dynmultistack menu that
  holds `sLockFieldControls=1` until it is dismissed with B.
- `include/global.h` SaveBlock1 comments are stale for this build
  (`FREE_EXTRA_SEEN_FLAGS_SAVEBLOCK1 = TRUE`); the authoritative `flags`/`vars`
  offsets in `release_harness.py` were read from the ELF's `FlagGet`/`VarGet`
  disassembly and cross-checked against the truck intro's six `setflag` calls.
- H2 over the R-P1..R-P28 scenario matrix is still `blocked` (the matrix is not
  enumerated); the release root itself is now captured and reproducible.

## Wave 3 C2 result (2026-09-28): H3 boundary detection + projection plumbing

Gate 0 (`fda0361`) and Gate V1 (`codex/gate-v1`) are merged.  Identity now comes
from `rom_identity.py` in every file this package owns (no digest literals); the
JSON inputs carry `<<rom_identity.NAME>>` placeholders that
`release_harness.resolve_identity()` resolves and rejects when unknown.

### H3 release-side boundary detection (no ROM hooks)

Every input is a named release variable; the release map pins are in
`release_harness.RELEASE_MAP`, the function pointers in `RELEASE_FUNCS`.

| Appendix C.3 kind | Release signal | Source |
| --- | --- | --- |
| 3 terminal | `gBattleOutcome != 0` | EWRAM `0x020006A0`; `include/battle.h`, `B_OUTCOME_*` |
| 2 forced switch | `gBattleMainFunc == HandleFaintedMonActions` **or** `gBattlerControllerFuncs[b] == PlayerHandleChoosePokemon` with `gBattleMons[b].hp == 0` | IWRAM `0x03004F80` / `0x03004F54`; `src/battle_util.c`, `src/battle_controller_player.c` |
| 1 choice | `gBattleMainFunc == HandleTurnActionSelectionState` **and** `gBattlerControllerFuncs[b] == HandleInputChooseAction` | IWRAM, same addresses; `src/battle_main.c`, `src/battle_controller_player.c` |
| 0 none | anything else | — |

Evidence also reported: `gBattleControllerExecFlags` (EWRAM `0x02000340`) and
`gBattlerControllerFuncs[0..3]`.  The earlier root was **not** a decision
boundary: at frame 13056 it sat in `DoBattleIntro` with
`Controller_WaitForString`, so the A mash that advanced it was dismissing intro
text.  `root_settle` now mashes A (`keys`/`tap`/`gap`) until the **choice**
boundary and stops there, because the next A opens the move menu.

Live proof (pinned `0ccaaad0…` + worker `1529b4de…`, two fresh cold-boot runs):
root `9fb8096553028b770a9959df29c559bd244a9b18269176d063f145ddf4b3eaaa` at
frame **13236**, Route 101 (map 0/16), `gBattleTypeFlags=4`, `gBattleMainFunc=HandleTurnActionSelectionState`,
`gBattlerControllerFuncs[0]=HandleInputChooseAction` -> **kind 1 (choice)**.
Driving that root with A taps changes the boundary for real: at frame ~14916
`boundary_report` returns **kind 3 (terminal)** with `gBattleOutcome=1` (won),
`gBattleResults.opponentFaintCounter=1`, after 55 taps.
Forced switch (kind 2) is **not producible live**: the captured wild battle has a
single-mon player party, so `test_boundary_detection_kinds` covers kind 2 with
unit-level positive and negative controls instead.

### Projection vocabulary and exclusions

`release_projection()` returns the six requested groups with **exactly** the
`tools/rom_oracle/sdr2_vectors/tables.json` field names plus an `excluded` map.
`compare_projections(release, normal)` returns per-field diffs and the excluded
list with reasons.

Readable (from release RAM, offsets verified against the live root): `rng` 8/8,
`active` 32/37, `battle` 4/10, `field` 3/6, `side` 4/12, `party` 7/32
(side, slot, level, hp, max_hp, status1, present).

Excluded fields and reasons:

| Group | Fields | Reason |
| --- | --- | --- |
| party | species, item, ability, form, nature, move1-4, pp1-4, iv_*, ev_* | `struct Pokemon` box substructures (+0x20..0x4F) are PID-scrambled and this build's `PokemonSubstruct` layout is bespoke (species:11, teraType:5, heldItem:10, nickname11/12, ivsWerePerfected); the release-side cipher key is not the vanilla checksum, so decoding is not implemented and no value is invented |
| active | volatile_flags, protect_flags, special_status_flags | composite flag words packed by the A1 exporter; their bit layout is the A1 README's "v9 flag words", not present in this worktree |
| active | form, status3 | form is derived from species/personality; status3 has no release RAM field |
| battle | ai_flags, item_count, item1-4 | no single release RAM field carries these |
| field, side | weather_timer, terrain_timer, trick_room_timer, reflect/light_screen/aurora_veil/mist/safeguard/tailwind/follow_me timers, hazard_layers | timers live in status-timer storage that is not pinned in `RELEASE_MAP` |

Mapping assumptions to confirm with A1 before H2 runs: `weather` is the raw
`gBattleWeather` word, `terrain`/`trick_room` are masked `gFieldStatuses` bits,
`screens`/`hazards`/`conditions` are masked `gSideStatuses[side]` bits, `turn` is
`gBattleResults.battleTurnCounter`, and `stage_*` subtract `DEFAULT_STAT_STAGE`
(6) from `gBattleMons[b].statStages[]`.

`inject_scenario()` writes a scenario row's active battlers (species, level, hp,
max_hp, status1, ability, item, moves, pp), the party plaintext tail
(level/hp/max_hp/status1 + hasSpecies + party count) and both RNG streams, then
reads them back; nature/evs/ivs/form/nickname/candidate_action/replay_turns/
ai_flags are refused per field with a reason.

### Test tails

```
$ python -m pytest test_rom_release_harness.py -q
25 passed in 0.16s

$ python -m pytest test_rom_release_live.py -q
7 passed, 1 skipped in 24.02s

$ python -m pytest --ignore=work -q
7 failed, 417 passed, 12 skipped, 49 subtests passed in 59.94s
```

The 7 failures are the Gate V1 flip's cross-package fallout, not this package:
`test_rom_bridge.py::ProtocolTests` (5) drive fake workers that still announce
`protocol: 2` while `rom_bridge` now expects wire 3, and
`test_rom_diff_elf.py` (2) still compares against `rom_diff_harness.WORKER_SHA256`
= `0601297e…`.  Both files belong to B/D (`ROM_TRANSITION_FINISH_PLAN.md` §9.3).
Re-running the suite with this package's changes stashed reproduces the same 7
failures at the merge point.

## Wave 3 C2 addendum (2026-09-28): V1 worker f7b1c1e3 + unreadable-field scoping

Gate V1 re-merged (`c57a9fb`, active worker pin `f7b1c1e3…`). `rom_worker.exe`
replaced in the gitignored `native/rom_worker/build/mingw/` (not committed).
Root regenerated twice from the pinned `.gba`:

```
9fb8096553028b770a9959df29c559bd244a9b18269176d063f145ddf4b3eaaa  frame 13236
```

Identical across two fresh cold-boot runs, and **the same SHA as the 1529 root** —
the SDXSTT01 header embeds the ROM artifact hash, the wire protocol and the
worker version string, and `f7b1c1e3` is still wire 3 / `sidedex-mgba-worker/0.3.0`
/ mgba `0.10.5+26b7884`, so nothing in the header changed.
`test_rom_release_harness.py` 25 passed; `test_rom_release_live.py` 7 passed, 1 skipped.

### Fields unreadable by the release projection today, and what it would take

Authority: `/home/abj1/rom-release-checkout` (source tree of the pinned build).

| Group | Fields | Why unreadable now | What it takes |
| --- | --- | --- | --- |
| party | species, item, form, experience, friendship, ppBonuses, nickname11/12, ivsWerePerfected | live inside the box "secure" region (`struct BoxPokemon.secure`, `include/pokemon.h:249`, 48 bytes at party-mon offset **0x20**) which is XOR-encrypted | `struct PokemonSubstruct0` (`include/pokemon.h:130`): `species:11`, `teraType:5`, `heldItem:10`, `experience:21`, `nickname11:8`, `ppBonuses`, `friendship`, `pokeball:6`, `nickname12:8`, `ivsWerePerfected:1` |
| party | move1-4, pp1-4 | same encrypted region | `struct PokemonSubstruct1` (`include/pokemon.h:147`): `moveN:11` + `evolutionTrackerN:5` (u16 each), then `pp1..pp4` as `ppN:7` |
| party | evs (hp/atk/def/spe/spa/spd) | same | `struct PokemonSubstruct2` (`include/pokemon.h:169`): six plain `u8` EVs |
| party | ivs, nature | same | IVs in `struct PokemonSubstruct3` (`include/pokemon.h:185`) as six `uN:5` bitfields (`hpIV`…`spDefenseIV`) plus `dynamaxLevel:4` / `gigantamaxFactor:1`; nature needs A1's rule because this fork carries `hiddenNatureModifier:5` in `BoxPokemon` (`include/pokemon.h:256`) instead of vanilla's `personality % 25` |
| party | (all of the above) | — | **Decryption is cheap and known**: `DecryptBoxMon` (`src/pokemon.c:2549`) does `raw[i] ^= otId; raw[i] ^= personality` over the 12 `u32` words, i.e. the key is `personality ^ otId` — *not* vanilla's checksum-as-key, which is why the C2 attempt with `0xdf3fdf3f` failed. `GetSubstruct` (`src/pokemon.c:2582`) rotates which type sits where via the `personality % 24` switch; each substruct is 12 bytes |
| side | reflect/light_screen/aurora_veil/mist/safeguard/tailwind/follow_me timers, hazard_layers | live outside `gSideStatuses` (the flags word already read) | `struct SideTimer gSideTimers[2]`, EWRAM `0x02000594` (`include/battle.h:1136`, `src/battle_main.c:200`) |
| field | weather_timer, terrain_timer, trick_room_timer | same | `struct FieldTimer gFieldTimers`, EWRAM `0x02000878` (`include/battle.h:1169`, `src/battle_main.c:233`) — sits immediately after `gFieldStatuses` `0x02000874`, which cross-validates the pin |
| battle | ai_flags | not a global | `u32 aiFlags[MAX_BATTLERS_COUNT]` at offset **0x18** of `struct AI_ThinkingStruct` (`include/battle.h:396`), reached through the pointer `gBattleResources` (EWRAM `0x0200083C`, deref) `+ 0x14` (`->ai`, `include/battle.h:440`) |
| battle | item_count, item1-4 | no release-side carrier identified | needs A1's definition of these wire fields first; they are not a single release global |
| active | volatile_flags, protect_flags, special_status_flags | composite words packed by the A1 exporter | A1's "v9 flag word" bit spec, or read `gDisableStructs` `0x020005F4` (0x2C stride) / `gProtectStructs` `0x020006A4` and pick bits explicitly |
| active | form, status3 | form is derived from species/personality; no status3 RAM field | derivation rule (A1) for form; status3 has no release source |

Scoping note: the party table above means party `species/moves/pp/ivs/evs` are
recoverable with ~40 lines (XOR key + one 24-entry order table + the four struct
layouts), which would take the party group from 7/32 to 32/32 readable fields.
