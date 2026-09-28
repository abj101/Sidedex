"""Contract Appendix C tests for the SDR2 decoder. No emulator required.

Three layers:
  * the golden vectors in tools/rom_oracle/sdr2_vectors/ decode/reject exactly
    as their .json says (the vectors are synthetic wire examples, not ROM state);
  * flipping any single byte of the good choice_p1 vector is rejected;
  * every C.1 rejection that the vectors do not reach, built with the reference
    encoder (make_sdr2_vectors) and resealed so only the rule under test differs.
"""

import json
import os
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from tools.rom_oracle import make_sdr2_vectors as ref  # noqa: E402
from tools.rom_oracle import run_dynamic, sdr2  # noqa: E402

VECTORS = ROOT / "tools" / "rom_oracle" / "sdr2_vectors"
# The A1 candidate v9 ELF; every v9 emulator test skips until O stages it.
V9_ELF = Path(os.environ["SIDEDEX_ROM_V9_ELF"]) if os.environ.get("SIDEDEX_ROM_V9_ELF") else None
GOOD = ("choice_p1", "forced_switch_p26", "terminal_win", "unsupported_status")
# Each negative vector pins one C.1 rejection; the name is its `reject` field.
REJECT_CODES = {
    "bad_crc": "sdr2-crc",
    "bad_length": "sdr2-length",
    "bad_record_count": "sdr2-record-count",
    "duplicate_tag": "sdr2-duplicate-tag",
    "missing_tag7": "sdr2-missing-tag",
    "oversize": "sdr2-oversize",
    "wrong_request_id": "sdr2-request-id",
}


def expectation(name):
    return json.loads((VECTORS / f"{name}.json").read_text(encoding="utf-8"))


def blob(name):
    return (VECTORS / f"{name}.bin").read_bytes()


def parse_vector(name, **overrides):
    """Parse a vector with the request values its expectation declares."""
    exp = expectation(name)
    kwargs = {"request_id": exp["expect_request_id"], "capacity": exp["capacity"]}
    header = exp.get("header")
    if header:
        kwargs["mode"] = header["mode"]
        kwargs["critical_policy"] = header["critical_policy"]
    kwargs.update(overrides)
    return sdr2.parse(blob(name), **kwargs)


def run_dynamic_ready(event):
    """Parse one ROMTRACE ready line through the client's own validator."""
    from rom_bridge import parse_v9_ready
    return parse_v9_ready([event])


def sha256_of(path):
    import hashlib
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def reseal(data):
    """Recompute the CRC of a mutated buffer so only the tested rule can fail."""
    buf = bytearray(data)
    crc_input = bytearray(buf[12:])
    crc_input[:4] = b"\0" * 4
    struct.pack_into("<I", buf, 12, zlib.crc32(bytes(crc_input)) & 0xFFFFFFFF)
    return bytes(buf)


def decode_error(data, **kwargs):
    """Return the SDR2 error code for `data`, or None when it is accepted."""
    try:
        sdr2.parse(data, **kwargs)
    except sdr2.SDR2Error as exc:
        return exc.code
    return None


class GoldenVectorTests(unittest.TestCase):
    def test_every_vector_file_is_covered_here(self):
        present = {path.stem for path in VECTORS.glob("*.json")} - {"tables"}
        self.assertEqual(present, set(GOOD) | set(REJECT_CODES))

    def test_good_vectors_decode_to_exactly_their_json(self):
        for name in GOOD:
            with self.subTest(vector=name):
                exp = expectation(name)
                self.assertTrue(exp["valid"])
                parsed = parse_vector(name)
                self.assertEqual(parsed["header"], exp["header"])
                self.assertEqual(parsed["records"], exp["records"])
                self.assertEqual(parsed["child_expected"], exp["child_expected"])

    def test_bad_vectors_are_rejected_with_the_matching_code(self):
        for name, code in REJECT_CODES.items():
            with self.subTest(vector=name):
                exp = expectation(name)
                self.assertFalse(exp["valid"])
                self.assertEqual(exp["reject"], name)
                self.assertEqual(decode_error(blob(name), request_id=exp["expect_request_id"],
                                              capacity=exp["capacity"]), code)

    def test_good_vectors_carry_the_named_groups_in_ascending_tag_order(self):
        parsed = parse_vector("choice_p1")
        self.assertEqual(set(parsed["records"]),
                         {"party", "active", "field", "side", "battle", "rng", "legal",
                          "trainer_action", "events"})
        self.assertEqual(len(parsed["records"]["party"]), 12)
        self.assertEqual(len(parsed["records"]["active"]), 2)
        self.assertEqual(parsed["records"]["legal"][0],
                         {"kind": 0, "id": 33, "pp_or_species": 25, "target_or_slot": 1})
        self.assertEqual(parsed["child_expected"], True)
        self.assertFalse(parse_vector("terminal_win")["child_expected"])


