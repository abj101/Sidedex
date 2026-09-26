#include <mgba/core/core.h>
#include <mgba/core/log.h>
#include <mgba/core/serialize.h>
#include <mgba-util/vfs.h>

#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef ROM_WORKER_VERSION
#define ROM_WORKER_VERSION "dev"
#endif
#ifndef ROM_WORKER_MGBA_VERSION
#define ROM_WORKER_MGBA_VERSION "unversioned"
#endif

#define PROTOCOL_VERSION 1
#define MAX_REQUEST_BYTES 4096
#define MAX_FRAMES_PER_REQUEST 100000

static struct mCore *s_core;
static struct VFile *s_saved_state;

static void discard_log(struct mLogger *logger, int category, enum mLogLevel level,
                        const char *format, va_list args) {
    (void) logger;
    (void) category;
    (void) level;
    (void) format;
    (void) args;
}

static bool json_uint(const char *line, const char *key, uint64_t *value) {
    char needle[64];
    if (snprintf(needle, sizeof(needle), "\"%s\"", key) >= (int) sizeof(needle)) {
        return false;
    }
    const char *field = strstr(line, needle);
    if (!field || !(field = strchr(field + strlen(needle), ':'))) {
        return false;
    }
    ++field;
    while (*field == ' ' || *field == '\t') ++field;
    if (*field < '0' || *field > '9') return false;
    errno = 0;
    char *end = NULL;
    unsigned long long parsed = strtoull(field, &end, 10);
    if (errno || end == field || (*end != ',' && *end != '}' && *end != ' ' && *end != '\t')) {
        return false;
    }
    *value = (uint64_t) parsed;
    return true;
}

static bool json_string(const char *line, const char *key, char *out, size_t out_size) {
    char needle[64];
    if (snprintf(needle, sizeof(needle), "\"%s\"", key) >= (int) sizeof(needle)) {
        return false;
    }
    const char *field = strstr(line, needle);
    if (!field || !(field = strchr(field + strlen(needle), ':'))) return false;
    ++field;
    while (*field == ' ' || *field == '\t') ++field;
    if (*field++ != '"') return false;
    size_t i = 0;
    while (*field && *field != '"') {
        if (*field == '\\' || i + 1 >= out_size) return false;
        out[i++] = *field++;
    }
    if (*field != '"') return false;
    out[i] = '\0';
    return true;
}

static void respond_error(const char *reason) {
    printf("{\"protocol\":%d,\"ok\":false,\"error\":\"%s\"}\n", PROTOCOL_VERSION, reason);
    fflush(stdout);
}

static void respond_status(void) {
    char title[13] = {0};
    char code[5] = {0};
    s_core->getGameTitle(s_core, title);
    s_core->getGameCode(s_core, code);
    for (size_t i = 0; title[i]; ++i) {
        if ((unsigned char) title[i] < 0x20 || title[i] == '"' || title[i] == '\\') title[i] = '_';
    }
    for (size_t i = 0; code[i]; ++i) {
        if ((unsigned char) code[i] < 0x20 || code[i] == '"' || code[i] == '\\') code[i] = '_';
    }
    printf("{\"protocol\":%d,\"ok\":true,\"worker\":\"sidedex-mgba-worker/" ROM_WORKER_VERSION
           "\",\"mgba\":\"" ROM_WORKER_MGBA_VERSION "\","
           "\"platform\":\"gba\",\"rom_title\":\"%s\",\"rom_code\":\"%s\","
           "\"rom_bytes\":%zu,\"frame_count\":%" PRIu32 "}\n",
           PROTOCOL_VERSION, title, code, s_core->romSize(s_core), s_core->frameCounter(s_core));
    fflush(stdout);
}

static bool read_memory(uint64_t address, uint64_t width, uint32_t *value) {
    if (address > UINT32_MAX || (width != 1 && width != 2 && width != 4)) return false;
    size_t available = 0;
    const uint8_t *memory = mCoreGetMemoryBlock(s_core, (uint32_t) address, &available);
    if (!memory || available < width) return false;
    uint32_t result = 0;
    for (uint64_t i = 0; i < width; ++i) result |= (uint32_t) memory[i] << (8 * i);
    *value = result;
    return true;
}

