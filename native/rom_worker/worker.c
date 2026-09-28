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

#include "stop_policy.h"

#ifdef _WIN32
#include <windows.h>
#endif

#ifndef ROM_WORKER_VERSION
#define ROM_WORKER_VERSION "dev"
#endif
#ifndef ROM_WORKER_MGBA_VERSION
#define ROM_WORKER_MGBA_VERSION "unversioned"
#endif
#define ROM_WORKER_NAME "sidedex-mgba-worker/" ROM_WORKER_VERSION

#define PROTOCOL_VERSION 3
/* A base64 state blob can exceed the ordinary 64 KiB request line, so the
 * request buffer holds one full message plus JSON framing. */
#define MAX_MESSAGE_BYTES 1048576
#define MAX_REQUEST_BYTES (MAX_MESSAGE_BYTES + 65536)
#define MAX_FRAMES_PER_REQUEST 100000
#define MAX_TRACE_EVENTS 256
#define MAX_TRACE_BYTES 512
#define EWRAM_START 0x02000000u
#define EWRAM_END 0x02040000u
#define IWRAM_START 0x03000000u
#define IWRAM_END 0x03008000u
#define MAX_MEMORY_WRITE_BYTES 2048
#define MAX_MEMORY_BLOCK_BYTES 4096
#define MAX_BATCH_TURNS 8
#define MAX_BATCH_RESPONSE_BYTES 1048576
#define MAX_V9_MAILBOX_BYTES 2048
#define MAX_V9_RESULT_BYTES 8192
#define MAX_V9_RESULT_BASE64_BYTES 10924
#define V9_RESULT_HEADER_BYTES 64
/* Slot count is the binding limit for the ~528 KiB savestates we ship; the
 * byte cap is the hard ceiling the contract requires (64 MiB). */
#define MAX_STATE_SLOTS 3
#define MAX_STATE_TOTAL_BYTES (64u * 1024u * 1024u)
#define MAX_STATE_NAME 64
#define MAX_HANDLE_HEX 32
#define MAX_SHA_HEX 64
#define MAX_KEYS_MASK 0x3FFu
#define SAVE_FORMAT_ID "mgba-savestate-savedata-rtc-v1"
#define BLOB_HEADER_VERSION 1u
#define BLOB_HEADER_MAGIC "SDXSTT01"
#define BLOB_HEADER_BYTES 216u
#define MAX_BLOB_BASE64_BYTES 900000u

/* ------------------------------------------------------------------ SHA-256 */

typedef struct {
    uint32_t state[8];
    uint64_t bits;
    uint8_t buffer[64];
    size_t buffered;
} Sha256;

static const uint32_t SHA256_K[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u,
    0x923f82a4u, 0xab1c5ed5u, 0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u,
    0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u, 0xe49b69c1u, 0xefbe4786u,
    0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u,
    0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u,
    0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u, 0xa2bfe8a1u, 0xa81a664bu,
    0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au,
    0x5b9cca4fu, 0x682e6ff3u, 0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u,
};

static uint32_t rotr32(uint32_t value, unsigned count) {
    return (value >> count) | (value << (32 - count));
}

static void sha256_init(Sha256 *ctx) {
    ctx->state[0] = 0x6a09e667u; ctx->state[1] = 0xbb67ae85u;
    ctx->state[2] = 0x3c6ef372u; ctx->state[3] = 0xa54ff53au;
    ctx->state[4] = 0x510e527fu; ctx->state[5] = 0x9b05688cu;
    ctx->state[6] = 0x1f83d9abu; ctx->state[7] = 0x5be0cd19u;
    ctx->bits = 0;
    ctx->buffered = 0;
}

