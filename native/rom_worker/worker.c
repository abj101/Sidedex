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
#include <time.h>

#ifndef ROM_WORKER_VERSION
#define ROM_WORKER_VERSION "dev"
#endif
#ifndef ROM_WORKER_MGBA_VERSION
#define ROM_WORKER_MGBA_VERSION "unversioned"
#endif

#define PROTOCOL_VERSION 1
#define MAX_REQUEST_BYTES 65536
#define MAX_FRAMES_PER_REQUEST 100000
#define MAX_TRACE_EVENTS 256
#define MAX_TRACE_BYTES 512
#define EWRAM_START 0x02000000u
#define EWRAM_END 0x02040000u
#define MAX_MEMORY_WRITE_BYTES 2048
#define MAX_BATCH_TURNS 8
#define MAX_BATCH_RESPONSE_BYTES 1048576

static struct mCore *s_core;
static struct VFile *s_saved_state;
static uint64_t s_artifact_bytes;
static char s_trace_events[MAX_TRACE_EVENTS][MAX_TRACE_BYTES];
static size_t s_trace_count;
static bool s_trace_overflow;
static bool s_turn_complete;
static bool s_saw_after_turn;
static bool s_slim_trace;
struct BatchTurn {
    bool complete;
    bool overflow;
    uint64_t frames;
    size_t count;
    char events[MAX_TRACE_EVENTS][MAX_TRACE_BYTES];
};
static struct BatchTurn s_batch[MAX_BATCH_TURNS];

static void respond_error(const char *reason);
static bool write_memory(uint64_t address, const char *hex);

static void capture_log(struct mLogger *logger, int category, enum mLogLevel level,
                        const char *format, va_list args) {
    (void) logger;
    (void) level;
    const char *category_name = mLogCategoryName(category);
    if (!category_name || strcmp(category_name, "GBA Debug") != 0 || s_trace_overflow) return;
    char message[MAX_TRACE_BYTES];
    vsnprintf(message, sizeof(message), format, args);
    if (strncmp(message, "ROMTRACE|", 9) != 0) return;
    if (strncmp(message, "ROMTRACE|complete|", 18) == 0) s_turn_complete = true;
    // A first-turn KO restarts the test runner before battleTurnCounter
    // increments, so the hook never emits complete. Stop on the post-turn
    // mailbox ready instead of spinning to max_frames.
    if (strncmp(message, "ROMTRACE|phase|", 15) == 0 && strstr(message, "|after-turn") != NULL)
        s_saw_after_turn = true;
    if (s_saw_after_turn && strncmp(message, "ROMTRACE|ready|", 15) == 0)
        s_turn_complete = true;
    if (s_slim_trace &&
        strncmp(message, "ROMTRACE|query|", 15) != 0 &&
        strncmp(message, "ROMTRACE|action|", 16) != 0 &&
        strncmp(message, "ROMTRACE|party|", 15) != 0 &&
        strncmp(message, "ROMTRACE|active|", 16) != 0 &&
        strncmp(message, "ROMTRACE|rng|", 13) != 0 &&
        strncmp(message, "ROMTRACE|phase|", 15) != 0 &&
        strncmp(message, "ROMTRACE|status|", 16) != 0 &&
        strncmp(message, "ROMTRACE|faint|", 15) != 0 &&
        strncmp(message, "ROMTRACE|critical-policy|", 25) != 0 &&
        strncmp(message, "ROMTRACE|complete|", 18) != 0 &&
        strncmp(message, "ROMTRACE|ready|", 15) != 0)
        return;
    if (s_trace_count == MAX_TRACE_EVENTS || strlen(message) >= MAX_TRACE_BYTES - 1) {
        s_trace_overflow = true;
        return;
    }
    strcpy(s_trace_events[s_trace_count++], message);
}

static void print_json_string(const char *value) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *) value; *p; ++p) {
        if (*p == '"' || *p == '\\') { putchar('\\'); putchar(*p); }
        else if (*p < 0x20 || *p >= 0x7f) printf("\\u%04x", *p);
        else putchar(*p);
    }
    putchar('"');
}

