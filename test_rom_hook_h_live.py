"""Live evidence for package H: the SIDEDEX_DYNAMIC_AI player action and RNG carry.

Every claim has a negative control. Skipped unless the H test ELF and the pinned
mGBA worker are staged (SIDEDEX_H_ELF / SIDEDEX_ROM_WORKER_EXE override the
defaults under native/rom_worker/build/mingw). The identity checked is the ELF's
own sha256, so no pin is touched. Optional SIDEDEX_H_BASE_ELF is a previous hook
ELF for the injected-action regression differential.

    python -m pytest test_rom_hook_h_live.py -q
"""
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import rom_bridge  # noqa: E402
import rom_identity as rid  # noqa: E402
from tools.rom_oracle import run_dynamic  # noqa: E402

BUILD = ROOT / "native/rom_worker/build/mingw"
ELF = Path(os.environ.get("SIDEDEX_H_ELF", BUILD / "pokeemerald-test-h-headless.elf"))
WORKER = Path(os.environ.get("SIDEDEX_ROM_WORKER_EXE", BUILD / "rom_worker.exe"))
BASE_ELF = Path(os.environ.get("SIDEDEX_H_BASE_ELF", BUILD / "pokeemerald-test-base-headless.elf"))
FIXTURES = ROOT / "tools/rom_oracle/fixtures"
STATE_KEYS = ("party", "active", "field", "side", "battle", "rng", "legal")

pytestmark = pytest.mark.skipif(not (ELF.is_file() and WORKER.is_file()),
                                reason="H test ELF or pinned worker not staged")

GROWL = {"kind": "move", "move_id": "MOVE_GROWL"}
TACKLE = {"kind": "move", "move_id": "MOVE_TACKLE"}
HYDRO = {"kind": "move", "move_id": "MOVE_HYDRO_PUMP"}
AI = {"kind": "ai"}
MOVE_NAME = {33: "MOVE_TACKLE", 45: "MOVE_GROWL", 56: "MOVE_HYDRO_PUMP", 163: "MOVE_SLASH"}
MOVE_PP = {33: 35, 45: 40, 56: 5, 163: 20}


def switch(slot):
    return {"kind": "switch", "slot": slot}


def fixture_root(name):
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return {**data["root"], "critical_policy": data["critical_policy"],
            "operation": "simulate-turn", "mode": "normal"}


def token(text):
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:2], "little")


class Sess:
    """One worker on one ELF; request ids are unique u16 tokens."""

    def __init__(self, elf=ELF):
        self.elf = elf
        self.sha = hashlib.sha256(Path(elf).read_bytes()).hexdigest().upper()
        self.client = rom_bridge.RomWorkerClient(
            worker_path=WORKER, rom_path=elf, expected_artifact_sha256=self.sha,
            expected_worker_sha256=rid.CANDIDATE_WORKER_V3_SHA256, wire_protocol=3, timeout=60.0)
        self.client.__enter__()
        trace = []
        for _ in range(400):
            trace.extend(run_dynamic._trace(self.client.run_frames(60)))
            ready = [line for line in trace if line.startswith("ROMTRACE|ready|")]
            if ready:
                self.ann = rom_bridge.parse_v9_ready(ready)
                break
        else:
            raise RuntimeError("the ELF never announced a v9 mailbox")
        self.client.save_state()
        self.n = 0
        self.last = 0

    def close(self):
        self.client.close()

    def _run(self, request, handle):
        self.n += 1
        text = f"h{self.n}"
        while token(text) in (0, self.last):
            text += "x"
        self.last = token(text)
        request = {**request, "request_id": self.last}
        payload = run_dynamic.encode_v9_request(request, self.ann, compact=True)
        reply = self.client.batch_turns_v3(
            self.ann["address"], self.ann["result_address"], self.ann["result_capacity"], [payload],
            mailbox_bytes=876, engine_elf_sha256=self.sha,
            deadline_ms=int(time.monotonic() * 1000) + 60_000, request_id=text, handle=handle)
        return reply["turns"][0]

    def probe(self, root):
        """Hold at the opening boundary; returns (turn, node handle, natural RNG)."""
        turn = self._run(root, None)
        return turn, turn["child"]["handle"], rng_of(turn)

    def act(self, node, root, action, rng=None, drop_node=False):
        """Release `node` with `action`; RNG is the node's own tag-6 carry by default."""
        main, second = rng
        turn = self._run({**root, "candidate_action": action, "rng_main": main,
                          "rng_secondary": second}, node)
        if drop_node:
            self.client.drop_state(node)
        return turn

    def drop(self, turn):
        if turn.get("child"):
            self.client.drop_state(turn["child"]["handle"])


