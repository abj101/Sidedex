"""Record the query-card outputs (damage, KO, AI action) for every green fixture row.

`python tools/rom_oracle/query_cards_record.py [elf [protocol]] > out.json` runs `run_dynamic.run_turn` on
the root of each row for both query operations and hashes the ROMTRACE query trace and the
parsed cards. The v9 hook adds `state|clegal` records; the trace hash ignores those. The committed `query_cards_expected.json` was recorded on the v8c ELF, so
`test_rom_query_cards_live.py` proves the v9 migration left every card unchanged.
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import rom_query  # noqa: E402
from tools.rom_oracle import run_dynamic  # noqa: E402

FIXTURES = sorted((ROOT / "tools/rom_oracle/fixtures").glob("*.json"))
DATA = json.loads((ROOT / "data/game_data.json").read_text(encoding="utf-8"))
ELF = ROOT / "native/rom_worker/build/mingw/pokeemerald-test-headless.elf"


def _h(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def rows():
    for path in FIXTURES:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if fixture.get("status") == "green-target":
            yield fixture["id"], {**fixture["root"], "critical_policy": fixture["critical_policy"]}


def collect_row(run, row, root):
    """`run(request) -> trace` for one query request per operation -> {op: {trace, cards}}."""
    out = {}
    for op in ("query-damage", "query-ai-action"):
        request = {**root, "operation": op}
        try:
            trace = run(request)
            snapshot = rom_query.snapshot_from_request(request)
            cards = rom_query.parse(trace, snapshot, DATA)
            out[op] = {"trace": _h([e for e in trace if "|state|clegal|" not in e]), "cards": _h(cards)}
        except Exception as error:  # an unsupported row records its reason code
            out[op] = {"error": str(error)[:120]}
    return out


def collect(run):
    """{row: {op: {trace, cards}}} over every green fixture row."""
    return {row: collect_row(run, row, root) for row, root in rows()}


if __name__ == "__main__":
    elf = Path(sys.argv[1]) if len(sys.argv) > 1 else ELF
    version = int(sys.argv[2]) if len(sys.argv) > 2 else None
    sha = hashlib.sha256(elf.read_bytes()).hexdigest()
    print(json.dumps(collect(lambda request: run_dynamic.run_turn(elf, sha, request, protocol_version=version)),
                     indent=1, sort_keys=True))
