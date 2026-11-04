/* Minimal assertion + little-endian helpers for the shesha C client tests. */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int g_t_failed = 0;
static const char *g_t_name = "";

#define TEST_BEGIN(name)                                                            \
    do {                                                                            \
        g_t_name = (name);                                                          \
        printf("  [ RUN  ] %s\n", g_t_name);                                        \
    } while (0)

#define TEST_CHECK(cond)                                                            \
    do {                                                                            \
        if (!(cond)) {                                                              \
            g_t_failed++;                                                           \
            printf("  [ FAIL ] %s (%s:%d): %s\n", g_t_name, __FILE__, __LINE__,     \
                   #cond);                                                          \
        }                                                                           \
    } while (0)

#define TEST_CHECK_MSG(cond, ...)                                                   \
    do {                                                                            \
        if (!(cond)) {                                                              \
            g_t_failed++;                                                           \
            printf("  [ FAIL ] %s (%s:%d): ", g_t_name, __FILE__, __LINE__);        \
            printf(__VA_ARGS__);                                                    \
            printf("\n");                                                           \
        }                                                                           \
    } while (0)

#define TEST_EQ_I64(a, b)                                                           \
    do {                                                                            \
        long long _a = (long long)(a);                                              \
        long long _b = (long long)(b);                                              \
        if (_a != _b) {                                                             \
            g_t_failed++;                                                           \
            printf("  [ FAIL ] %s (%s:%d): %s == %s (%lld != %lld)\n", g_t_name,    \
                   __FILE__, __LINE__, #a, #b, _a, _b);                             \
        }                                                                           \
    } while (0)

#define TEST_SUMMARY()                                                              \
    do {                                                                            \
        if (g_t_failed == 0) printf("\n  ALL PASS\n");                              \
        else printf("\n  %d CHECK(S) FAILED\n", g_t_failed);                        \
    } while (0)

#define TEST_EXIT() (g_t_failed > 0 ? 1 : 0)

static inline void t_put16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
}

static inline void t_put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    p[2] = (uint8_t)((v >> 16) & 0xFF);
    p[3] = (uint8_t)((v >> 24) & 0xFF);
}

static inline void t_put64(uint8_t *p, uint64_t v)
{
    t_put32(p, (uint32_t)(v & 0xFFFFFFFFu));
    t_put32(p + 4, (uint32_t)(v >> 32));
}

static inline void t_putf(uint8_t *p, float f)
{
    uint32_t u;
    memcpy(&u, &f, sizeof u);
    t_put32(p, u);
}

static inline uint16_t t_le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static inline uint32_t t_le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static inline uint64_t t_le64(const uint8_t *p)
{
    return (uint64_t)t_le32(p) | ((uint64_t)t_le32(p + 4) << 32);
}