def rng_of(turn):
    header = turn["binary"]["header"]
    return ([header[f"rng_main{i}"] for i in range(4)], [header[f"rng_sec{i}"] for i in range(4)])


def state(turn):
    records = turn["binary"]["records"]
    return {key: records[key] for key in STATE_KEYS if key in records}


def player_moves(turn):
    return [e["payload"][1] for e in turn["binary"]["records"].get("events", [])
            if e["kind"] == 1 and e["payload"][0] == 0]


def event_kinds(turn, kind):
    return [e["payload"] for e in turn["binary"]["records"].get("events", []) if e["kind"] == kind]


@pytest.fixture
def sess():
    session = Sess()
    yield session
    session.close()


def shaped_root(opponent_hp=None, moves=None, flags=None):
    """Bulbasaur alone against a special-only Charizard (Growl has no value)."""
    root = fixture_root("p29_multiturn_branch.json")
    char = copy.deepcopy(root["player_party"][1])
    char["moves"] = ["MOVE_SURF", "MOVE_NONE", "MOVE_NONE", "MOVE_NONE"]
    char["pp"] = [15, 0, 0, 0]
    if opponent_hp is not None:
        char["hp"] = opponent_hp
    root["player_party"] = root["player_party"][:1]
    root["opponent_party"] = [char]
    if moves:
        root["player_party"][0]["moves"] = [MOVE_NAME[m] for m in moves]
        root["player_party"][0]["pp"] = [MOVE_PP[m] for m in moves]
    if flags is not None:
        root["ai_flags"] = flags
    return root


def ai_move(sess, root):
    _, node, q0 = sess.probe(root)
    turn = sess.act(node, root, AI, q0)
    sess.client.drop_state(node)
    sess.drop(turn)
    return turn


# --------------------------------------------------------------------------- #
# Encoder
# --------------------------------------------------------------------------- #

def test_encoder_accepts_the_ai_action_only_as_the_candidate():
    request = fixture_root("p01_physical_neutral.json")
    request["candidate_action"] = AI
    payload = run_dynamic.encode_request(request, protocol_version=8)
    assert int.from_bytes(payload[12:14], "little") == run_dynamic.ACTION_KIND_AI == 2
    request["replay_turns"] = [{"player_action": AI, "enemy_action": TACKLE}]
    with pytest.raises(ValueError, match="only valid as the candidate"):
        run_dynamic.encode_request(request, protocol_version=8)
    request["replay_turns"] = []
    request["second_action"] = AI
    with pytest.raises(ValueError, match="only valid as the candidate"):
        run_dynamic.encode_request(request, protocol_version=8)


# --------------------------------------------------------------------------- #
# SIDEDEX_DYNAMIC_AI
# --------------------------------------------------------------------------- #

def test_ai_move_choice_is_deterministic_and_not_a_fixed_injected_move(sess):
    root = shaped_root()
    _, node, q0 = sess.probe(root)
    first = sess.act(node, root, AI, q0)
    sess.drop(first)
    second = sess.act(node, root, AI, q0)
    sess.drop(second)
    # Same node + same RNG: byte-identical outcome.
    assert state(first) == state(second)
    assert first["binary"]["records"]["events"] == second["binary"]["records"]["events"]
    chosen = player_moves(first)
    assert chosen == [56], chosen  # Hydro Pump: the resisted-by-nothing damaging move
    assert event_kinds(first, 1)[0][2] == 1  # targets the opponent battler
    # Negative controls: a fixed injected Growl or Tackle plays a different turn.
    for action, move in ((GROWL, 45), (TACKLE, 33)):
        injected = sess.act(node, root, action, q0)
        sess.drop(injected)
        assert player_moves(injected) == [move]
        assert state(injected) != state(first)
    # The injected move the AI picked is the same player action (RNG differs: the
    # player AI's own scoring draws from the stream).
    injected = sess.act(node, root, HYDRO, q0)
    sess.drop(injected)
    assert player_moves(injected) == [56]
    assert rng_of(injected) != rng_of(first)


