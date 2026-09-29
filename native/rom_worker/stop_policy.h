/* Contract Appendix B stop policy for one batch turn.
 *
 * The v9 hook commits the binary result and only then emits
 * `ROMTRACE|binary-ready|<address>|<length>`; the boundary or terminal marker
 * comes first, and `ROMTRACE|complete|` after that. A compact (v9) turn must
 * therefore stop on binary-ready alone: stopping on boundary/terminal reads the
 * result buffer before the hook committed it, and the worker then rejects every
 * turn as incomplete-state. A trace turn keeps the historical behaviour, where
 * the first boundary/terminal/binary-ready marker stops the turn.
 *
 * Both the decision and the accumulator live here because worker.c links them
 * into the emulator while test_rom_worker_smoke.py compiles this header on its
 * own and drives the exact production decision over a fake marker stream, with
 * no ROM involved.
 */
#ifndef SIDEDEX_STOP_POLICY_H
#define SIDEDEX_STOP_POLICY_H

#include <stdio.h>
#include <string.h>

#define STOP_KIND_BYTES 16

struct stop_state {
    char kind[STOP_KIND_BYTES];  /* highest-ranked completing marker seen */
    int turn_complete;
};

static int stop_rank(const char *kind) {
    if (strcmp(kind, "boundary") == 0 || strcmp(kind, "terminal") == 0 ||
        strcmp(kind, "binary-ready") == 0) return 2;
    if (strcmp(kind, "turn-complete") == 0) return 1;
    return 0;
}

/* True when this marker may end the turn. Unknown kinds never end it. */
static int stop_completes_turn(const char *kind, int compact) {
    if (compact) return strcmp(kind, "binary-ready") == 0;
    return stop_rank(kind) > 0;
}

/* Records the marker; returns 1 when it became the recorded stop kind. */
static int stop_mark(struct stop_state *state, const char *kind, int compact) {
    if (!stop_completes_turn(kind, compact)) return 0;
    int recorded = stop_rank(kind) > stop_rank(state->kind);
    if (recorded) snprintf(state->kind, sizeof(state->kind), "%s", kind);
    state->turn_complete = 1;
    return recorded;
}

#endif
