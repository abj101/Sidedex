"""Local Emerald Imperium Box and matchup planner."""

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from rom_bridge import ENGINE_ELF_SHA256

ROOT = Path(__file__).parent
DB_PATH = ROOT / "sidedex.sqlite3"
DATA_PATH = ROOT / "data" / "game_data.json"
DEFAULT_ENGINE_ELF = ROOT / "native" / "rom_worker" / "build" / "mingw" / "pokeemerald-test-headless.elf"
DEFAULT_ROM_WORKER = ROOT / "native" / "rom_worker" / "build" / "mingw" / "rom_worker.exe"
STATUSES = ("alive", "dead", "unavailable")


def connect(path=DB_PATH):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS owned (
        id INTEGER PRIMARY KEY, species TEXT NOT NULL, level INTEGER,
        ability TEXT, item TEXT, nature TEXT, moves TEXT NOT NULL DEFAULT '[]',
        ivs TEXT NOT NULL DEFAULT '{}', evs TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'alive', in_party INTEGER NOT NULL DEFAULT 0,
        owner TEXT, pair_id TEXT, box_no INTEGER NOT NULL DEFAULT 1,
        box_slot INTEGER, party_slot INTEGER
    )""")
    if "box_no" not in {row[1] for row in db.execute("PRAGMA table_info(owned)")}:
        db.execute("ALTER TABLE owned ADD COLUMN box_no INTEGER NOT NULL DEFAULT 1")
    if "box_slot" not in {row[1] for row in db.execute("PRAGMA table_info(owned)")}:
        db.execute("ALTER TABLE owned ADD COLUMN box_slot INTEGER")
    if "party_slot" not in {row[1] for row in db.execute("PRAGMA table_info(owned)")}:
        db.execute("ALTER TABLE owned ADD COLUMN party_slot INTEGER")
    # Repair missing, invalid, or duplicate assignments before creating the
    # partial indexes. This also upgrades databases made before either slot
    # column existed.
    with _atomic(db):
        db.execute("UPDATE owned SET box_no=1 WHERE box_no NOT BETWEEN 1 AND 14 OR box_no IS NULL")
        for row in db.execute("SELECT id, box_no, box_slot FROM owned WHERE in_party=0 ORDER BY id").fetchall():
            slot = row["box_slot"]
            duplicate = (slot is not None and db.execute(
                "SELECT 1 FROM owned WHERE id!=? AND in_party=0 AND box_no=? AND box_slot=?",
                (row["id"], row["box_no"], slot)).fetchone())
            if not isinstance(slot, int) or not 0 <= slot < 30 or duplicate:
                db.execute("UPDATE owned SET box_slot=NULL WHERE id=?", (row["id"],))
                slot = _first_open_slot(db, row["box_no"])
                if slot is not None:
                    db.execute("UPDATE owned SET box_slot=? WHERE id=?", (slot, row["id"]))
        used_party_slots = set()
        party_rows = db.execute("SELECT id, party_slot FROM owned WHERE in_party=1 ORDER BY id").fetchall()
        for row in party_rows:
            slot = row["party_slot"]
            if isinstance(slot, int) and 0 <= slot < 6 and slot not in used_party_slots:
                used_party_slots.add(slot)
                continue
            slot = next((candidate for candidate in range(6) if candidate not in used_party_slots), None)
            db.execute("UPDATE owned SET party_slot=? WHERE id=?", (slot, row["id"]))
            if slot is not None:
                used_party_slots.add(slot)
        db.execute("UPDATE owned SET party_slot=NULL WHERE in_party=0 AND party_slot IS NOT NULL")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS owned_box_slot ON owned(box_no, box_slot) WHERE in_party=0 AND box_slot IS NOT NULL")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS owned_party_slot ON owned(party_slot) WHERE in_party=1 AND party_slot IS NOT NULL")
    db.execute("""CREATE TABLE IF NOT EXISTS team_configs (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        settings TEXT NOT NULL
    )""")
    db.commit()
    return db


def _first_open_slot(db, box_no, exclude_id=None):
    used = {row[0] for row in db.execute(
        "SELECT box_slot FROM owned WHERE box_no=? AND in_party=0 AND id!=?", (box_no, exclude_id or -1))}
    return next((slot for slot in range(30) if slot not in used), None)


def _first_open_party_slot(db, exclude_id=None):
    used = {row[0] for row in db.execute(
        "SELECT party_slot FROM owned WHERE in_party=1 AND id!=?", (exclude_id or -1,))}
    return next((slot for slot in range(6) if slot not in used), None)


@contextmanager
def _atomic(db):
    """Run an atomic write, nesting safely inside a caller transaction."""
    if db.in_transaction:
        name = "sidedex_" + str(id(db))
        db.execute(f"SAVEPOINT {name}")
        try:
            yield
            db.execute(f"RELEASE SAVEPOINT {name}")
        except Exception:
            db.execute(f"ROLLBACK TO SAVEPOINT {name}")
            db.execute(f"RELEASE SAVEPOINT {name}")
            raise
    else:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
            db.commit()
        except Exception:
            db.rollback()
            raise


def apply_party_slots(db, slots):
    """Atomically make six ordered party slots; place outgoing members in open Box slots."""
    try:
        if len(slots) != 6:
            raise ValueError
        normalized = []
        for value in slots:
            if value is None:
                normalized.append(None)
                continue
            if isinstance(value, bool):
                raise ValueError
            entry_id = int(value)
            if isinstance(value, float) and value != entry_id:
                raise ValueError
            normalized.append(entry_id)
    except (TypeError, ValueError):
        raise ValueError("Party slots must contain six unique owned IDs or empty slots.") from None
    incoming = [value for value in normalized if value is not None]
    if len(incoming) != len(set(incoming)):
        raise ValueError("Party slots must contain six unique owned IDs or empty slots.")

    with _atomic(db):
        found = {row["id"]: row for row in db.execute(
            f"SELECT id FROM owned WHERE id IN ({','.join('?' for _ in incoming)})", incoming)} if incoming else {}
        if len(found) != len(incoming):
            raise ValueError("Party choices must be owned Pokémon.")
        outgoing = db.execute("SELECT id,box_no FROM owned WHERE in_party=1 AND id NOT IN (" +
                              (",".join("?" for _ in incoming) or "-1") + ") ORDER BY id", incoming).fetchall()
        db.execute("UPDATE owned SET party_slot=NULL WHERE in_party=1")
        for entry_id in incoming:
            db.execute("UPDATE owned SET in_party=1,box_slot=NULL WHERE id=?", (entry_id,))
        for row in outgoing:
            destination = None
            for number in range(1, 15):
                slot = _first_open_slot(db, number)
                if slot is not None:
                    destination = (number, slot)
                    break
            if destination is None:
                raise ValueError("Every Box is full; the party cannot be changed.")
            db.execute("UPDATE owned SET in_party=0,status='alive',box_no=?,box_slot=? WHERE id=?",
                       (*destination, row["id"]))
        for slot, entry_id in enumerate(normalized):
            if entry_id is not None:
                db.execute("UPDATE owned SET party_slot=? WHERE id=?", (slot, entry_id))


def list_team_configs(db):
    """Return saved configurations ordered by name, with decoded settings."""
    return [
        {"id": row["id"], "name": row["name"], "settings": json.loads(row["settings"])}
        for row in db.execute("SELECT id,name,settings FROM team_configs ORDER BY name COLLATE NOCASE, id")
    ]


def save_team_config(db, name, settings, config_id=None):
    """Create or update a named configuration; settings are stored as JSON."""
    name = str(name).strip()
    if not name:
        raise ValueError("Configuration name cannot be empty.")
    encoded = json.dumps(settings)
    with _atomic(db):
        if config_id is None:
            return db.execute("INSERT INTO team_configs(name,settings) VALUES(?,?)",
                              (name, encoded)).lastrowid
        cursor = db.execute("UPDATE team_configs SET name=?,settings=? WHERE id=?",
                            (name, encoded, config_id))
        if not cursor.rowcount:
            raise ValueError("Configuration no longer exists.")
        return config_id


def delete_team_config(db, config_id):
    """Delete a saved configuration by ID; return whether it existed."""
    with _atomic(db):
        return bool(db.execute("DELETE FROM team_configs WHERE id=?", (config_id,)).rowcount)


def move_box_slot(db, entry_id, box_no, target_slot):
    """Move to an empty slot or swap with its occupant in the same box."""
    if not 0 <= target_slot < 30:
        raise ValueError("Choose a slot in the Box.")
    db.execute("BEGIN IMMEDIATE")
    try:
        source = db.execute("SELECT box_slot FROM owned WHERE id=? AND box_no=? AND in_party=0",
                            (entry_id, box_no)).fetchone()
        if source is None or source[0] is None:
            raise ValueError("This Pokémon is no longer in the open Box.")
        if source[0] != target_slot:
            occupant = db.execute("SELECT id FROM owned WHERE box_no=? AND box_slot=? AND in_party=0",
                                  (box_no, target_slot)).fetchone()
            db.execute("UPDATE owned SET box_slot=NULL WHERE id=?", (entry_id,))
            if occupant:
                db.execute("UPDATE owned SET box_slot=? WHERE id=?", (source[0], occupant[0]))
            db.execute("UPDATE owned SET box_slot=? WHERE id=?", (target_slot, entry_id))
        db.commit()
    except Exception:
        db.rollback()
        raise


def move_party_to_box(db, entry_id, box_no, target_slot):
    """Move a party member onto a Box slot; its occupant swaps into the party slot."""
    if not 1 <= box_no <= 14:
        raise ValueError("Choose a Box from 1 to 14.")
    if not 0 <= target_slot < 30:
        raise ValueError("Choose a slot in the Box.")
    db.execute("BEGIN IMMEDIATE")
    try:
        source = db.execute("SELECT party_slot FROM owned WHERE id=? AND in_party=1",
                            (entry_id,)).fetchone()
        if source is None or source["party_slot"] is None:
            raise ValueError("This Pokémon is no longer in the party.")
        party_slot = source["party_slot"]
        occupant = db.execute("SELECT id FROM owned WHERE box_no=? AND box_slot=? AND in_party=0",
                              (box_no, target_slot)).fetchone()
        db.execute("UPDATE owned SET in_party=0,party_slot=NULL WHERE id=?", (entry_id,))
        if occupant:
            db.execute("UPDATE owned SET in_party=1,box_slot=NULL,party_slot=? WHERE id=?",
                       (party_slot, occupant["id"]))
        db.execute("UPDATE owned SET status='alive',box_no=?,box_slot=? WHERE id=?",
                   (box_no, target_slot, entry_id))
        db.commit()
    except Exception:
        db.rollback()
        raise


def save_entry(db, values, entry_id=None):
    """Save one Box record and enforce the six-member party limit."""
    db.execute("BEGIN IMMEDIATE")
    try:
        current_party = db.execute("SELECT COUNT(*) FROM owned WHERE in_party=1").fetchone()[0]
        previous = db.execute("SELECT in_party, box_no, box_slot, party_slot FROM owned WHERE id=?", (entry_id,)).fetchone() if entry_id else None
        was_party = bool(previous["in_party"]) if previous else False
        if values["in_party"] and not was_party and current_party >= 6:
            raise ValueError("Party is limited to six Pokémon.")
        box_no = values.get("box_no") or (db.execute("SELECT box_no FROM owned WHERE id=?", (entry_id,)).fetchone()[0] if entry_id else 1)
        if not 1 <= box_no <= 14:
            raise ValueError("Choose a Box from 1 to 14.")
        if not values["in_party"]:
            occupied = db.execute("SELECT COUNT(*) FROM owned WHERE box_no=? AND in_party=0 AND id!=?", (box_no, entry_id or -1)).fetchone()[0]
            if occupied >= 30:
                raise ValueError(f"Box {box_no} is full (30 Pokémon).")
        box_slot = previous["box_slot"] if previous and previous["box_no"] == box_no else None
        if not values["in_party"]:
            if box_slot is None or db.execute(
                "SELECT 1 FROM owned WHERE box_no=? AND box_slot=? AND in_party=0 AND id!=?",
                (box_no, box_slot, entry_id or -1)).fetchone():
                box_slot = _first_open_slot(db, box_no, entry_id)
        party_slot = None
        if values["in_party"]:
            old_slot = previous["party_slot"] if previous and was_party else None
            party_slot = old_slot if old_slot is not None and 0 <= old_slot < 6 else _first_open_party_slot(db, entry_id)
            if party_slot is None:
                raise ValueError("Party is limited to six Pokémon.")
        packed = (
            values["species"], values["level"], values["ability"], values["item"],
            values["nature"], json.dumps(values["moves"]), json.dumps(values["ivs"]),
            json.dumps(values["evs"]), values["status"], int(values["in_party"]),
            values["owner"], values["pair_id"], box_no, box_slot, party_slot,
        )
        if entry_id:
            db.execute("""UPDATE owned SET species=?,level=?,ability=?,item=?,nature=?,moves=?,
                ivs=?,evs=?,status=?,in_party=?,owner=?,pair_id=?,box_no=?,box_slot=?,party_slot=? WHERE id=?""", (*packed, entry_id))
            saved_id = entry_id
        else:
            saved_id = db.execute("""INSERT INTO owned(species,level,ability,item,nature,moves,ivs,evs,status,
                in_party,owner,pair_id,box_no,box_slot,party_slot) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", packed).lastrowid
        db.commit()
        return saved_id
    except Exception:
        db.rollback()
        raise