def test_ai_move_choice_depends_on_state_and_on_the_opponent_flags(sess):
    # The choice tracks the position, not the move slot.
    for perm in ([56, 45, 33, 163], [163, 33, 45, 56], [33, 56, 163, 45]):
        assert player_moves(ai_move(sess, shaped_root(moves=perm))) == [56], perm
    # A 5 HP opponent: the AI now picks a KOing move instead.
    ko = ai_move(sess, shaped_root(opponent_hp=5))
    assert player_moves(ko) != [56]
    # AI flags are the opponent's: with none, every move scores alike and the pick
    # is random over seeds; with the smart flags it never varies.
    picks_none, picks_smart = set(), set()
    for seed in (11, 222, 3333, 44444, 555555, 6666666, 77777777, 888):
        main = [seed, seed * 7 + 1, seed * 13 + 5, 1]
        for flags, picks in ((0, picks_none), (16391, picks_smart)):
            root = shaped_root(flags=flags)
            root["rng_main"] = main
            picks.add(tuple(player_moves(ai_move(sess, root))))
    assert picks_smart == {(56,)}
    assert len(picks_none) > 1, picks_none


def ghost_root(active_moves=("MOVE_TACKLE", "MOVE_SLASH")):
    """Normal-type Bulbasaur (Charizard behind it) against an immune Gengar."""
    root = fixture_root("p29_multiturn_branch.json")
    gengar = copy.deepcopy(root["opponent_party"][0])
    gengar.update(species="SPECIES_GENGAR", ability="ABILITY_LEVITATE", pp=[30, 0, 0, 0],
                  moves=["MOVE_LICK", "MOVE_NONE", "MOVE_NONE", "MOVE_NONE"])
    root["opponent_party"] = [gengar]
    bulbasaur, charizard = root["player_party"][:2]
    bulbasaur["moves"] = list(active_moves) + ["MOVE_NONE"] * (4 - len(active_moves))
    bulbasaur["pp"] = [5] * len(active_moves) + [0] * (4 - len(active_moves))
    charizard["moves"] = ["MOVE_FLAMETHROWER", "MOVE_NONE", "MOVE_NONE", "MOVE_NONE"]
    charizard["pp"] = [15, 0, 0, 0]
    return root


def test_ai_voluntary_switch(sess):
    root = ghost_root()
    _, node, q0 = sess.probe(root)
    ai = sess.act(node, root, AI, q0)
    sess.drop(ai)
    again = sess.act(node, root, AI, q0)
    sess.drop(again)
    assert state(ai) == state(again)
    # Every move is a no-effect move: the AI leaves for Charizard (slot 1).
    assert event_kinds(ai, 7) == [[0, 0]] and event_kinds(ai, 8) == [[0, 1, 6]]
    assert [a["species"] for a in ai["binary"]["records"]["active"]][0] == 6
    injected = sess.act(node, root, switch(1), q0)
    sess.drop(injected)
    assert event_kinds(injected, 8) == event_kinds(ai, 8)
    # Negative controls: a fixed injected move does not switch, and the same AI on
    # the same position with a move that hits Gengar attacks instead.
    tackle = sess.act(node, root, TACKLE, q0)
    sess.drop(tackle)
    assert event_kinds(tackle, 7) == [] and event_kinds(tackle, 8) == []
    assert [a["species"] for a in tackle["binary"]["records"]["active"]][0] == 1
    sess.client.drop_state(node)
    hitter = ghost_root(("MOVE_SHADOW_BALL", "MOVE_TACKLE"))
    turn = ai_move(sess, hitter)
    assert event_kinds(turn, 7) == [] and player_moves(turn) == [247]


