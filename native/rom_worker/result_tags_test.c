/* Each stdin line "<status> <boundary> <tag> <tag> ..." prints 1 or 0 for
 * result_tags_ok. Built by test_rom_worker_smoke.py; not part of the worker. */
#include <stdio.h>
#include <stdlib.h>

#include "result_tags.h"

int main(void) {
    char line[256];
    while (fgets(line, sizeof(line), stdin)) {
        char *end = line;
        long status = strtol(end, &end, 10), boundary = strtol(end, &end, 10);
        unsigned seen = 0;
        for (long tag; (tag = strtol(end, &end, 10)) > 0;) seen |= 1u << tag;
        printf("%d\n", result_tags_ok((uint16_t) status, (uint16_t) boundary, (uint16_t) seen));
    }
    return 0;
}