def read_data(file_mtime_ns=None):
    return json.loads(DATA_PATH.read_text(encoding="utf-8")) if DATA_PATH.exists() else None


def resolve_trainer_level(raw_level, highest_party_level=None):
    """Resolve the game's 200+n level encoding only with the required context."""
    try:
        raw = int(raw_level)
    except (TypeError, ValueError):
        return str(raw_level) if raw_level is not None else "Unknown"
    if 200 <= raw < 300:
        if highest_party_level is None:
            return "?"
        return str(highest_party_level - (raw - 200))
    return str(raw)


def main():
    import streamlit as st
    from planner_ui import render_page

    @st.fragment
    def planner():
        with closing(connect()) as db:
            render_page(db, game_data, save_entry, resolve_trainer_level, connect, move_box_slot,
                        apply_party_slots, move_party_to_box)

    st.set_page_config(page_title="Sidedex · Emerald Imperium", page_icon="S", layout="wide")
    st.html(ROOT / "ui.css")
    game_data = st.cache_data(read_data)(DATA_PATH.stat().st_mtime_ns if DATA_PATH.exists() else None)
    if not game_data:
        st.error(f"Game data not found at {DATA_PATH.relative_to(ROOT)}.")
        return
    if Path(os.environ.get("SIDEDEX_ENGINE_ELF_PATH", DEFAULT_ENGINE_ELF)).is_file():
        @st.cache_resource
        def prewarm_rom_once():
            import rom_eval
            rom_eval.prewarm()
            return True

        prewarm_rom_once()
    planner()


