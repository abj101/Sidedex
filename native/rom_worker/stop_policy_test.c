/* Drives the production Appendix B stop policy over a fake ROMTRACE stream.
 *
 * Each stdin line is "<compact> <kind> <kind> ..." and the program prints
 * "<recorded stop kind>|<turn complete>" for that sequence. Built and run by
 * test_rom_worker_smoke.py; it is not part of the worker build.
 */
#include <stdio.h>
#include <stdlib.h>

#include "stop_policy.h"

int main(void) {
    char line[1024];
    while (fgets(line, sizeof(line), stdin)) {
        struct stop_state state = {"", 0};
        char *token = strtok(line, " \t\r\n");
        if (!token) continue;
        int compact = atoi(token);
        while ((token = strtok(NULL, " \t\r\n"))) stop_mark(&state, token, compact);
        printf("%s|%d\n", state.kind[0] ? state.kind : "none", state.turn_complete);
    }
    return 0;
}