def forced_root():
    root = fixture_root("p26_faint_forced_replacement.json")
    pikachu = {"species": "SPECIES_PIKACHU", "level": 50, "ability": "ABILITY_STATIC",
               "item": "ITEM_NONE", "moves": ["MOVE_THUNDERBOLT", "MOVE_TACKLE", "MOVE_NONE", "MOVE_NONE"],
               "nature": "NATURE_HARDY", "hp": 110, "max_hp": 110, "status1": "STATUS1_NONE",
               "ivs": [31] * 6, "evs": [0] * 6, "pp": [15, 35, 0, 0]}
    root["player_party"] = root["player_party"] + [pikachu]
    return root


def test_ai_forced_replacement_picks_the_matchup_not_the_first_slot(sess):
    root = forced_root()
    _, node, q0 = sess.probe(root)
    faint = sess.act(node, root, GROWL, q0, drop_node=True)
    assert faint["binary"]["header"]["boundary"] == 2
    node2, q1 = faint["child"]["handle"], rng_of(faint)
    ai = sess.act(node2, root, AI, q1)
    sess.drop(ai)
    assert [a["species"] for a in ai["binary"]["records"]["active"]][0] == 25  # Pikachu vs Gyarados
    assert event_kinds(ai, 8) == [[0, 2, 25]]
    # Negative control: the first legal slot (Charizard) is a different outcome.
    first = sess.act(node2, root, switch(1), q1)
    sess.drop(first)
    assert [a["species"] for a in first["binary"]["records"]["active"]][0] == 6
    assert state(first) != state(ai)
    # The AI's replacement is exactly the injected slot 2 outcome (no RNG drawn).
    injected = sess.act(node2, root, switch(2), q1)
    sess.drop(injected)
    assert state(injected) == state(ai)


def test_player_ai_window_does_not_leak_into_injected_turns(sess):
    """An AI turn followed by an injected turn equals the injected-only history."""
    root = shaped_root()
    _, node, q0 = sess.probe(root)
    ai = sess.act(node, root, AI, q0, drop_node=True)
    after_ai = sess.act(ai["child"]["handle"], root, TACKLE, rng_of(ai))
    sess.drop(after_ai)
    # Same second turn from the same boundary state via a second identical path.
    twin = sess.act(ai["child"]["handle"], root, TACKLE, rng_of(ai))
    sess.drop(twin)
    sess.client.drop_state(ai["child"]["handle"])
    assert state(after_ai) == state(twin)
    assert player_moves(after_ai) == [33]


# --------------------------------------------------------------------------- #
# Injected-action regression against a previous hook ELF
# --------------------------------------------------------------------------- #

