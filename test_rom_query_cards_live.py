"""Live: the v9 trace-path query cards equal the v8c recording, and native equals mGBA.

`tools/rom_oracle/query_cards_expected.json` holds the trace/card hashes recorded on the
v8c ELF (hook 8) for every green fixture row and both query operations
(`query_cards_record.py`). The active v9 ELF must reproduce them through the rom_eval
request path, and so must the native binary (when present) speaking the same op.
"""
import copy
import hashlib
import json
import time
from pathlib import Path

import pytest

import rom_bridge
import rom_eval
from tools.rom_oracle import native_gate_n1 as n1
from tools.rom_oracle import query_cards_record as rec
from tools.rom_oracle import run_dynamic

EXPECTED = json.loads((Path(rec.ROOT) / "tools/rom_oracle/query_cards_expected.json").read_text(encoding="utf-8"))


def _need_mgba():
    if not rec.ELF.is_file() or not rom_bridge.DEFAULT_WORKER.is_file():
        pytest.skip("pinned ROM test ELF or mGBA worker is not installed")


@pytest.fixture(scope="module")
def mgba():
    _need_mgba()
    client, ann = rom_eval._boot_engine(time.monotonic() + 120, rec.ELF, rom_bridge.DEFAULT_WORKER)

    def run(request):
        payload = run_dynamic.encode_request(request, result_buffer=ann)
        return rom_eval._run_batch(client, ann, [payload], time.monotonic() + 60, "cards")["turns"][0]["events"]

    yield run
    client.close()


@pytest.fixture(scope="module")
def native():
    if not n1.NATIVE_BIN_WIN.is_file():
        pytest.skip("native binary is not built")
    sha = n1.sha256(n1.NATIVE_BIN_WIN)
    engine = n1.Engine(["wsl.exe", "-e", "sh", "-c", f"exec {n1.NATIVE_BIN} --rom {n1.NATIVE_BIN}"], sha)
    ann = engine.announce()
    handle = engine.save("cards-base")

    def run(request):
        payload = run_dynamic.encode_request(request, result_buffer=ann)
        reply = engine.op("batch_turns", handle=handle, address=ann["address"], mailbox_bytes=876,
                          budget_ms=60_000, max_frames=24_000, payloads=payload.hex(), request_id="cards",
                          source_sha="0" * 40, engine_elf_sha256=sha, worker_sha256="0" * 64)
        return reply["turns"][0]["events"]

    yield run
    engine.close()


def test_v9_query_cards_equal_the_v8c_recording(mgba):
    assert rec.collect(mgba) == EXPECTED


def test_negative_controls_detect_a_changed_card(mgba):
    got = rec.collect(mgba)
    tampered = copy.deepcopy(EXPECTED)
    tampered["P1"]["query-damage"]["cards"] = "0" * 16
    assert got != tampered
    row, root = next(rec.rows())
    weaker = copy.deepcopy(root)
    weaker["opponent_party"][0]["hp"] = 1  # KO probability changes with the target's HP
    changed = rec.collect_row(mgba, row, weaker)
    assert changed["query-damage"]["cards"] != EXPECTED[row]["query-damage"]["cards"]


def test_native_query_cards_equal_mgba(mgba, native):
    assert rec.collect(native) == rec.collect(mgba) == EXPECTED