static void sha256_block(Sha256 *ctx, const uint8_t *block) {
    uint32_t w[64];
    for (unsigned i = 0; i < 16; ++i) {
        w[i] = ((uint32_t) block[i * 4] << 24) | ((uint32_t) block[i * 4 + 1] << 16) |
               ((uint32_t) block[i * 4 + 2] << 8) | (uint32_t) block[i * 4 + 3];
    }
    for (unsigned i = 16; i < 64; ++i) {
        uint32_t s0 = rotr32(w[i - 15], 7) ^ rotr32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = rotr32(w[i - 2], 17) ^ rotr32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = ctx->state[0], b = ctx->state[1], c = ctx->state[2], d = ctx->state[3];
    uint32_t e = ctx->state[4], f = ctx->state[5], g = ctx->state[6], h = ctx->state[7];
    for (unsigned i = 0; i < 64; ++i) {
        uint32_t s1 = rotr32(e, 6) ^ rotr32(e, 11) ^ rotr32(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t temp1 = h + s1 + ch + SHA256_K[i] + w[i];
        uint32_t s0 = rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t temp2 = s0 + maj;
        h = g; g = f; f = e; e = d + temp1;
        d = c; c = b; b = a; a = temp1 + temp2;
    }
    ctx->state[0] += a; ctx->state[1] += b; ctx->state[2] += c; ctx->state[3] += d;
    ctx->state[4] += e; ctx->state[5] += f; ctx->state[6] += g; ctx->state[7] += h;
}

static void sha256_update(Sha256 *ctx, const uint8_t *data, size_t length) {
    ctx->bits += (uint64_t) length * 8;
    while (length > 0) {
        size_t space = sizeof(ctx->buffer) - ctx->buffered;
        size_t take = length < space ? length : space;
        memcpy(ctx->buffer + ctx->buffered, data, take);
        ctx->buffered += take;
        data += take;
        length -= take;
        if (ctx->buffered == sizeof(ctx->buffer)) {
            sha256_block(ctx, ctx->buffer);
            ctx->buffered = 0;
        }
    }
}

static void sha256_final(Sha256 *ctx, uint8_t out[32]) {
    uint64_t bits = ctx->bits;
    uint8_t pad = 0x80;
    sha256_update(ctx, &pad, 1);
    uint8_t zero = 0;
    while (ctx->buffered != 56) sha256_update(ctx, &zero, 1);
    uint8_t length[8];
    for (unsigned i = 0; i < 8; ++i) length[i] = (uint8_t) (bits >> (56 - i * 8));
    sha256_update(ctx, length, 8);
    for (unsigned i = 0; i < 8; ++i) {
        out[i * 4] = (uint8_t) (ctx->state[i] >> 24);
        out[i * 4 + 1] = (uint8_t) (ctx->state[i] >> 16);
        out[i * 4 + 2] = (uint8_t) (ctx->state[i] >> 8);
        out[i * 4 + 3] = (uint8_t) ctx->state[i];
    }
}

static void sha256_bytes(const uint8_t *data, size_t length, uint8_t out[32]) {
    Sha256 ctx;
    sha256_init(&ctx);
    sha256_update(&ctx, data, length);
    sha256_final(&ctx, out);
}

static void sha256_hex(const uint8_t *data, size_t length, char out[65]) {
    uint8_t digest[32];
    sha256_bytes(data, length, digest);
    for (unsigned i = 0; i < 32; ++i) sprintf(out + i * 2, "%02x", digest[i]);
    out[64] = '\0';
}

/* ------------------------------------------------------------------- base64 */

static const char BASE64_ALPHABET[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static size_t base64_encoded_size(size_t length) {
    return ((length + 2) / 3) * 4;
}

static void base64_encode(const uint8_t *data, size_t length, char *out) {
    size_t written = 0;
    for (size_t i = 0; i < length; i += 3) {
        uint32_t triple = (uint32_t) data[i] << 16;
        if (i + 1 < length) triple |= (uint32_t) data[i + 1] << 8;
        if (i + 2 < length) triple |= (uint32_t) data[i + 2];
        out[written++] = BASE64_ALPHABET[(triple >> 18) & 0x3f];
        out[written++] = BASE64_ALPHABET[(triple >> 12) & 0x3f];
        out[written++] = i + 1 < length ? BASE64_ALPHABET[(triple >> 6) & 0x3f] : '=';
        out[written++] = i + 2 < length ? BASE64_ALPHABET[triple & 0x3f] : '=';
    }
    out[written] = '\0';
}

static int base64_value(char character) {
    if (character >= 'A' && character <= 'Z') return character - 'A';
    if (character >= 'a' && character <= 'z') return character - 'a' + 26;
    if (character >= '0' && character <= '9') return character - '0' + 52;
    if (character == '+') return 62;
    if (character == '/') return 63;
    return -1;
}

static ssize_t base64_decode(const char *text, uint8_t *out, size_t out_size) {
    size_t length = strlen(text);
    if (length % 4 != 0) return -1;
    size_t written = 0;
    for (size_t i = 0; i < length; i += 4) {
        int values[4];
        for (unsigned j = 0; j < 4; ++j) {
            char character = text[i + j];
            if (character == '=') {
                values[j] = -2;
            } else {
                values[j] = base64_value(character);
                if (values[j] < 0) return -1;
            }
        }
        if (values[0] < 0 || values[1] < 0) return -1;
        uint32_t triple = ((uint32_t) values[0] << 18) | ((uint32_t) values[1] << 12);
        if (values[2] >= 0) triple |= (uint32_t) values[2] << 6;
        if (values[3] >= 0) triple |= (uint32_t) values[3];
        if (written < out_size) out[written++] = (uint8_t) (triple >> 16);
        if (values[2] >= 0) { if (written < out_size) out[written++] = (uint8_t) (triple >> 8); }
        if (values[3] >= 0) { if (written < out_size) out[written++] = (uint8_t) triple; }
    }
    if (written > out_size) return -1;
    return (ssize_t) written;
}

/* --------------------------------------------------------------- state slots */

struct StateSlot {
    bool used;
    char handle[MAX_HANDLE_HEX + 1];
    char name[MAX_STATE_NAME + 1];
    struct VFile *file;
    uint64_t bytes;
    char sha256[MAX_SHA_HEX + 1];
    char artifact_sha256[MAX_SHA_HEX + 1];
    char worker_version[40];
    char mgba_version[40];
    char save_format[64];
};

static struct StateSlot s_slots[MAX_STATE_SLOTS];
static uint64_t s_handle_counter;
static uint64_t s_state_total_limit = MAX_STATE_TOTAL_BYTES;

static struct mCore *s_core;
static uint64_t s_artifact_bytes;
static uint64_t s_save_bytes;
static char s_save_sha256[65];
static char s_trace_events[MAX_TRACE_EVENTS][MAX_TRACE_BYTES];
static size_t s_trace_count;
static bool s_trace_overflow;
static bool s_saw_after_turn;
static bool s_slim_trace;
/* Contract Appendix B stop policy; header-resident so the policy itself is
 * unit-testable without an emulator (see stop_policy.h). */
static struct stop_state s_stop = {"", 0};
static bool s_compact_stop;
static char s_stop_marker[MAX_TRACE_BYTES];
struct BatchTurn {
    bool complete;
    bool overflow;
    uint64_t frames;
    double elapsed_ms;
    double restore_ms;
    size_t count;
    char stop[16];
    char stop_marker[MAX_TRACE_BYTES];
    char events[MAX_TRACE_EVENTS][MAX_TRACE_BYTES];
    uint64_t emulation_ns;
    uint64_t log_ns;
    uint64_t restore_ns;
    uint64_t cycles;
};
static struct BatchTurn s_batch[MAX_BATCH_TURNS];
struct BinaryBatchTurn {
    uint64_t frames;
    double restore_ms;
    double save_ms;
    double elapsed_ms;
    size_t result_length;
    char result_base64[MAX_V9_RESULT_BASE64_BYTES + 1];
    bool terminal;
    int child_slot;
};
static struct BinaryBatchTurn s_binary_batch[MAX_BATCH_TURNS];

/* ------------------------------------------------------------------ profiler */
/* High-resolution (QPC on Windows) timing, enabled only by
 * SIDEDEX_WORKER_PROFILE=1. SIDEDEX_WORKER_MIN_LOG=1 attaches an mLogFilter
 * that drops every log category except GBA Debug (the ROMTRACE channel). Both
 * default off: no behaviour change when unset. */
static bool s_profile;
static bool s_min_log;
static double s_tick_ns;
static uint64_t s_emu_ns;
static uint64_t s_log_ns;
static uint64_t s_cycles;
static uint64_t s_save_ns;

static void profile_clock_init(void) {
#ifdef _WIN32
    LARGE_INTEGER frequency;
    if (QueryPerformanceFrequency(&frequency) && frequency.QuadPart) {
        s_tick_ns = 1e9 / (double) frequency.QuadPart;
    }
#else
    s_tick_ns = 1e9 / (double) CLOCKS_PER_SEC;
#endif
}

static uint64_t profile_now_ns(void) {
#ifdef _WIN32
    LARGE_INTEGER counter;
    if (s_tick_ns > 0 && QueryPerformanceCounter(&counter)) {
        return (uint64_t) ((double) counter.QuadPart * s_tick_ns);
    }
#endif
    return (uint64_t) ((double) clock() * 1e9 / (double) CLOCKS_PER_SEC);
}

static void respond_error(const char *reason);
static bool write_hex(uint64_t address, const char *hex);

static void mark_stop(const char *kind, const char *marker) {
    if (stop_mark(&s_stop, kind, s_compact_stop ? 1 : 0))
        snprintf(s_stop_marker, sizeof(s_stop_marker), "%s", marker);
}

static void capture_log(struct mLogger *logger, int category, enum mLogLevel level,
                        const char *format, va_list args) {
    uint64_t start = s_profile ? profile_now_ns() : 0;
    (void) logger;
    (void) level;
    const char *category_name = mLogCategoryName(category);
    if (!category_name || strcmp(category_name, "GBA Debug") != 0 || s_trace_overflow) {
        if (s_profile) s_log_ns += profile_now_ns() - start;
        return;
    }
    char message[MAX_TRACE_BYTES];
    vsnprintf(message, sizeof(message), format, args);
    if (strncmp(message, "ROMTRACE|", 9) != 0) {
        if (s_profile) s_log_ns += profile_now_ns() - start;
        return;
    }
    if (strncmp(message, "ROMTRACE|boundary|", 18) == 0) mark_stop("boundary", message);
    if (strncmp(message, "ROMTRACE|terminal|", 18) == 0) mark_stop("terminal", message);
    if (strncmp(message, "ROMTRACE|binary-ready|", 22) == 0) mark_stop("binary-ready", message);
    if (strncmp(message, "ROMTRACE|complete|", 18) == 0) mark_stop("turn-complete", message);
    // A first-turn KO restarts the test runner before battleTurnCounter
    // increments, so the hook never emits complete. Stop on the post-turn
    // mailbox ready instead of spinning to max_frames.
    if (strncmp(message, "ROMTRACE|phase|", 15) == 0 && strstr(message, "|after-turn") != NULL)
        s_saw_after_turn = true;
    if (s_saw_after_turn && strncmp(message, "ROMTRACE|ready|", 15) == 0)
        mark_stop("turn-complete", message);
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
        strncmp(message, "ROMTRACE|legal|", 15) != 0 &&
        strncmp(message, "ROMTRACE|boundary|", 18) != 0 &&
        strncmp(message, "ROMTRACE|terminal|", 18) != 0 &&
        strncmp(message, "ROMTRACE|binary-ready|", 22) != 0 &&
        strncmp(message, "ROMTRACE|unrepresentable|", 25) != 0 &&
        strncmp(message, "ROMTRACE|instruction-budget-exceeded|", 37) != 0 &&
        strncmp(message, "ROMTRACE|complete|", 18) != 0 &&
        strncmp(message, "ROMTRACE|ready|", 15) != 0) {
        if (s_profile) s_log_ns += profile_now_ns() - start;
        return;
    }
    if (s_trace_count == MAX_TRACE_EVENTS || strlen(message) >= MAX_TRACE_BYTES - 1) {
        s_trace_overflow = true;
        if (s_profile) s_log_ns += profile_now_ns() - start;
        return;
    }
    strcpy(s_trace_events[s_trace_count++], message);
    if (s_profile) s_log_ns += profile_now_ns() - start;
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

static bool json_present(const char *line, const char *key) {
    char needle[64];
    if (snprintf(needle, sizeof(needle), "\"%s\"", key) >= (int) sizeof(needle)) return false;
    const char *field = strstr(line, needle);
    return field && strchr(field + strlen(needle), ':') != NULL;
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

/* Identity digests are compared as lowercase hex regardless of host casing. */
static void normalize_hex(char *hex) {
    for (char *p = hex; *p; ++p) {
        if (*p >= 'A' && *p <= 'F') *p = (char) (*p - 'A' + 'a');
    }
}

static bool valid_state_name(const char *name) {
    size_t length = strlen(name);
    if (length == 0 || length > MAX_STATE_NAME) return false;
    for (const char *p = name; *p; ++p) {
        if (*p <= 0x20 || *p == '"' || *p == '\\' || (unsigned char) *p >= 0x7f) return false;
    }
    return true;
}

static int find_slot_by_handle(const char *handle) {
    for (int i = 0; i < MAX_STATE_SLOTS; ++i) {
        if (s_slots[i].used && strcmp(s_slots[i].handle, handle) == 0) return i;
    }
    return -1;
}

static int find_slot_by_name(const char *name) {
    for (int i = 0; i < MAX_STATE_SLOTS; ++i) {
        if (s_slots[i].used && strcmp(s_slots[i].name, name) == 0) return i;
    }
    return -1;
}

static void free_slot(struct StateSlot *slot) {
    if (slot->file) slot->file->close(slot->file);
    memset(slot, 0, sizeof(*slot));
}

static uint64_t slots_used_bytes(int skip_index) {
    uint64_t total = 0;
    for (int i = 0; i < MAX_STATE_SLOTS; ++i) {
        if (s_slots[i].used && i != skip_index) total += s_slots[i].bytes;
    }
    return total;
}

static void mint_handle(char out[MAX_HANDLE_HEX + 1]) {
    uint64_t counter = ++s_handle_counter;
    uint64_t mixed = (uint64_t) time(NULL) * 0x9E3779B97F4A7C15ull;
    mixed ^= counter * 0xBF58476D1CE4E5B9ull;
    mixed ^= (uint64_t) clock() * 0x94D049BB133111EBull;
    mixed ^= mixed >> 29;
    uint64_t second = mixed ^ (counter << 17) ^ 0xD6E8FEB86659FD93ull;
    snprintf(out, MAX_HANDLE_HEX + 1, "%016" PRIx64 "%016" PRIx64, mixed, second);
}

static bool slot_read_payload(const struct StateSlot *slot, uint8_t **out, size_t *out_length) {
    ssize_t size = slot->file->size(slot->file);
    if (size <= 0) return false;
    uint8_t *buffer = malloc((size_t) size);
    if (!buffer) return false;
    slot->file->seek(slot->file, 0, SEEK_SET);
    if (slot->file->read(slot->file, buffer, (size_t) size) != size) {
        free(buffer);
        return false;
    }
    *out = buffer;
    *out_length = (size_t) size;
    return true;
}

/* Store an immutable payload under a name. Re-using a name replaces the slot
 * and mints a fresh handle; the previous handle becomes stale. */
static int slot_store(const uint8_t *payload, size_t length, const char *name,
                      const char *sha256_hex, const char *artifact_sha256) {
    int existing = find_slot_by_name(name);
    if (slots_used_bytes(existing) + length > s_state_total_limit) return -1;  /* memory cap */
    int index = existing;
    if (index < 0) {
        for (int i = 0; i < MAX_STATE_SLOTS; ++i) {
            if (!s_slots[i].used) { index = i; break; }
        }
    }
    if (index < 0) return -2;  /* slot cap */
    if (existing >= 0) free_slot(&s_slots[index]);
    struct VFile *file = VFileMemChunk(payload, length);
    if (!file) return -3;
    struct StateSlot *slot = &s_slots[index];
    memset(slot, 0, sizeof(*slot));
    slot->used = true;
    slot->file = file;
    slot->bytes = length;
    snprintf(slot->name, sizeof(slot->name), "%s", name);
    snprintf(slot->sha256, sizeof(slot->sha256), "%s", sha256_hex);
    snprintf(slot->artifact_sha256, sizeof(slot->artifact_sha256), "%s", artifact_sha256);
    snprintf(slot->worker_version, sizeof(slot->worker_version), "%s", ROM_WORKER_NAME);
    snprintf(slot->mgba_version, sizeof(slot->mgba_version), "%s", ROM_WORKER_MGBA_VERSION);
    snprintf(slot->save_format, sizeof(slot->save_format), "%s", SAVE_FORMAT_ID);
    mint_handle(slot->handle);
    return index;
}

static bool slot_identity_matches(const struct StateSlot *slot, const char *artifact_sha256) {
    return strcmp(slot->artifact_sha256, artifact_sha256) == 0 &&
           strcmp(slot->worker_version, ROM_WORKER_NAME) == 0 &&
           strcmp(slot->mgba_version, ROM_WORKER_MGBA_VERSION) == 0 &&
           strcmp(slot->save_format, SAVE_FORMAT_ID) == 0;
}

static bool json_base64(const char *line, char *out, size_t out_size) {
    if (!json_string(line, "mailboxes", out, out_size)) return false;
    size_t length = strlen(out);
    if (length == 0 || length % 4 != 0) return false;
    for (size_t i = 0; i < length; ++i)
        if (base64_value(out[i]) < 0 && out[i] != '=') return false;
    return true;
}

static int save_current_state(const char *name, const char *artifact_sha) {
    struct VFile *next = VFileMemChunk(NULL, 0);
    if (!next || !mCoreSaveStateNamed(s_core, next, SAVESTATE_SAVEDATA | SAVESTATE_RTC)) {
        if (next) next->close(next);
        return -3;
    }
    ssize_t size = next->size(next);
    uint8_t *payload = size > 0 ? malloc((size_t) size) : NULL;
    if (size <= 0 || !payload) {
        free(payload);
        next->close(next);
        return -3;
    }
    next->seek(next, 0, SEEK_SET);
    bool read_ok = next->read(next, payload, (size_t) size) == size;
    next->close(next);
    if (!read_ok) { free(payload); return -3; }
    char digest[MAX_SHA_HEX + 1];
    sha256_hex(payload, (size_t) size, digest);
    int index = slot_store(payload, (size_t) size, name, digest, artifact_sha);
    free(payload);
    return index;
}

static void print_slot_identity(const struct StateSlot *slot) {
    printf("\"identity\":{\"worker_version\":");
    print_json_string(slot->worker_version);
    printf(",\"mgba_version\":");
    print_json_string(slot->mgba_version);
    printf(",\"artifact_sha256\":");
    print_json_string(slot->artifact_sha256);
    printf(",\"wire_protocol\":%d,\"save_format\":", PROTOCOL_VERSION);
    print_json_string(slot->save_format);
    printf("}");
}

static void print_slot_summary(const struct StateSlot *slot) {
    printf("{\"handle\":");
    print_json_string(slot->handle);
    printf(",\"name\":");
    print_json_string(slot->name);
    printf(",\"bytes\":%" PRIu64 ",\"sha256\":", slot->bytes);
    print_json_string(slot->sha256);
    printf(",");
    print_slot_identity(slot);
    printf("}");
}

/* ------------------------------------------------------------- state blobs */

static void write_le32(uint8_t *out, uint32_t value) {
    out[0] = (uint8_t) value; out[1] = (uint8_t) (value >> 8);
    out[2] = (uint8_t) (value >> 16); out[3] = (uint8_t) (value >> 24);
}

static void write_le64(uint8_t *out, uint64_t value) {
    for (unsigned i = 0; i < 8; ++i) out[i] = (uint8_t) (value >> (i * 8));
}

static uint32_t read_le32(const uint8_t *in) {
    return (uint32_t) in[0] | ((uint32_t) in[1] << 8) | ((uint32_t) in[2] << 16) |
           ((uint32_t) in[3] << 24);
}

static void write_le16(uint8_t *out, uint16_t value) {
    out[0] = (uint8_t) value; out[1] = (uint8_t) (value >> 8);
}

static uint16_t read_le16(const uint8_t *in) {
    return (uint16_t) in[0] | ((uint16_t) in[1] << 8);
}

static uint32_t crc32_bytes(const uint8_t *data, size_t length) {
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < length; ++i) {
        crc ^= data[i];
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t) -(int32_t) (crc & 1));
    }
    return ~crc;
}

static bool validate_v9_result(uint8_t *result, size_t length, uint16_t request_id,
                               uint16_t mode, uint16_t policy) {
    if (length < V9_RESULT_HEADER_BYTES || memcmp(result, "SDR2", 4) != 0 ||
        read_le16(result + 4) != 1 || read_le16(result + 6) != V9_RESULT_HEADER_BYTES ||
        read_le32(result + 8) != length || read_le16(result + 16) != request_id ||
        read_le16(result + 22) != mode || read_le16(result + 24) != policy) return false;
    uint32_t expected_crc = read_le32(result + 12);
    uint8_t stored_crc[4]; memcpy(stored_crc, result + 12, 4);
    memset(result + 12, 0, 4);
    uint32_t actual_crc = crc32_bytes(result + 12, length - 12);
    memcpy(result + 12, stored_crc, 4);
    if (actual_crc != expected_crc) return false;
    size_t offset = V9_RESULT_HEADER_BYTES;
    uint16_t records = read_le16(result + 26);
    uint16_t seen = 0;
    uint16_t previous_tag = 0;
    for (uint16_t i = 0; i < records; ++i) {
        if (offset + 4 > length) return false;
        uint16_t tag = read_le16(result + offset), bytes = read_le16(result + offset + 2);
        size_t start = offset + 4, end = start + bytes, padded = (end + 3u) & ~3u;
        if (tag < 1 || tag > 11 || tag <= previous_tag || (seen & (1u << tag)) || end > length || padded > length) return false;
        previous_tag = tag;
        seen |= (uint16_t) (1u << tag);
        if (tag <= 7) {
            if (bytes < 4) return false;
            uint16_t count = read_le16(result + start), words = read_le16(result + start + 2);
            if ((size_t) bytes != 4u + (size_t) count * words * 4u) return false;
            static const uint16_t expected_count[] = {0, 12, 2, 1, 2, 1, 1, 0};
            /* Contract Appendix C.2 word widths; test_rom_worker_smoke.py parses
             * this table and fails if it drifts from sdr2_vectors/tables.json. */
            static const uint16_t expected_words[] = {0, 32, 37, 6, 12, 10, 8, 4};
            if (words != expected_words[tag] || (tag != 7 && count != expected_count[tag]) ||
                (tag == 7 && ((read_le16(result + 20) == 3 && count != 0) ||
                              (read_le16(result + 20) != 3 && count == 0)))) return false;
            if (tag == 6) {
                for (unsigned word = 0; word < 8; ++word)
                    if (read_le32(result + start + 4 + word * 4) != read_le32(result + 32 + word * 4)) return false;
            }
        } else if (tag == 8 || tag == 10 || tag == 11) {
            uint16_t words = tag == 8 ? 3 : 2;
            if (bytes != 4 + words * 4 || read_le16(result + start) != 1 ||
                read_le16(result + start + 2) != words) return false;
        } else if (tag == 9) {
            size_t event = start;
            while (event < end) {
                if (event + 4 > end) return false;
                uint16_t words = read_le16(result + event + 2);
                event += 4 + (size_t) words * 4;
                if (event > end) return false;
            }
        }
        for (size_t pad = end; pad < padded; ++pad) if (result[pad]) return false;
        offset = padded;
    }
    bool groups_ok = read_le16(result + 18) == 0 ? (seen & 0x00FEu) == 0x00FEu : seen == (1u << 11);
    uint16_t boundary = read_le16(result + 20);
    return offset == length && groups_ok && boundary <= 3 &&
           ((boundary == 3) == ((seen & (1u << 10)) != 0));
}

static uint64_t read_le64(const uint8_t *in) {
    uint64_t value = 0;
    for (unsigned i = 0; i < 8; ++i) value |= (uint64_t) in[i] << (i * 8);
    return value;
}

static void hex_to_raw(const char *hex, uint8_t out[32]) {
    for (unsigned i = 0; i < 32; ++i) {
        unsigned value = 0;
        sscanf(hex + i * 2, "%2x", &value);
        out[i] = (uint8_t) value;
    }
}

static void raw_to_hex(const uint8_t raw[32], char out[65]) {
    for (unsigned i = 0; i < 32; ++i) sprintf(out + i * 2, "%02x", raw[i]);
    out[64] = '\0';
}

/* Blob layout (little-endian): magic[8], header_version u32, wire_protocol u32,
 * payload_bytes u64, artifact_sha256[32], payload_sha256[32],
 * save_format[64], worker_version[32], mgba_version[32], payload. */
static bool build_blob(const struct StateSlot *slot, const uint8_t *payload, size_t length,
                       uint8_t **out, size_t *out_length) {
    uint8_t header[BLOB_HEADER_BYTES];
    memset(header, 0, sizeof(header));
    memcpy(header, BLOB_HEADER_MAGIC, 8);
    write_le32(header + 8, BLOB_HEADER_VERSION);
    write_le32(header + 12, PROTOCOL_VERSION);
    write_le64(header + 16, length);
    hex_to_raw(slot->artifact_sha256, header + 24);
    uint8_t payload_digest[32];
    sha256_bytes(payload, length, payload_digest);
    memcpy(header + 56, payload_digest, 32);
    snprintf((char *) header + 88, 64, "%s", SAVE_FORMAT_ID);
    snprintf((char *) header + 152, 32, "%s", ROM_WORKER_NAME);
    snprintf((char *) header + 184, 32, "%s", ROM_WORKER_MGBA_VERSION);
    uint8_t *blob = malloc(BLOB_HEADER_BYTES + length);
    if (!blob) return false;
    memcpy(blob, header, BLOB_HEADER_BYTES);
    memcpy(blob + BLOB_HEADER_BYTES, payload, length);
    *out = blob;
    *out_length = BLOB_HEADER_BYTES + length;
    return true;
}

/* Returns 0 on success, 1 for an identity mismatch, 2 for a malformed blob. */
static int parse_blob(const uint8_t *blob, size_t length, const char *artifact_sha256,
                      uint8_t **out_payload, size_t *out_length) {
    if (length < BLOB_HEADER_BYTES) return 2;
    if (memcmp(blob, BLOB_HEADER_MAGIC, 8) != 0) return 2;
    if (read_le32(blob + 8) != BLOB_HEADER_VERSION) return 2;
    uint32_t wire_protocol = read_le32(blob + 12);
    uint64_t payload_bytes = read_le64(blob + 16);
    if (payload_bytes != length - BLOB_HEADER_BYTES) return 2;
    if (wire_protocol != PROTOCOL_VERSION) return 1;
    if (strncmp((const char *) blob + 88, SAVE_FORMAT_ID, 64) != 0) return 1;
    if (strncmp((const char *) blob + 152, ROM_WORKER_NAME, 32) != 0) return 1;
    if (strncmp((const char *) blob + 184, ROM_WORKER_MGBA_VERSION, 32) != 0) return 1;
    char blob_artifact[MAX_SHA_HEX + 1];
    raw_to_hex(blob + 24, blob_artifact);
    if (strcmp(blob_artifact, artifact_sha256) != 0) return 1;
    const uint8_t *payload = blob + BLOB_HEADER_BYTES;
    uint8_t digest[32];
    sha256_bytes(payload, (size_t) payload_bytes, digest);
    if (memcmp(digest, blob + 56, 32) != 0) return 2;
    uint8_t *copy = malloc((size_t) payload_bytes);
    if (!copy) return 2;
    memcpy(copy, payload, (size_t) payload_bytes);
    *out_payload = copy;
    *out_length = (size_t) payload_bytes;
    return 0;
}

/* ------------------------------------------------------------ memory access */

static bool memory_range_ok(uint64_t address, uint64_t length) {
    if (length == 0) return false;
    if (address >= EWRAM_START && address < EWRAM_END) return length <= EWRAM_END - address;
    if (address >= IWRAM_START && address < IWRAM_END) return length <= IWRAM_END - address;
    return false;
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
    if (!memory_range_ok(address, length)) return false;

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

/* ------------------------------------------------------------- batch turns */

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
    char checkpoint_handle[MAX_HANDLE_HEX + 1] = "";
    bool has_handle = json_present(line, "handle");
    if (!json_uint(line, "address", &address) ||
        !json_uint(line, "max_frames", &frames_limit) || frames_limit == 0 || frames_limit > MAX_FRAMES_PER_REQUEST ||
        !json_uint(line, "mailbox_bytes", &mailbox_bytes) || mailbox_bytes == 0 || mailbox_bytes > MAX_MEMORY_WRITE_BYTES ||
        !json_uint(line, "budget_ms", &budget_ms) || budget_ms == 0 ||
        !json_hex_batch(line, payloads, sizeof(payloads)) ||
        !json_string(line, "request_id", request_id, sizeof(request_id)) ||
        !json_string(line, "source_sha", source_sha, sizeof(source_sha)) ||
        !json_string(line, "engine_elf_sha256", engine_sha, sizeof(engine_sha)) ||
        !json_string(line, "worker_sha256", worker_sha, sizeof(worker_sha)) ||
        (has_handle && !json_string(line, "handle", checkpoint_handle, sizeof(checkpoint_handle)))) {
        respond_error("invalid-batch-request");
        return false;
    }
    if (address < EWRAM_START || address >= EWRAM_END) {
        respond_error("invalid-batch-address");
        return false;
    }
    /* Named handle restores that immutable slot; otherwise the legacy default slot. */
    int checkpoint = has_handle ? find_slot_by_handle(checkpoint_handle) : find_slot_by_name("default");
    if (checkpoint < 0) {
        respond_error("stale-handle");
        return false;
    }
    struct VFile *saved_state = s_slots[checkpoint].file;
    char *items[MAX_BATCH_TURNS];
    size_t item_count = 1;
    items[0] = payloads;
    for (char *p = payloads; *p; ++p) if (*p == '|') {
        *p = '\0';
        if (item_count == MAX_BATCH_TURNS) { respond_error("batch-too-large"); return false; }
        items[item_count++] = p + 1;
    }
    for (size_t i = 0; i < item_count; ++i) {
        if (strlen(items[i]) / 2 != mailbox_bytes || strlen(items[i]) % 2 || mailbox_bytes > EWRAM_END - address) {
            respond_error("invalid-batch-payload"); return false;
        }
    }

    clock_t started = clock();
    for (size_t i = 0; i < item_count; ++i) {
        clock_t turn_started = clock();
        clock_t restore_started = clock();
        uint64_t restore_ns_start = s_profile ? profile_now_ns() : 0;
        saved_state->seek(saved_state, 0, SEEK_SET);
        if (!mCoreLoadStateNamed(s_core, saved_state, SAVESTATE_SAVEDATA | SAVESTATE_RTC) || !write_hex(address, items[i])) {
            respond_error("batch-checkpoint-or-write-failed"); return false;
        }
        s_batch[i].restore_ms = (double) (clock() - restore_started) * 1000.0 / CLOCKS_PER_SEC;
        s_batch[i].restore_ns = s_profile ? profile_now_ns() - restore_ns_start : 0;
        s_trace_count = 0; s_trace_overflow = false; s_saw_after_turn = false;
        s_stop.kind[0] = '\0'; s_stop.turn_complete = 0; s_stop_marker[0] = '\0';
        s_compact_stop = false;
        s_slim_trace = true;
        s_emu_ns = 0; s_log_ns = 0;
        uint64_t frames = 0;
        while (frames < frames_limit && !s_stop.turn_complete && !s_trace_overflow) {
            uint64_t frame_start = s_profile ? profile_now_ns() : 0;
            s_core->runFrame(s_core); ++frames;
            if (s_profile) s_emu_ns += profile_now_ns() - frame_start;
            if ((uint64_t) (clock() - started) * 1000 / CLOCKS_PER_SEC >= budget_ms) {
                s_slim_trace = false;
                respond_error("deadline-exceeded"); return false;
            }
        }
        s_slim_trace = false;
        s_batch[i].emulation_ns = s_emu_ns;
        s_batch[i].log_ns = s_log_ns;
        s_batch[i].cycles = s_profile ? frames * (uint64_t) s_core->frameCycles(s_core) : 0;
        s_batch[i].complete = s_stop.turn_complete;
        s_batch[i].overflow = s_trace_overflow;
        s_batch[i].frames = frames;
        s_batch[i].elapsed_ms = (double) (clock() - turn_started) * 1000.0 / CLOCKS_PER_SEC;
        s_batch[i].count = s_trace_count;
        snprintf(s_batch[i].stop, sizeof(s_batch[i].stop), "%s", s_stop.kind);
        snprintf(s_batch[i].stop_marker, sizeof(s_batch[i].stop_marker), "%s", s_stop_marker);
        for (size_t j = 0; j < s_trace_count; ++j) strcpy(s_batch[i].events[j], s_trace_events[j]);
    }

    size_t response_size = 512;
    for (size_t i = 0; i < item_count; ++i) {
        response_size += 96 + json_string_size(s_batch[i].stop_marker);
        for (size_t j = 0; j < s_batch[i].count; ++j) response_size += json_string_size(s_batch[i].events[j]) + 1;
    }
    if (response_size > MAX_BATCH_RESPONSE_BYTES) { respond_error("batch-response-too-large"); return false; }

    printf("{\"protocol\":%d,\"ok\":true,\"request_id\":", PROTOCOL_VERSION);
    print_json_string(request_id);
    printf(",\"source_sha\":"); print_json_string(source_sha);
    printf(",\"engine_elf_sha256\":"); print_json_string(engine_sha);
    printf(",\"worker_sha256\":"); print_json_string(worker_sha);
    printf(",\"turns\":[");
    for (size_t i = 0; i < item_count; ++i) {
        if (i) putchar(',');
        printf("{\"complete\":%s,\"elapsed_ms\":%.3f,\"restore_ms\":%.3f,\"stop\":",
               s_batch[i].complete ? "true" : "false", s_batch[i].elapsed_ms, s_batch[i].restore_ms);
        if (s_batch[i].stop[0]) print_json_string(s_batch[i].stop); else printf("null");
        printf(",\"stop_marker\":");
        if (s_batch[i].stop_marker[0]) print_json_string(s_batch[i].stop_marker); else printf("null");
        printf(",\"frames\":%" PRIu64 ",\"events\":[", s_batch[i].frames);
        for (size_t j = 0; j < s_batch[i].count; ++j) { if (j) putchar(','); print_json_string(s_batch[i].events[j]); }
        printf("],\"trace_overflow\":%s", s_batch[i].overflow ? "true" : "false");
        if (s_profile) {
            printf(",\"profile\":{\"emulation_ns\":%" PRIu64 ",\"log_ns\":%" PRIu64
                   ",\"restore_ns\":%" PRIu64 ",\"cycles\":%" PRIu64 ",\"frames\":%" PRIu64 "}",
                   s_batch[i].emulation_ns, s_batch[i].log_ns, s_batch[i].restore_ns,
                   s_batch[i].cycles, s_batch[i].frames);
        }
        printf("}");
    }
    printf("]}\n"); fflush(stdout);
    return true;
}

static bool run_batch_turns_v3(const char *line) {
    uint64_t address = 0, result_address = 0, result_capacity = 0, mailbox_bytes = 0;
    uint64_t mailbox_count = 0, budget_ms = 0, frames_limit = 0, request_token = 0, mode = 0, policy = 0;
    char request_id[129], source_sha[65], engine_sha[65], worker_sha[65];
    char encoded[MAX_BATCH_TURNS * MAX_V9_MAILBOX_BYTES * 2];
    char checkpoint_handle[MAX_HANDLE_HEX + 1] = "";
    bool has_handle = json_present(line, "handle");
    if (!json_uint(line, "address", &address) || !json_uint(line, "result_address", &result_address) ||
        !json_uint(line, "result_capacity", &result_capacity) || !json_uint(line, "mailbox_bytes", &mailbox_bytes) ||
        !json_uint(line, "mailbox_count", &mailbox_count) || !json_uint(line, "budget_ms", &budget_ms) ||
        !json_uint(line, "max_frames", &frames_limit) || !json_uint(line, "request_token", &request_token) ||
        !json_uint(line, "mode", &mode) || !json_uint(line, "critical_policy", &policy) ||
        !json_string(line, "request_id", request_id, sizeof(request_id)) ||
        !json_string(line, "source_sha", source_sha, sizeof(source_sha)) ||
        !json_string(line, "engine_elf_sha256", engine_sha, sizeof(engine_sha)) ||
        !json_string(line, "worker_sha256", worker_sha, sizeof(worker_sha)) ||
        !json_base64(line, encoded, sizeof(encoded)) ||
        (has_handle && !json_string(line, "handle", checkpoint_handle, sizeof(checkpoint_handle))) ||
        mailbox_count == 0 || mailbox_count > MAX_BATCH_TURNS || mailbox_bytes != 876 ||
        result_capacity == 0 || result_capacity > MAX_V9_RESULT_BYTES || mode > 1 || policy > 1 ||
        request_token > UINT16_MAX || frames_limit == 0 || frames_limit > 24000 || budget_ms == 0) {
        respond_error("invalid-batch-request"); return false;
    }
    if (!memory_range_ok(address, mailbox_bytes) || !memory_range_ok(result_address, result_capacity) ||
        (address < result_address + result_capacity && result_address < address + mailbox_bytes)) {
        respond_error("invalid-batch-address"); return false;
    }
    int checkpoint = has_handle ? find_slot_by_handle(checkpoint_handle) : find_slot_by_name("default");
    if (checkpoint < 0) { respond_error("stale-handle"); return false; }
    normalize_hex(engine_sha);
    if (!slot_identity_matches(&s_slots[checkpoint], engine_sha)) { respond_error("identity-mismatch"); return false; }
    size_t decoded_capacity = (size_t) mailbox_count * mailbox_bytes;
    size_t encoded_length = strlen(encoded);
    uint8_t *mailboxes = malloc(decoded_capacity);
    ssize_t decoded_length = mailboxes ? base64_decode(encoded, mailboxes, decoded_capacity) : -1;
    if (decoded_length < 0 || (size_t) decoded_length != decoded_capacity ||
        encoded_length != base64_encoded_size(decoded_capacity)) {
        free(mailboxes); respond_error("invalid-batch-payload"); return false;
    }

    clock_t started = clock();
    memset(s_binary_batch, 0, sizeof(s_binary_batch));
    for (size_t i = 0; i < (size_t) mailbox_count; ++i) {
        clock_t turn_started = clock();
        clock_t restore_started = clock();
        struct VFile *parent = s_slots[checkpoint].file;
        parent->seek(parent, 0, SEEK_SET);
        if (!mCoreLoadStateNamed(s_core, parent, SAVESTATE_SAVEDATA | SAVESTATE_RTC)) {
            free(mailboxes); respond_error("batch-checkpoint-or-write-failed"); return false;
        }
        s_binary_batch[i].restore_ms = (double) (clock() - restore_started) * 1000.0 / CLOCKS_PER_SEC;
        uint8_t request[876];
        memcpy(request, mailboxes + i * mailbox_bytes, sizeof(request));
        if (read_le32(request) != 0x53445831u || read_le16(request + 4) != 9 ||
            !memory_range_ok(address, sizeof(request)) || !memory_range_ok(result_address, result_capacity)) {
            free(mailboxes); respond_error("invalid-batch-payload"); return false;
        }
        write_le32(request + 864, (uint32_t) result_address);
        write_le32(request + 868, (uint32_t) result_capacity);
        write_le16(request + 872, 1);
        write_le16(request + 874, 1);
        write_le16(request + 858, (uint16_t) mode);
        write_le16(request + 860, (uint16_t) policy);
        write_le16(request + 862, (uint16_t) request_token);
        size_t available = 0;
        uint8_t *request_memory = mCoreGetMemoryBlock(s_core, (uint32_t) address, &available);
        if (!request_memory || available < sizeof(request)) {
            free(mailboxes); respond_error("invalid-batch-address"); return false;
        }
        memcpy(request_memory, request, sizeof(request));
        s_trace_count = 0; s_trace_overflow = false; s_saw_after_turn = false;
        s_stop.kind[0] = '\0'; s_stop.turn_complete = 0; s_stop_marker[0] = '\0';
        s_slim_trace = true;
        /* Contract Appendix B: a compact turn ends on binary-ready, never on the
         * boundary/terminal marker that precedes it, so the child savestate below
         * is taken after the hook committed the result. Without that marker the
         * frame bound ends the loop and the turn fails closed as incomplete. */
        s_compact_stop = true;
        uint64_t frames = 0;
        while (frames < frames_limit && !s_stop.turn_complete && !s_trace_overflow) {
            s_core->runFrame(s_core); ++frames;
            if ((uint64_t) (clock() - started) * 1000 / CLOCKS_PER_SEC >= budget_ms) {
                s_slim_trace = false; s_compact_stop = false;
                free(mailboxes); respond_error("deadline-exceeded"); return false;
            }
        }
        s_slim_trace = false;
        s_compact_stop = false;
        if (strcmp(s_stop.kind, "binary-ready") != 0 || s_trace_overflow) {
            free(mailboxes); respond_error("incomplete-state"); return false;
        }
        unsigned announced_address = 0, announced_length = 0;
        if (sscanf(s_stop_marker + 22, "%u|%u", &announced_address, &announced_length) != 2 ||
            announced_address != result_address || announced_length < V9_RESULT_HEADER_BYTES ||
            announced_length > result_capacity || announced_length > MAX_V9_RESULT_BYTES) {
            free(mailboxes); respond_error("incomplete-state"); return false;
        }
        size_t result_available = 0;
        const uint8_t *result_memory = mCoreGetMemoryBlock(s_core, (uint32_t) result_address, &result_available);
        if (!result_memory || result_available < announced_length) {
            free(mailboxes); respond_error("incomplete-state"); return false;
        }
        uint8_t result[MAX_V9_RESULT_BYTES];
        memcpy(result, result_memory, announced_length);
        if (!validate_v9_result(result, announced_length, (uint16_t) request_token,
                                (uint16_t) mode, (uint16_t) policy)) {
            free(mailboxes); respond_error("incomplete-state"); return false;
        }
        if (read_le16(result + 18) != 0) {
            s_binary_batch[i].terminal = true;
            s_binary_batch[i].frames = frames;
            s_binary_batch[i].result_length = announced_length;
            s_binary_batch[i].elapsed_ms = (double) (clock() - turn_started) * 1000.0 / CLOCKS_PER_SEC;
            base64_encode(result, announced_length, s_binary_batch[i].result_base64);
            continue;
        }
        uint16_t boundary = read_le16(result + 20);
        if (boundary == 3) {
            s_binary_batch[i].terminal = true;
        } else {
            char child_name[MAX_STATE_NAME + 1];
            snprintf(child_name, sizeof(child_name), "child-%04x-%u", (unsigned) request_token, (unsigned) i);
            if (find_slot_by_name(child_name) >= 0) {
                free(mailboxes); respond_error("state-limit-exceeded"); return false;
            }
            clock_t save_started = clock();
            int child = save_current_state(child_name, engine_sha);
            s_binary_batch[i].save_ms = (double) (clock() - save_started) * 1000.0 / CLOCKS_PER_SEC;
            if (child == -1) { free(mailboxes); respond_error("state-memory-exceeded"); return false; }
            if (child == -2) { free(mailboxes); respond_error("state-limit-exceeded"); return false; }
            if (child < 0) { free(mailboxes); respond_error("save-state-failed"); return false; }
            s_binary_batch[i].child_slot = child + 1;
        }
        s_binary_batch[i].frames = frames;
        s_binary_batch[i].result_length = announced_length;
        s_binary_batch[i].elapsed_ms = (double) (clock() - turn_started) * 1000.0 / CLOCKS_PER_SEC;
        base64_encode(result, announced_length, s_binary_batch[i].result_base64);
    }
    free(mailboxes);
    printf("{\"protocol\":%d,\"ok\":true,\"request_id\":", PROTOCOL_VERSION); print_json_string(request_id);
    printf(",\"source_sha\":"); print_json_string(source_sha);
    printf(",\"engine_elf_sha256\":"); print_json_string(engine_sha);
    printf(",\"worker_sha256\":"); print_json_string(worker_sha);
    printf(",\"turns\":[");
    for (size_t i = 0; i < (size_t) mailbox_count; ++i) {
        if (i) putchar(',');
        struct BinaryBatchTurn *turn = &s_binary_batch[i];
        printf("{\"frames\":%" PRIu64 ",\"elapsed_ms\":%.3f,\"restore_ms\":%.3f,\"save_ms\":%.3f,\"result\":",
               turn->frames, turn->elapsed_ms, turn->restore_ms, turn->save_ms);
        print_json_string(turn->result_base64);
        if (turn->child_slot) {
            const struct StateSlot *child = &s_slots[turn->child_slot - 1];
            printf(",\"child\":{\"handle\":"); print_json_string(child->handle);
            printf(",\"bytes\":%" PRIu64 ",\"sha256\":", child->bytes); print_json_string(child->sha256);
            printf(",\"name\":"); print_json_string(child->name); printf(",");
            print_slot_identity(child); printf("}");
        } else printf(",\"child\":null");
        putchar('}');
    }
    printf("],\"ipc_request_bytes\":%zu}\n", strlen(line) + 1); fflush(stdout);
    return true;
}

/* ---------------------------------------------------------------- responses */

static void respond_error(const char *reason) {
    printf("{\"protocol\":%d,\"ok\":false,\"error\":\"%s\"}\n", PROTOCOL_VERSION, reason);
    fflush(stdout);
}

static void print_core_status_fields(void) {
    char title[13] = {0};
    char code[9] = {0};
    s_core->getGameTitle(s_core, title);
    s_core->getGameCode(s_core, code);
    printf("\"worker\":\"" ROM_WORKER_NAME
           "\",\"mgba\":\"" ROM_WORKER_MGBA_VERSION "\",\"platform\":\"gba\",\"rom_title\":");
    print_json_string(title);
    printf(",\"rom_code\":");
    print_json_string(code);
    printf(",\"rom_bytes\":%" PRIu64 ",\"frame_count\":%" PRIu32,
           s_artifact_bytes, s_core->frameCounter(s_core));
    printf(",\"supported_wire_protocols\":[%d],\"state_schema\":\"sidedex-state-v1\","
           "\"transition_schema\":\"sidedex-transition-v2\",\"decode_schema\":\"decode-v2\"",
           PROTOCOL_VERSION);
    printf(",\"save_bytes\":%" PRIu64 ",\"save_sha256\":", s_save_bytes);
    if (s_save_sha256[0]) print_json_string(s_save_sha256); else printf("null");
}

static void respond_status(void) {
    printf("{\"protocol\":%d,\"ok\":true,", PROTOCOL_VERSION);
    print_core_status_fields();
    printf("}\n");
    fflush(stdout);
}

static void respond_run_frames(void) {
    printf("{\"protocol\":%d,\"ok\":true,", PROTOCOL_VERSION);
    print_core_status_fields();
    printf(",\"trace_events\":[");
    for (size_t i = 0; i < s_trace_count; ++i) {
        if (i) putchar(',');
        print_json_string(s_trace_events[i]);
    }
    printf("],\"trace_overflow\":%s", s_trace_overflow ? "true" : "false");
    if (s_profile) {
        printf(",\"profile\":{\"emulation_ns\":%" PRIu64 ",\"log_ns\":%" PRIu64
               ",\"cycles\":%" PRIu64 "}", s_emu_ns, s_log_ns, s_cycles);
    }
    printf("}\n");
    fflush(stdout);
}

static void respond_slot(int index) {
    printf("{\"protocol\":%d,\"ok\":true,\"slot\":", PROTOCOL_VERSION);
    print_slot_summary(&s_slots[index]);
    if (s_profile && s_save_ns) printf(",\"save_ns\":%" PRIu64, s_save_ns);
    s_save_ns = 0; /* one-shot: only the save that set it reports it */
    printf("}\n");
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

static bool file_sha256(const char *path, uint64_t expected_size, char out[65]) {
    FILE *file = fopen(path, "rb");
    if (!file) return false;
    uint8_t *bytes = malloc((size_t) expected_size);
    bool ok = bytes && fread(bytes, 1, (size_t) expected_size, file) == expected_size && fgetc(file) == EOF;
    fclose(file);
    if (!ok) { free(bytes); return false; }
    sha256_hex(bytes, (size_t) expected_size, out);
    free(bytes);
    return true;
}

/* ----------------------------------------------------------------- handlers */

static bool handle_save_state(const char *line) {
    char name[MAX_STATE_NAME + 1] = "default";
    char artifact_sha[MAX_SHA_HEX + 1];
    if (!json_string(line, "artifact_sha256", artifact_sha, sizeof(artifact_sha)) ||
        (json_present(line, "name") && !json_string(line, "name", name, sizeof(name)))) {
        respond_error("invalid-request");
        return false;
    }
    normalize_hex(artifact_sha);
    if (!valid_state_name(name)) {
        respond_error("invalid-state-name");
        return false;
    }
    uint64_t save_start = s_profile ? profile_now_ns() : 0;
    struct VFile *next = VFileMemChunk(NULL, 0);
    if (!next || !mCoreSaveStateNamed(s_core, next, SAVESTATE_SAVEDATA | SAVESTATE_RTC)) {
        if (next) next->close(next);
        respond_error("save-state-failed");
        return false;
    }
    ssize_t size = next->size(next);
    uint8_t *payload = malloc((size_t) size);
    if (size <= 0 || !payload) {
        if (payload) free(payload);
        next->close(next);
        respond_error("save-state-failed");
        return false;
    }
    next->seek(next, 0, SEEK_SET);
    bool read_ok = next->read(next, payload, (size_t) size) == size;
    next->close(next);
    if (!read_ok) {
        free(payload);
        respond_error("save-state-failed");
        return false;
    }
    char digest[MAX_SHA_HEX + 1];
    sha256_hex(payload, (size_t) size, digest);
    int index = slot_store(payload, (size_t) size, name, digest, artifact_sha);
    free(payload);
    if (index == -1) { respond_error("state-memory-exceeded"); return false; }
    if (index == -2) { respond_error("state-limit-exceeded"); return false; }
    if (index < 0) { respond_error("save-state-failed"); return false; }
    s_save_ns = s_profile ? profile_now_ns() - save_start : 0;
    respond_slot(index);
    return true;
}

static bool handle_load_state(const char *line) {
    char handle[MAX_HANDLE_HEX + 1] = "";
    char artifact_sha[MAX_SHA_HEX + 1];
    if (!json_string(line, "artifact_sha256", artifact_sha, sizeof(artifact_sha)) ||
        (json_present(line, "handle") && !json_string(line, "handle", handle, sizeof(handle)))) {
        respond_error("invalid-request");
        return false;
    }
    normalize_hex(artifact_sha);
    int index = handle[0] ? find_slot_by_handle(handle) : find_slot_by_name("default");
    if (index < 0) { respond_error("stale-handle"); return false; }
    if (!slot_identity_matches(&s_slots[index], artifact_sha)) {
        respond_error("identity-mismatch");
        return false;
    }
    struct VFile *file = s_slots[index].file;
    file->seek(file, 0, SEEK_SET);
    if (!mCoreLoadStateNamed(s_core, file, SAVESTATE_SAVEDATA | SAVESTATE_RTC)) {
        respond_error("load-state-failed");
        return false;
    }
    respond_status();
    return true;
}

static bool handle_drop_state(const char *line) {
    char handle[MAX_HANDLE_HEX + 1];
    if (!json_string(line, "handle", handle, sizeof(handle))) {
        respond_error("invalid-request");
        return false;
    }
    int index = find_slot_by_handle(handle);
    if (index < 0) { respond_error("stale-handle"); return false; }
    free_slot(&s_slots[index]);
    printf("{\"protocol\":%d,\"ok\":true,\"dropped\":true,\"handle\":", PROTOCOL_VERSION);
    print_json_string(handle);
    printf("}\n");
    fflush(stdout);
    return true;
}

static void handle_list_states(void) {
    printf("{\"protocol\":%d,\"ok\":true,\"states\":[", PROTOCOL_VERSION);
    bool first = true;
    for (int i = 0; i < MAX_STATE_SLOTS; ++i) {
        if (!s_slots[i].used) continue;
        if (!first) putchar(',');
        first = false;
        print_slot_summary(&s_slots[i]);
    }
    printf("],\"slot_limit\":%d,\"memory_limit\":%" PRIu64 ",\"memory_used\":%" PRIu64 "}\n",
           MAX_STATE_SLOTS, s_state_total_limit, slots_used_bytes(-1));
    fflush(stdout);
}

static bool handle_export_state(const char *line) {
    char handle[MAX_HANDLE_HEX + 1];
    char artifact_sha[MAX_SHA_HEX + 1] = "";
    bool has_artifact = json_present(line, "artifact_sha256");
    if (!json_string(line, "handle", handle, sizeof(handle)) ||
        (has_artifact && !json_string(line, "artifact_sha256", artifact_sha, sizeof(artifact_sha)))) {
        respond_error("invalid-request");
        return false;
    }
    if (has_artifact) normalize_hex(artifact_sha);
    int index = find_slot_by_handle(handle);
    if (index < 0) { respond_error("stale-handle"); return false; }
    if (has_artifact && strcmp(s_slots[index].artifact_sha256, artifact_sha) != 0) {
        respond_error("identity-mismatch");
        return false;
    }
    size_t base64_size = base64_encoded_size((size_t) s_slots[index].bytes + BLOB_HEADER_BYTES);
    if (base64_size > MAX_BLOB_BASE64_BYTES) { respond_error("state-limit-exceeded"); return false; }
    uint8_t *payload = NULL;
    size_t payload_length = 0;
    if (!slot_read_payload(&s_slots[index], &payload, &payload_length)) {
        respond_error("load-state-failed");
        return false;
    }
    uint8_t *blob = NULL;
    size_t blob_length = 0;
    bool built = build_blob(&s_slots[index], payload, payload_length, &blob, &blob_length);
    free(payload);
    if (!built) { respond_error("save-state-failed"); return false; }
    char *encoded = malloc(base64_size + 1);
    if (!encoded) { free(blob); respond_error("save-state-failed"); return false; }
    base64_encode(blob, blob_length, encoded);
    printf("{\"protocol\":%d,\"ok\":true,\"handle\":", PROTOCOL_VERSION);
    print_json_string(s_slots[index].handle);
    printf(",\"bytes\":%" PRIu64 ",\"sha256\":", s_slots[index].bytes);
    print_json_string(s_slots[index].sha256);
    printf(",\"blob_bytes\":%zu,\"base64_bytes\":%zu,\"blob\":", blob_length, strlen(encoded));
    print_json_string(encoded);
    printf("}\n");
    fflush(stdout);
    free(encoded);
    free(blob);
    return true;
}

static bool handle_import_state(const char *line) {
    char name[MAX_STATE_NAME + 1] = "imported";
    char artifact_sha[MAX_SHA_HEX + 1];
    if (!json_string(line, "artifact_sha256", artifact_sha, sizeof(artifact_sha)) ||
        (json_present(line, "name") && !json_string(line, "name", name, sizeof(name)))) {
        respond_error("invalid-request");
        return false;
    }
    normalize_hex(artifact_sha);
    if (!valid_state_name(name)) { respond_error("invalid-state-name"); return false; }
    size_t text_size = strlen(line);
    char *text = malloc(text_size + 1);
    if (!text || !json_string(line, "blob", text, text_size + 1)) {
        free(text);
        respond_error("invalid-request");
        return false;
    }
    size_t encoded_length = strlen(text);
    size_t max_decoded = encoded_length / 4 * 3;
    uint8_t *decoded = malloc(max_decoded + 1);
    ssize_t decoded_length = decoded ? base64_decode(text, decoded, max_decoded) : -1;
    free(text);
    if (decoded_length < 0) { free(decoded); respond_error("incomplete-state"); return false; }
    uint8_t *payload = NULL;
    size_t payload_length = 0;
    int parsed = parse_blob(decoded, (size_t) decoded_length, artifact_sha, &payload, &payload_length);
    free(decoded);
    if (parsed == 1) { respond_error("identity-mismatch"); return false; }
    if (parsed != 0) { respond_error("incomplete-state"); return false; }
    char digest[MAX_SHA_HEX + 1];
    sha256_hex(payload, payload_length, digest);
    int index = slot_store(payload, payload_length, name, digest, artifact_sha);
    free(payload);
    if (index == -1) { respond_error("state-memory-exceeded"); return false; }
    if (index == -2) { respond_error("state-limit-exceeded"); return false; }
    if (index < 0) { respond_error("save-state-failed"); return false; }
    respond_slot(index);
    return true;
}

static bool handle_load_state_blob(const char *line) {
    char artifact_sha[MAX_SHA_HEX + 1];
    if (!json_string(line, "artifact_sha256", artifact_sha, sizeof(artifact_sha))) {
        respond_error("invalid-request");
        return false;
    }
    normalize_hex(artifact_sha);
    size_t text_size = strlen(line);
    char *text = malloc(text_size + 1);
    if (!text || !json_string(line, "blob", text, text_size + 1)) {
        free(text);
        respond_error("invalid-request");
        return false;
    }
    size_t encoded_length = strlen(text);
    size_t max_decoded = encoded_length / 4 * 3;
    uint8_t *decoded = malloc(max_decoded + 1);
    ssize_t decoded_length = decoded ? base64_decode(text, decoded, max_decoded) : -1;
    free(text);
    if (decoded_length < 0) { free(decoded); respond_error("incomplete-state"); return false; }
    uint8_t *payload = NULL;
    size_t payload_length = 0;
    int parsed = parse_blob(decoded, (size_t) decoded_length, artifact_sha, &payload, &payload_length);
    free(decoded);
    if (parsed == 1) { respond_error("identity-mismatch"); return false; }
    if (parsed != 0) { respond_error("incomplete-state"); return false; }
    struct VFile *file = VFileMemChunk(payload, payload_length);
    free(payload);
    if (!file) { respond_error("load-state-failed"); return false; }
    if (!mCoreLoadStateNamed(s_core, file, SAVESTATE_SAVEDATA | SAVESTATE_RTC)) {
        file->close(file);
        respond_error("load-state-failed");
        return false;
    }
    file->close(file);
    respond_status();
    return true;
}

static bool handle_request(const char *line, bool *should_quit) {
    uint64_t protocol = 0;
    char op[40];
    if (!json_uint(line, "protocol", &protocol) || protocol != PROTOCOL_VERSION ||
        !json_string(line, "op", op, sizeof(op))) {
        respond_error("invalid-request");
        return false;
    }

    if (strcmp(op, "status") == 0) {
        respond_status();
        return true;
    }
    if (strcmp(op, "run_frames") == 0) {
        uint64_t frames = 0, keys = 0;
        if (!json_uint(line, "frames", &frames) || frames > MAX_FRAMES_PER_REQUEST) {
            respond_error("invalid-frame-count");
            return false;
        }
        if (json_present(line, "keys")) {
            if (!json_uint(line, "keys", &keys) || keys > MAX_KEYS_MASK) {
                respond_error("invalid-keys");
                return false;
            }
            s_core->setKeys(s_core, (uint32_t) keys);
        }
        s_trace_count = 0;
        s_trace_overflow = false;
        s_emu_ns = 0; s_log_ns = 0;
        for (uint64_t i = 0; i < frames; ++i) {
            uint64_t frame_start = s_profile ? profile_now_ns() : 0;
            s_core->runFrame(s_core);
            if (s_profile) s_emu_ns += profile_now_ns() - frame_start;
        }
        s_cycles = s_profile ? frames * (uint64_t) s_core->frameCycles(s_core) : 0;
        respond_run_frames();
        return true;
    }
    if (strcmp(op, "set_keys") == 0) {
        uint64_t keys = 0;
        if (!json_uint(line, "keys", &keys) || keys > MAX_KEYS_MASK) {
            respond_error("invalid-keys");
            return false;
        }
        s_core->setKeys(s_core, (uint32_t) keys);
        printf("{\"protocol\":%d,\"ok\":true,\"keys\":%" PRIu64 "}\n", PROTOCOL_VERSION, keys);
        fflush(stdout);
        return true;
    }
    if (strcmp(op, "reset") == 0) {
        s_core->reset(s_core);
        respond_status();
        return true;
    }
    if (strcmp(op, "save_state") == 0) return handle_save_state(line);
    if (strcmp(op, "load_state") == 0) return handle_load_state(line);
    if (strcmp(op, "drop_state") == 0) return handle_drop_state(line);
    if (strcmp(op, "list_states") == 0) { handle_list_states(); return true; }
    if (strcmp(op, "export_state") == 0) return handle_export_state(line);
    if (strcmp(op, "import_state") == 0) return handle_import_state(line);
    if (strcmp(op, "load_state_blob") == 0) return handle_load_state_blob(line);
    if (strcmp(op, "read_memory") == 0) {
        uint64_t address = 0;
        uint64_t width = 0;
        uint32_t value = 0;
        if (!json_uint(line, "address", &address) || !json_uint(line, "width", &width) ||
            !read_memory(address, width, &value)) {
            respond_error("unmapped-memory");
            return false;
        }
        printf("{\"protocol\":%d,\"ok\":true,\"address\":%" PRIu64 ","
               "\"width\":%" PRIu64 ",\"value\":%" PRIu32 "}\n",
               PROTOCOL_VERSION, address, width, value);
        fflush(stdout);
        return true;
    }
    if (strcmp(op, "read_memory_block") == 0) {
        uint64_t address = 0, length = 0;
        if (!json_uint(line, "address", &address) || !json_uint(line, "length", &length) ||
            length == 0 || length > MAX_MEMORY_BLOCK_BYTES) {
            respond_error("invalid-memory-block");
            return false;
        }
        if (!memory_range_ok(address, length)) {
            respond_error("unmapped-memory");
            return false;
        }
        size_t available = 0;
        const uint8_t *memory = mCoreGetMemoryBlock(s_core, (uint32_t) address, &available);
        if (!memory || available < length) {
            respond_error("unmapped-memory");
            return false;
        }
        printf("{\"protocol\":%d,\"ok\":true,\"address\":%" PRIu64 ",\"length\":%" PRIu64 ",\"data\":\"",
               PROTOCOL_VERSION, address, length);
        for (uint64_t i = 0; i < length; ++i) printf("%02x", memory[i]);
        printf("\"}\n");
        fflush(stdout);
        return true;
    }
    if (strcmp(op, "write_memory") == 0) {
        uint64_t address = 0;
        char hex[MAX_MEMORY_WRITE_BYTES * 2 + 1];
        if (!json_uint(line, "address", &address) ||
            !json_string(line, "data", hex, sizeof(hex)) || !write_memory(address, hex)) {
            respond_error("invalid-memory-write");
            return false;
        }
        respond_status();
        return true;
    }
    if (strcmp(op, "batch_turns") == 0) return run_batch_turns(line);
    if (strcmp(op, "batch_turns_v3") == 0) return run_batch_turns_v3(line);
    if (strcmp(op, "quit") == 0) {
        printf("{\"protocol\":%d,\"ok\":true}\n", PROTOCOL_VERSION);
        fflush(stdout);
        *should_quit = true;
        return true;
    }
    respond_error("unknown-operation");
    return false;
}

static void load_state_limit_override(void) {
    const char *override = getenv("SIDEDEX_WORKER_STATE_BYTES_LIMIT");
    if (!override || !*override) return;
    errno = 0;
    char *end = NULL;
    unsigned long long parsed = strtoull(override, &end, 10);
    if (!errno && end && *end == '\0' && parsed > 0) s_state_total_limit = (uint64_t) parsed;
}

int main(int argc, char **argv) {
    const char *rom_path = NULL, *save_path = NULL;
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--rom") == 0 && i + 1 < argc && !rom_path) rom_path = argv[++i];
        else if (strcmp(argv[i], "--save") == 0 && i + 1 < argc && !save_path) save_path = argv[++i];
        else {
            fprintf(stderr, "usage: rom_worker --rom PATH [--save 128-KiB-SAVE]\n");
            return 2;
        }
    }
    if (!rom_path) {
        fprintf(stderr, "usage: rom_worker --rom PATH [--save 128-KiB-SAVE]\n");
        return 2;
    }
    if (!artifact_size(rom_path, &s_artifact_bytes)) {
        fprintf(stderr, "could not read input artifact size: %s\n", rom_path);
        return 2;
    }
    if (save_path) {
        if (!artifact_size(save_path, &s_save_bytes) || s_save_bytes != 128u * 1024u ||
            !file_sha256(save_path, s_save_bytes, s_save_sha256)) {
            fprintf(stderr, "save image must be an existing readable 131072-byte file: %s\n", save_path);
            return 2;
        }
    }
    load_state_limit_override();
    profile_clock_init();
    const char *profile_env = getenv("SIDEDEX_WORKER_PROFILE");
    s_profile = profile_env && *profile_env && strcmp(profile_env, "0") != 0;
    const char *minlog_env = getenv("SIDEDEX_WORKER_MIN_LOG");
    s_min_log = minlog_env && *minlog_env && strcmp(minlog_env, "0") != 0;
    struct mLogger logger = {.log = capture_log};
    static struct mLogFilter s_log_filter;
    if (s_min_log) {
        /* Headless setting: drop every log category except GBA Debug, which is
         * the ROMTRACE channel. Category filters run before capture_log, so the
         * per-frame log callback is skipped for all non-ROMTRACE messages. */
        mLogFilterInit(&s_log_filter);
        s_log_filter.defaultLevels = 0;
        mLogFilterSet(&s_log_filter, "gba.debug", mLOG_ALL);
        logger.filter = &s_log_filter;
    }
    mLogSetDefaultLogger(&logger);

    s_core = mCoreFind(rom_path);
    if (!s_core || !s_core->init(s_core) || !mCoreLoadFile(s_core, rom_path)) {
        fprintf(stderr, "mGBA could not initialize or load ROM: %s\n", rom_path);
        if (s_core) s_core->deinit(s_core);
        return 3;
    }
    mCoreInitConfig(s_core, "sidedex-rom-worker");
    mCoreLoadConfig(s_core);
    s_core->reset(s_core);
    if (save_path && !mCoreLoadSaveFile(s_core, save_path, true)) {
        fprintf(stderr, "mGBA could not load save image: %s\n", save_path);
        s_core->deinit(s_core);
        return 5;
    }
    if (s_core->platform(s_core) != mPLATFORM_GBA) {
        fprintf(stderr, "loaded core is not a Game Boy Advance core\n");
        s_core->deinit(s_core);
        return 4;
    }

    respond_status();
    static char line[MAX_REQUEST_BYTES];
    bool should_quit = false;
    while (!should_quit && fgets(line, sizeof(line), stdin)) {
        if (!strchr(line, '\n') && !feof(stdin)) {
            respond_error("request-too-large");
            int ch;
            while ((ch = getchar()) != '\n' && ch != EOF) {}
            continue;
        }
        (void) handle_request(line, &should_quit);
    }
    for (int i = 0; i < MAX_STATE_SLOTS; ++i) {
        if (s_slots[i].used) free_slot(&s_slots[i]);
    }
    s_core->deinit(s_core);
    return 0;
}