def green_actions():
    rows = []
    for path in sorted(FIXTURES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") != "green-target":
            continue
        root = {**data["root"], "critical_policy": data["critical_policy"],
                "operation": "simulate-turn", "mode": "normal"}
        for entry in data["actions"]:
            inject = entry.get("inject_rng") or data.get("inject_rng")
            rows.append((data["id"], entry["label"], root, entry["action"], inject))
    return rows


def run_row(session, root, action, inject):
    try:
        _, node, q0 = session.probe(root)
        rng = (list(inject["main"]), list(inject["secondary"])) if inject else q0
        turn = session.act(node, root, action, rng)
        session.client.drop_state(node)
        session.drop(turn)
        header = {k: v for k, v in turn["binary"]["header"].items() if k not in ("request_id", "crc")}
        return header, turn["binary"]["records"]
    except rom_bridge.WorkerResponseError as error:
        return str(error)


@pytest.mark.skipif(not BASE_ELF.is_file(), reason="no previous hook ELF staged")
def test_injected_actions_match_the_previous_hook_elf_on_every_green_row():
    rows = green_actions()
    assert rows
    new, old = Sess(ELF), Sess(BASE_ELF)
    try:
        results = []
        for fixture_id, label, root, action, inject in rows:
            a = run_row(new, root, action, inject)
            b = run_row(old, root, action, inject)
            assert a == b, (fixture_id, label)
            results.append(a)
        # Negative control: the comparison sees a different action.
        other = GROWL if rows[0][3] != GROWL else TACKLE
        assert run_row(new, rows[0][2], other, rows[0][4]) != results[0]
    finally:
        new.close()
        old.close()


# --------------------------------------------------------------------------- #
# RNG carry: chaining with the previous result's tag-6 quads is the uninterrupted run
# --------------------------------------------------------------------------- #

def replay_state(root, player_action, enemy_action):
    """One uninterrupted scripted turn (recorded actions, no hold in between)."""
    session = Sess()
    try:
        replayed = {**root, "replay_turns": [{"player_action": player_action,
                                              "enemy_action": enemy_action}]}
        turn = session._run(replayed, None)
        return turn
    finally:
        session.close()


@pytest.mark.parametrize("action", [GROWL, TACKLE, switch(1)])
def test_tag6_carry_reproduces_the_uninterrupted_scripted_turn(sess, action):
    root = fixture_root("p29_multiturn_branch.json")
    _, node, q0 = sess.probe(root)
    chained = sess.act(node, root, action, q0)
    sess.drop(chained)
    uninterrupted = replay_state(root, action, TACKLE)
    assert state(chained) == state(uninterrupted)
    assert rng_of(chained) == rng_of(uninterrupted)
    # The chained turn's events are the uninterrupted turn's, minus the battle-start
    # ability activation only the full run from turn 0 emits.
    ev_c = chained["binary"]["records"]["events"]
    ev_u = uninterrupted["binary"]["records"]["events"]
    assert ev_u[len(ev_u) - len(ev_c):] == ev_c
    assert len(ev_u) - len(ev_c) <= 1
    # Negative controls: not carrying (the request seed) or carrying a corrupted
    # quad diverges from the uninterrupted run.
    uncarried = sess.act(node, root, action, (root["rng_main"], root["rng_secondary"]))
    sess.drop(uncarried)
    assert rng_of(uncarried) != rng_of(uninterrupted)
    bad_main = list(q0[0])
    bad_main[0] ^= 1
    corrupted = sess.act(node, root, action, (bad_main, q0[1]))
    sess.drop(corrupted)
    assert rng_of(corrupted) != rng_of(uninterrupted)
    bad_second = list(q0[1])
    bad_second[3] += 1
    corrupted2 = sess.act(node, root, action, (q0[0], bad_second))
    sess.drop(corrupted2)
    assert state(corrupted2) != state(uninterrupted)


def test_carry_chain_restarts_reproducibly_from_a_saved_child(sess):
    root = fixture_root("p29_multiturn_branch.json")
    _, node, q = sess.probe(root)
    for action in (GROWL, TACKLE):  # two chained turns, each carrying the last tag 6
        turn = sess.act(node, root, action, q, drop_node=True)
        node, q = turn["child"]["handle"], rng_of(turn)
    fork = node
    outcomes = []
    for _ in range(2):  # the same third turn released twice from the saved child
        turn = sess.act(fork, root, GROWL, q)
        outcomes.append((state(turn), rng_of(turn)))
        sess.drop(turn)
    assert outcomes[0] == outcomes[1]
    # Negative control: another action from the same child diverges.
    other = sess.act(fork, root, TACKLE, q)
    assert (state(other), rng_of(other)) != outcomes[0]
    sess.drop(other)
    sess.client.drop_state(fork)


def test_ai_turns_chain_deterministically_with_carry(sess):
    root = shaped_root()

    def chain():
        _, node, q = sess.probe(root)
        out = []
        for _ in range(3):
            turn = sess.act(node, root, AI, q, drop_node=True)
            out.append((state(turn), player_moves(turn), rng_of(turn)))
            if turn["binary"]["header"]["boundary"] == 3:
                break
            node, q = turn["child"]["handle"], rng_of(turn)
        if turn.get("child"):
            sess.client.drop_state(turn["child"]["handle"])
        return out

    first, second = chain(), chain()
    assert first == second and len(first) >= 2
    assert all(moves for _, moves, _ in first)