def engine_setup_status():
    """Return startup diagnostics without hashing the large ELF."""
    elf = Path(os.environ.get("SIDEDEX_ENGINE_ELF_PATH", DEFAULT_ENGINE_ELF))
    worker = Path(os.environ.get("SIDEDEX_ROM_WORKER_EXE", DEFAULT_ROM_WORKER))
    messages = []
    if not elf.is_file():
        messages.append((
            "engine artifact missing: copy the pinned pokeemerald-test-headless.elf "
            f"(SHA-256 {ENGINE_ELF_SHA256}) to native/rom_worker/build/mingw/ "
            "or set SIDEDEX_ENGINE_ELF_PATH, then run native/rom_worker/build.ps1; "
            "see tools/rom_oracle/README.md.",
            True))
    else:
        messages.append((f"engine artifact ok: {elf}", False))
    if not worker.is_file():
        messages.append((
            f"rom worker missing: {worker}; run native/rom_worker/build.ps1.",
            True))
    else:
        messages.append((f"rom worker ok: {worker}", False))
    return messages


def self_check():
    """Runnable check for the party cap and transaction rollback."""
    with tempfile.TemporaryDirectory() as temp:
        db = connect(Path(temp) / "check.sqlite3")
        values = {"species": "SPECIES_TEST", "level": 5, "ability": None, "item": None,
                  "nature": None, "moves": [], "ivs": {}, "evs": {}, "status": "alive",
                  "in_party": True, "owner": None, "pair_id": None}
        for _ in range(6):
            save_entry(db, values)
        try:
            save_entry(db, values)
        except ValueError:
            assert db.execute("SELECT COUNT(*) FROM owned WHERE in_party=1").fetchone()[0] == 6
        else:
            raise AssertionError("seventh party member was accepted")
        values["in_party"] = False
        for _ in range(30):
            save_entry(db, values)
        try:
            save_entry(db, values)
        except ValueError:
            assert db.execute("SELECT COUNT(*) FROM owned WHERE box_no=1 AND in_party=0").fetchone()[0] == 30
        else:
            raise AssertionError("31st Box member was accepted")
        values["box_no"] = 2
        saved_id = save_entry(db, values)
        assert db.execute("SELECT box_no FROM owned WHERE id=?", (saved_id,)).fetchone()[0] == 2
        other_id = save_entry(db, values)
        move_box_slot(db, saved_id, 2, 1)
        assert db.execute("SELECT box_slot FROM owned WHERE id=?", (saved_id,)).fetchone()[0] == 1
        assert db.execute("SELECT box_slot FROM owned WHERE id=?", (other_id,)).fetchone()[0] == 0
        move_box_slot(db, saved_id, 2, 29)
        assert db.execute("SELECT box_slot FROM owned WHERE id=?", (saved_id,)).fetchone()[0] == 29
        db.close()
    assert resolve_trainer_level("202") == "?"
    assert resolve_trainer_level("202", 50) == "48"
    assert resolve_trainer_level("42", 50) == "42"
    for message, _warning in engine_setup_status():
        print(message)


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        self_check()
        print("party cap check passed")
    else:
        main()