class SingleByteFlipTests(unittest.TestCase):
    def test_flipping_any_single_byte_of_the_good_vector_is_rejected(self):
        original = blob("choice_p1")
        exp = expectation("choice_p1")
        kwargs = {"request_id": exp["expect_request_id"], "capacity": exp["capacity"],
                  "mode": exp["header"]["mode"], "critical_policy": exp["header"]["critical_policy"]}
        for index in range(len(original)):
            mutated = bytearray(original)
            mutated[index] ^= 0xFF
            code = decode_error(bytes(mutated), **kwargs)
            self.assertIsNotNone(code, f"byte {index} flip was accepted")
            self.assertTrue(code.startswith("sdr2-"), (index, code))


class ContractRuleTests(unittest.TestCase):
    """The C.1 rejections the golden vectors do not reach."""

    def base(self, *, boundary=1, hp0=100, terminal=None):
        return ref.build(ref.common(ref.CHOICE if boundary != 3 else None, 2, hp0=hp0, terminal=terminal),
                         turn=2, boundary=boundary)

    def test_header_and_magic_rules(self):
        good = bytearray(self.base())
        for label, index, value in (("magic", slice(0, 4), b"XDR2"),
                                    ("format", slice(4, 6), (2).to_bytes(2, "little")),
                                    ("header size", slice(6, 8), (63).to_bytes(2, "little"))):
            with self.subTest(field=label):
                mutated = bytearray(good)
                mutated[index] = value
                self.assertEqual(decode_error(reseal(mutated)), "sdr2-version")

    def test_oversize_uses_the_announced_capacity(self):
        exp = expectation("choice_p1")
        self.assertEqual(decode_error(blob("choice_p1"), capacity=exp["bytes"] - 1), "sdr2-oversize")
        self.assertIsNone(decode_error(blob("choice_p1"), capacity=exp["bytes"]))

    def test_unknown_tag_and_descending_tag_are_rejected(self):
        unknown = ref.build([(12, struct.pack("<HH", 0, 4))], turn=2, boundary=1)
        self.assertEqual(decode_error(unknown), "sdr2-version")
        descending = ref.build(list(reversed(ref.common(ref.CHOICE, 2))), turn=2, boundary=1)
        self.assertEqual(decode_error(descending), "sdr2-duplicate-tag")

    def test_word_width_and_nonzero_padding_are_length_errors(self):
        data = bytearray(self.base())
        struct.pack_into("<H", data, 70, 31)    # party words_per_entry 32 -> 31 (payload+2)
        self.assertEqual(decode_error(reseal(data)), "sdr2-length")
        # Record = tag(2) + length(2) + payload; ref.build zero-pads to 4 bytes. A
        # 19-byte payload leaves one pad byte at offset 64 + 4 + 19 = 87.
        data = bytearray(ref.build([(7, b"\x00" * 19)], turn=2, boundary=1))
        self.assertEqual(len(data), 88)
        self.assertEqual(data[87], 0)
        data[87] = 0x01
        self.assertEqual(decode_error(reseal(data)), "sdr2-length")

    def test_boundary_and_status_rules(self):
        self.assertEqual(decode_error(ref.build(ref.common(ref.CHOICE, 2), status=6, boundary=0)),
                         "sdr2-status")                      # nonzero status with a full record set
        self.assertEqual(decode_error(ref.build(ref.common(ref.CHOICE, 2), boundary=0)),
                         "sdr2-version")                     # status 0 without a boundary kind
        forced = ref.build(ref.common(ref.CHOICE, 2, hp0=0), turn=2, boundary=2)
        self.assertEqual(decode_error(forced), "sdr2-missing-tag")  # a move at a forced switch
        self.assertEqual(decode_error(ref.build(ref.common([], 2), turn=2, boundary=1)),
                         "sdr2-missing-tag")                 # tag 7 present but empty
        terminal = ref.build(ref.common(None, 3, terminal=1) + [ref.typed(10, [ref.entry(ref.TERMINAL, result=1, reason=0)])],
                             turn=3, boundary=3)
        self.assertIsNone(decode_error(terminal))
        with_legal = ref.build(ref.common(ref.CHOICE, 3, terminal=1)
                               + [ref.typed(10, [ref.entry(ref.TERMINAL, result=1, reason=0)])],
                               turn=3, boundary=3)
        self.assertEqual(decode_error(with_legal), "sdr2-version")

    def test_request_mode_policy_and_rng_identity_rules(self):
        good = self.base()
        self.assertEqual(decode_error(good, request_id=2), "sdr2-request-id")
        self.assertEqual(decode_error(good, mode=1), "sdr2-mode")
        self.assertEqual(decode_error(good, critical_policy=1), "sdr2-mode")
        mismatched = ref.build(ref.common(ref.CHOICE, 2), turn=2, boundary=1,
                               rng=(9, 10, 11, 12, 13, 14, 15, 16))
        self.assertEqual(decode_error(mismatched), "sdr2-identity")

    def test_unknown_event_kind_fails_closed(self):
        records = ref.common(ref.CHOICE, 2)
        records[-1] = (9, struct.pack("<HHI", 99, 1, 1))
        self.assertEqual(decode_error(ref.build(records, turn=2, boundary=1)), "sdr2-version")

    def test_entry_count_and_truncated_payload_are_length_errors(self):
        data = bytearray(self.base())
        struct.pack_into("<H", data, 68, 11)                # party entry_count 12 -> 11 (payload)
        self.assertEqual(decode_error(reseal(data)), "sdr2-length")
        self.assertEqual(decode_error(self.base()[:-4]), "sdr2-length")


