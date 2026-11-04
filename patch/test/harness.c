/* Host harness for CHATTERFIX handlers.c.
 * stdin : lines "t_us col row layer pressed"  (sorted by time; firmware-side events)
 * stdout: lines "t_us col row layer pressed"  (what reaches the original key_event_process)
 * argv[1] = scan period in us (how often adc_sensor_process -> chatter_flush runs)
 * argv[2] = initial tick value (to test wraparound)
 * argv[3] = a column whose keys are TMR (type 2), -1 = none
 * argv[4] = (v4) debounce setting in ms (config header byte 9)
 * argv[5] = (v4) Snap Tap pairs "a:b,c:d" (key index = col*6+row), "-" = none
 * Guard mode (v4): argv[1] = "guard"; stdin lines of hex report bytes -> "1"/"0" per line.
 */
#define HOST_TEST
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>

static uint64_t now_us;
void encoder_quadrature_tick(void) {}
void key_event_orig(uint32_t col, uint32_t row, uint32_t layer, uint32_t pressed)
{
    printf("%llu %u %u %u %u\n", (unsigned long long)now_us, col, row, layer, pressed);
}
#include "handlers.c"

int main(int argc, char **argv)
{
#ifdef CHATTER_PALETTE
    if (argc > 1 && !strcmp(argv[1], "palette")) {
        /* stdin: "<21 header slot bytes hex> | R G B (decimal)" -> "n R G B changed" */
        char line[512];
        while (fgets(line, sizeof line, stdin)) {
            char *bar = strchr(line, '|');
            if (!bar) continue;
            *bar = 0;
            uint8_t slots[21] = { 0 }, rgb[18] = { 0 };
            unsigned n = 0, v, r, g, b;
            for (char *s = strtok(line, " \r\n"); s && n < 21; s = strtok(0, " \r\n"))
                if (sscanf(s, "%x", &v) == 1) slots[n++] = (uint8_t)v;
            if (sscanf(bar + 1, "%u %u %u", &r, &g, &b) != 3) continue;
            uint32_t pn = palette_load(slots, rgb);
            uint8_t c[3] = { (uint8_t)r, (uint8_t)g, (uint8_t)b };
            int ch = palette_remap(c, pn, rgb);
            printf("%u %u %u %u %d\n", (unsigned)pn, c[0], c[1], c[2], ch);
        }
        return 0;
    }
    if (argc > 1 && !strcmp(argv[1], "effect")) {
        /* stdin: "<21 slot bytes hex> | mode bright gbright R G B" -> "R G B" */
        char line[512];
        while (fgets(line, sizeof line, stdin)) {
            char *bar = strchr(line, '|');
            if (!bar) continue;
            *bar = 0;
            uint8_t slots[21] = { 0 }, rgb[18] = { 0 };
            unsigned n = 0, v, m, br, gb, r, g, b;
            for (char *s = strtok(line, " \r\n"); s && n < 21; s = strtok(0, " \r\n"))
                if (sscanf(s, "%x", &v) == 1) slots[n++] = (uint8_t)v;
            if (sscanf(bar + 1, "%u %u %u %u %u %u", &m, &br, &gb, &r, &g, &b) != 6) continue;
            uint32_t pn = palette_load(slots, rgb);
            uint8_t c[3] = { (uint8_t)r, (uint8_t)g, (uint8_t)b };
            effect_colour(m, c, br, gb, pn, rgb);
            printf("%u %u %u\n", c[0], c[1], c[2]);
        }
        return 0;
    }
#endif
#ifdef CHATTER_V4
    if (argc > 1 && !strcmp(argv[1], "guard")) {
        char line[512];
        while (fgets(line, sizeof line, stdin)) {
            uint8_t b[64] = { 0 };
            unsigned n = 0, v;
            for (char *s = strtok(line, " \r\n"); s && n < 64; s = strtok(0, " \r\n"))
                if (sscanf(s, "%x", &v) == 1) b[n++] = (uint8_t)v;
            int ok = chatter_cmd_ok(b);
            printf("%d", ok);
            for (unsigned i = 0; i < 8; i++)
                printf(" %02x", b[i]);
            printf("\n");
        }
        return 0;
    }
#endif
    uint64_t scan = argc > 1 ? strtoull(argv[1], 0, 10) : 125;
    chatter_ticks = argc > 2 ? (uint32_t)strtoul(argv[2], 0, 0) : 0;
    /* all keys mechanical (type 7); argv[3] = a column whose keys are TMR (type 2) */
    for (unsigned i = 0; i < sizeof host_sw_type; i++)
        host_sw_type[i] = 7;
    if (argc > 3 && atoi(argv[3]) >= 0)
        for (unsigned rr = 0; rr < 6; rr++)
            host_sw_type[atoi(argv[3]) * 6 + rr] = 2;
#ifdef CHATTER_V4
    memset(host_partner, 0xFF, sizeof host_partner);
    if (argc > 4)
        host_debounce = (uint8_t)atoi(argv[4]);
    if (argc > 5 && strcmp(argv[5], "-")) {
        char buf[256];
        strncpy(buf, argv[5], sizeof buf - 1);
        buf[sizeof buf - 1] = 0;
        for (char *s = strtok(buf, ","); s; s = strtok(0, ",")) {
            unsigned a, b;
            if (sscanf(s, "%u:%u", &a, &b) == 2 && a < NKEYS && b < NKEYS) {
                host_partner[a] = (uint8_t)b;
                host_partner[b] = (uint8_t)a;
            }
        }
    }
#endif
    unsigned long long t; unsigned c, r, l, p;
    int have = scanf("%llu %u %u %u %u", &t, &c, &r, &l, &p) == 5;
    uint64_t next_tick = 125, next_scan = 0, end = (uint64_t)-1;
    while (1) {
        if (!have && end == (uint64_t)-1) end = now_us + 500000;   /* run 0.5 s after last event */
        if (now_us >= end) break;
        /* advance to next point of interest (1 us resolution) */
        now_us++;
        if (now_us >= next_tick) { chatter_tick_then_encoder(); next_tick += 125; }
        if (now_us >= next_scan) {
            next_scan += scan;
            chatter_flush();                              /* before-hook of adc_sensor_process */
            while (have && t <= now_us) {                 /* key events produced by this scan */
                if (!chatter_key_event_filter(c, r, l, p))
                    key_event_orig(c, r, l, p);
                have = scanf("%llu %u %u %u %u", &t, &c, &r, &l, &p) == 5;
            }
        }
    }
    fprintf(stderr, "stats deferred=%u merged=%u delivered=%u overflow=%u snaptap=%u\n",
            chatter_stats[0], chatter_stats[1], chatter_stats[2], chatter_stats[3], chatter_stats[4]);
    return 0;
}