static size_t json_string_size(const char *value) {
    size_t size = 2;
    for (const unsigned char *p = (const unsigned char *) value; *p; ++p) {
        size += (*p == '"' || *p == '\\') ? 2 : (*p < 0x20 || *p >= 0x7f) ? 6 : 1;
    }
    return size;
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

static bool json_hex_batch(const char *line, char *out, size_t out_size) {
    if (!json_string(line, "payloads", out, out_size)) return false;
    size_t count = 0;
    for (const char *p = out; *p; ++p) {
        if (*p == '|') continue;
        if (!((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'f')) || ++count > MAX_BATCH_TURNS * MAX_MEMORY_WRITE_BYTES * 2) return false;
    }
    return count > 0 && count % 2 == 0;
}

static bool write_hex(uint64_t address, const char *hex) {
    char bounded[MAX_MEMORY_WRITE_BYTES * 2 + 1];
    if (strlen(hex) > sizeof(bounded) - 1) return false;
    strcpy(bounded, hex);
    return write_memory(address, bounded);
}

static bool run_batch_turns(const char *line) {
    uint64_t address = 0, frames_limit = 0, mailbox_bytes = 0, budget_ms = 0;
    char payloads[MAX_BATCH_TURNS * (MAX_MEMORY_WRITE_BYTES * 2 + 1)];
    char request_id[129], source_sha[65], engine_sha[65], worker_sha[65];
    if (!s_saved_state || !json_uint(line, "address", &address) ||
        !json_uint(line, "max_frames", &frames_limit) || frames_limit == 0 || frames_limit > MAX_FRAMES_PER_REQUEST ||
        !json_uint(line, "mailbox_bytes", &mailbox_bytes) || mailbox_bytes == 0 || mailbox_bytes > MAX_MEMORY_WRITE_BYTES ||
        !json_uint(line, "budget_ms", &budget_ms) || budget_ms == 0 ||
        !json_hex_batch(line, payloads, sizeof(payloads)) ||
        !json_string(line, "request_id", request_id, sizeof(request_id)) ||
        !json_string(line, "source_sha", source_sha, sizeof(source_sha)) ||
        !json_string(line, "engine_elf_sha256", engine_sha, sizeof(engine_sha)) ||
        !json_string(line, "worker_sha256", worker_sha, sizeof(worker_sha))) {
        respond_error("invalid_batch_request");
        return false;
    }
    if (address < EWRAM_START || address >= EWRAM_END) {
        respond_error("invalid_batch_address");
        return false;
    }
    char *items[MAX_BATCH_TURNS];
    size_t item_count = 1;
    items[0] = payloads;
    for (char *p = payloads; *p; ++p) if (*p == '|') {
        *p = '\0';
        if (item_count == MAX_BATCH_TURNS) { respond_error("batch_too_large"); return false; }
        items[item_count++] = p + 1;
    }
    for (size_t i = 0; i < item_count; ++i) {
        if (strlen(items[i]) / 2 != mailbox_bytes || strlen(items[i]) % 2 || mailbox_bytes > EWRAM_END - address) {
            respond_error("invalid_batch_payload"); return false;
        }
    }

    clock_t started = clock();
    for (size_t i = 0; i < item_count; ++i) {
        s_saved_state->seek(s_saved_state, 0, SEEK_SET);
        if (!mCoreLoadStateNamed(s_core, s_saved_state, SAVESTATE_SAVEDATA | SAVESTATE_RTC) || !write_hex(address, items[i])) {
            respond_error("batch_checkpoint_or_write_failed"); return false;
        }
        s_trace_count = 0; s_trace_overflow = false; s_turn_complete = false; s_saw_after_turn = false;
        s_slim_trace = true;
        uint64_t frames = 0;
        while (frames < frames_limit && !s_turn_complete && !s_trace_overflow) {
            s_core->runFrame(s_core); ++frames;
            if ((uint64_t) (clock() - started) * 1000 / CLOCKS_PER_SEC >= budget_ms) {
                s_slim_trace = false;
                respond_error("deadline_exceeded"); return false;
            }
        }
        s_slim_trace = false;
        s_batch[i].complete = s_turn_complete;
        s_batch[i].overflow = s_trace_overflow;
        s_batch[i].frames = frames;
        s_batch[i].count = s_trace_count;
        for (size_t j = 0; j < s_trace_count; ++j) strcpy(s_batch[i].events[j], s_trace_events[j]);
    }

    size_t response_size = 512;
    for (size_t i = 0; i < item_count; ++i) {
        response_size += 64;
        for (size_t j = 0; j < s_batch[i].count; ++j) response_size += json_string_size(s_batch[i].events[j]) + 1;
    }
    if (response_size > MAX_BATCH_RESPONSE_BYTES) { respond_error("batch_response_too_large"); return false; }

    printf("{\"protocol\":%d,\"ok\":true,\"request_id\":", PROTOCOL_VERSION);
    print_json_string(request_id);
    printf(",\"source_sha\":"); print_json_string(source_sha);
    printf(",\"engine_elf_sha256\":"); print_json_string(engine_sha);
    printf(",\"worker_sha256\":"); print_json_string(worker_sha);
    printf(",\"turns\":[");
    for (size_t i = 0; i < item_count; ++i) {
        if (i) putchar(',');
        printf("{\"complete\":%s,\"frames\":%" PRIu64 ",\"events\":[", s_batch[i].complete ? "true" : "false", s_batch[i].frames);
        for (size_t j = 0; j < s_batch[i].count; ++j) { if (j) putchar(','); print_json_string(s_batch[i].events[j]); }
        printf("],\"trace_overflow\":%s}", s_batch[i].overflow ? "true" : "false");
    }
    printf("]}\n"); fflush(stdout);
    return true;
}

static void respond_error(const char *reason) {
    printf("{\"protocol\":%d,\"ok\":false,\"error\":\"%s\"}\n", PROTOCOL_VERSION, reason);
    fflush(stdout);
}

static void respond_status(void) {
    char title[13] = {0};
    char code[9] = {0};
    s_core->getGameTitle(s_core, title);
    s_core->getGameCode(s_core, code);
    printf("{\"protocol\":%d,\"ok\":true,\"worker\":\"sidedex-mgba-worker/" ROM_WORKER_VERSION
           "\",\"mgba\":\"" ROM_WORKER_MGBA_VERSION "\",\"platform\":\"gba\",\"rom_title\":",
           PROTOCOL_VERSION);
    print_json_string(title);
    printf(",\"rom_code\":");
    print_json_string(code);
    printf(",\"rom_bytes\":%" PRIu64 ",\"frame_count\":%" PRIu32 "}\n",
           s_artifact_bytes, s_core->frameCounter(s_core));
    fflush(stdout);
}

static void respond_run_frames(void) {
    char title[13] = {0};
    char code[9] = {0};
    s_core->getGameTitle(s_core, title);
    s_core->getGameCode(s_core, code);
    printf("{\"protocol\":%d,\"ok\":true,\"worker\":\"sidedex-mgba-worker/" ROM_WORKER_VERSION
           "\",\"mgba\":\"" ROM_WORKER_MGBA_VERSION "\",\"platform\":\"gba\",\"rom_title\":",
           PROTOCOL_VERSION);
    print_json_string(title);
    printf(",\"rom_code\":");
    print_json_string(code);
    printf(",\"rom_bytes\":%" PRIu64 ",\"frame_count\":%" PRIu32 ",\"trace_events\":[",
           s_artifact_bytes, s_core->frameCounter(s_core));
    for (size_t i = 0; i < s_trace_count; ++i) {
        if (i) putchar(',');
        print_json_string(s_trace_events[i]);
    }
    printf("],\"trace_overflow\":%s}\n", s_trace_overflow ? "true" : "false");
    fflush(stdout);
}

static bool artifact_size(const char *path, uint64_t *size) {
    FILE *file = fopen(path, "rb");
    if (!file) return false;
    bool ok = fseek(file, 0, SEEK_END) == 0;
    long length = ok ? ftell(file) : -1;
    fclose(file);
    if (length < 0) return false;
    *size = (uint64_t) length;
    return true;
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

static bool write_memory(uint64_t address, const char *hex) {
    size_t hex_length = strlen(hex);
    if (hex_length == 0 || hex_length % 2 != 0 || hex_length > MAX_MEMORY_WRITE_BYTES * 2) {
        return false;
    }
    size_t length = hex_length / 2;
    if (address < EWRAM_START || address >= EWRAM_END || length > EWRAM_END - address) {
        return false;
    }

    uint8_t bytes[MAX_MEMORY_WRITE_BYTES];
    for (size_t i = 0; i < length; ++i) {
        unsigned char high = (unsigned char) hex[i * 2];
        unsigned char low = (unsigned char) hex[i * 2 + 1];
        if (!((high >= '0' && high <= '9') || (high >= 'a' && high <= 'f')) ||
            !((low >= '0' && low <= '9') || (low >= 'a' && low <= 'f'))) {
            return false;
        }
        uint8_t hi = high <= '9' ? high - '0' : high - 'a' + 10;
        uint8_t lo = low <= '9' ? low - '0' : low - 'a' + 10;
        bytes[i] = (uint8_t) ((hi << 4) | lo);
    }

    size_t available = 0;
    uint8_t *memory = mCoreGetMemoryBlock(s_core, (uint32_t) address, &available);
    if (!memory || available < length) return false;
    memcpy(memory, bytes, length);
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
        s_trace_count = 0;
        s_trace_overflow = false;
        for (uint64_t i = 0; i < frames; ++i) s_core->runFrame(s_core);
        respond_run_frames();
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
    if (strcmp(op, "write_memory") == 0) {
        uint64_t address = 0;
        char hex[MAX_MEMORY_WRITE_BYTES * 2 + 1];
        if (!json_uint(line, "address", &address) ||
            !json_string(line, "data", hex, sizeof(hex)) || !write_memory(address, hex)) {
            respond_error("invalid_memory_write");
            return false;
        }
        respond_status();
        return true;
    }
    if (strcmp(op, "batch_turns") == 0) return run_batch_turns(line);
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
    if (!artifact_size(argv[2], &s_artifact_bytes)) {
        fprintf(stderr, "could not read input artifact size: %s\n", argv[2]);
        return 2;
    }
    struct mLogger logger = {.log = capture_log};
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