class V9RequestTests(unittest.TestCase):
    """Contract Appendix B: the 876-byte v9 request tail (no emulator needed)."""

    READY = "ROMTRACE|ready|33554560|876|9|3|33556000|8192"

    def setUp(self):
        fixture = json.loads((ROOT / "tools" / "rom_oracle" / "fixtures"
                              / "p01_physical_neutral.json").read_text(encoding="utf-8"))
        self.request = {**fixture["root"], "critical_policy": fixture["critical_policy"],
                        "operation": "simulate-turn", "mode": "normal", "request_id": 7}
        self.ready = run_dynamic_ready(self.READY)

    def test_mailbox_layout_sizes_follow_the_contract(self):
        self.assertEqual(run_dynamic.mailbox_bytes(7), 856)
        self.assertEqual(run_dynamic.mailbox_bytes(8), 864)
        self.assertEqual(run_dynamic.mailbox_bytes(9), 876)

    def test_v9_tail_carries_the_announced_result_buffer(self):
        payload = run_dynamic.encode_v9_request(self.request, self.ready)
        self.assertEqual(len(payload), 876)
        address, capacity, version, flags = struct.unpack_from("<IIHH", payload, 864)
        self.assertEqual((address, capacity),
                         (self.ready["result_address"], self.ready["result_capacity"]))
        self.assertEqual(version, run_dynamic.V9_RESULT_VERSION)
        self.assertEqual(flags & 1, 1)                 # bit 0 requests the compact result
        self.assertEqual(flags & ~1, 0)                # remaining bits are zero

    def test_v9_is_the_v8_request_with_a_new_version_and_tail(self):
        v8 = run_dynamic.encode_request(self.request, protocol_version=8)
        v9 = run_dynamic.encode_v9_request(self.request, self.ready)
        self.assertEqual(v9[:4], v8[:4])
        self.assertEqual(v9[6:864], v8[6:864])
        self.assertEqual(struct.unpack_from("<H", v8, 4)[0], 8)
        self.assertEqual(struct.unpack_from("<H", v9, 4)[0], 9)

    def test_a_caller_supplied_result_buffer_is_refused(self):
        for banned in ("result_address", "result_capacity"):
            with self.subTest(field=banned):
                with self.assertRaises(ValueError):
                    run_dynamic.encode_v9_request({**self.request, banned: 0x02000000}, self.ready)

    def test_v9_requires_the_roms_announcement(self):
        with self.assertRaises(ValueError):
            run_dynamic.encode_v9_request(self.request, None)
        # encode_request keeps its v7/v8 contract: v9 is not a layout it invents.
        with self.assertRaises(ValueError):
            run_dynamic.encode_request(self.request, protocol_version=9)

    def test_the_announcement_rejects_a_caller_chosen_address(self):
        ready = run_dynamic_ready(self.READY)
        self.assertEqual(ready["result_address"], 0x02000620)
        self.assertEqual(ready["address"], 0x02000080)
        # The ROM's echo is checked: an overlapping or out-of-range pair is refused.
        with self.assertRaises(Exception):
            run_dynamic_ready("ROMTRACE|ready|33554560|876|9|3|33554500|8192")

    @unittest.skipUnless(V9_ELF, "the A1 candidate v9 ELF is not staged yet (SIDEDEX_ROM_V9_ELF)")
    def test_live_v9_turn_decodes_a_compact_result(self):
        worker = Path(os.environ.get(
            "SIDEDEX_ROM_WORKER_EXE", ROOT / "native" / "rom_worker" / "build" / "dev" / "rom_worker.exe"))
        result = run_dynamic.run_v9_turn(
            V9_ELF, sha256_of(V9_ELF), self.request, worker_path=worker, worker_sha256=sha256_of(worker))
        self.assertEqual(result["payload_bytes"], 876)
        turn = result["turns"][0]
        self.assertEqual(turn["binary"]["header"]["status"], sdr2.STATUS_OK)
        self.assertTrue(turn["binary"]["child_expected"])
        self.assertIsNotNone(turn["child"])


if __name__ == "__main__":
    unittest.main()
