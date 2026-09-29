/* Contract Appendix C.1 required-tag rule for one SDR2 result, by boundary.
 *
 * `seen` has bit N set when tag N is present. Nonterminal success = exactly
 * tags 1..9; terminal success (boundary 3) = exactly 1..6, 8, 9, 10 (no legal
 * list, tag 7); any nonzero status = exactly tag 11. Header-only so
 * test_rom_worker_smoke.py can compile it without an emulator.
 */
#ifndef SIDEDEX_RESULT_TAGS_H
#define SIDEDEX_RESULT_TAGS_H

#include <stdint.h>

static int result_tags_ok(uint16_t status, uint16_t boundary, uint16_t seen) {
    if (status != 0) return seen == (1u << 11);
    return seen == (boundary == 3 ? 0x077Eu : 0x03FEu);
}

#endif