static bool handle_request(const char *line, bool *should_quit) {
    uint64_t protocol = 0;
    char op[40];
    if (!json_uint(line, "protocol", &protocol) || protocol != PROTOCOL_VERSION ||
        !json_string(line, "op", op, sizeof(op))) {
        respond_error("invalid_request");
        return false;
    }

    if (strcmp(op, "status") == 0) {
        respond_status();
        return true;
    }
    if (strcmp(op, "run_frames") == 0) {
        uint64_t frames = 0;
        if (!json_uint(line, "frames", &frames) || frames > MAX_FRAMES_PER_REQUEST) {
            respond_error("invalid_frame_count");
            return false;
        }
        for (uint64_t i = 0; i < frames; ++i) s_core->runFrame(s_core);
        respond_status();
        return true;
    }
    if (strcmp(op, "reset") == 0) {
        s_core->reset(s_core);
        respond_status();
        return true;
    }
    if (strcmp(op, "save_state") == 0) {
        struct VFile *next = VFileMemChunk(NULL, 0);
        if (!next || !mCoreSaveStateNamed(s_core, next, SAVESTATE_SAVEDATA | SAVESTATE_RTC)) {
            if (next) next->close(next);
            respond_error("save_state_failed");
            return false;
        }
        if (s_saved_state) s_saved_state->close(s_saved_state);
        s_saved_state = next;
        printf("{\"protocol\":%d,\"ok\":true,\"state_bytes\":%" PRId64 "}\n",
               PROTOCOL_VERSION, (int64_t) s_saved_state->size(s_saved_state));
        fflush(stdout);
        return true;
    }
    if (strcmp(op, "load_state") == 0) {
        if (!s_saved_state) {
            respond_error("no_saved_state");
            return false;
        }
        s_saved_state->seek(s_saved_state, 0, SEEK_SET);
        if (!mCoreLoadStateNamed(s_core, s_saved_state, SAVESTATE_SAVEDATA | SAVESTATE_RTC)) {
            respond_error("load_state_failed");
            return false;
        }
        respond_status();
        return true;
    }
    if (strcmp(op, "read_memory") == 0) {
        uint64_t address = 0;
        uint64_t width = 0;
        uint32_t value = 0;
        if (!json_uint(line, "address", &address) || !json_uint(line, "width", &width) ||
            !read_memory(address, width, &value)) {
            respond_error("unmapped_memory_or_invalid_width");
            return false;
        }
        printf("{\"protocol\":%d,\"ok\":true,\"address\":%" PRIu64 ","
               "\"width\":%" PRIu64 ",\"value\":%" PRIu32 "}\n",
               PROTOCOL_VERSION, address, width, value);
        fflush(stdout);
        return true;
    }
    if (strcmp(op, "quit") == 0) {
        printf("{\"protocol\":%d,\"ok\":true}\n", PROTOCOL_VERSION);
        fflush(stdout);
        *should_quit = true;
        return true;
    }
    respond_error("unknown_operation");
    return false;
}

int main(int argc, char **argv) {
    if (argc != 3 || strcmp(argv[1], "--rom") != 0) {
        fprintf(stderr, "usage: rom_worker --rom PATH\n");
        return 2;
    }
    struct mLogger logger = {.log = discard_log};
    mLogSetDefaultLogger(&logger);

    s_core = mCoreFind(argv[2]);
    if (!s_core || !s_core->init(s_core) || !mCoreLoadFile(s_core, argv[2])) {
        fprintf(stderr, "mGBA could not initialize or load ROM: %s\n", argv[2]);
        if (s_core) s_core->deinit(s_core);
        return 3;
    }
    mCoreInitConfig(s_core, "sidedex-rom-worker");
    mCoreLoadConfig(s_core);
    s_core->reset(s_core);
    if (s_core->platform(s_core) != mPLATFORM_GBA) {
        fprintf(stderr, "loaded core is not a Game Boy Advance core\n");
        s_core->deinit(s_core);
        return 4;
    }

    respond_status();
    char line[MAX_REQUEST_BYTES];
    bool should_quit = false;
    while (!should_quit && fgets(line, sizeof(line), stdin)) {
        if (!strchr(line, '\n') && !feof(stdin)) {
            respond_error("request_too_large");
            int ch;
            while ((ch = getchar()) != '\n' && ch != EOF) {}
            continue;
        }
        (void) handle_request(line, &should_quit);
    }
    if (s_saved_state) s_saved_state->close(s_saved_state);
    s_core->deinit(s_core);
    return 0;
}
