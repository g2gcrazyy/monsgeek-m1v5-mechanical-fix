/*
 * CHATTERFIX — release-deferral filter for MonsGeek M1 V5 TMR firmware V408
 * (dev_id 2949) running mechanical switches.
 *
 * Problem: the stock firmware occasionally emits a phantom release followed by a
 * re-press ~16-27 ms later (either mid-press or right after a real release).
 *
 * Fix: every key event funnels through key_event_process(col, row, layer, pressed).
 * We hook it and:
 *   - release  -> do not send yet; remember it with a timestamp
 *   - press while a release for the same (col,row,layer) is pending
 *              -> cancel the pending release and drop this press, so the host
 *                 simply sees the key stay down (the glitch disappears)
 *   - every scan (adc_sensor_process, main loop) -> send pending releases that
 *     have been stable for HOLD_MS
 * Presses are never delayed. Releases are delayed by HOLD_MS.
 *
 * Time base: a counter incremented from the 8 kHz fast-tick ISR (125 us/tick).
 */
#include <stdint.h>

#define XSTR_(x) #x
#define XSTR(x) XSTR_(x)
#ifdef CHATTER_V4
#define VER "v4"
#else
#define VER "v3"
#endif
#ifndef HOLD_MS                     /* override: -DHOLD_MS=15 (build.sh: CHATTER_HOLD_MS=15) */
#define HOLD_MS     32u
#define CHATTER_TAG "CHATTERFIX v1 hold=32ms"
#else
#define CHATTER_TAG "CHATTERFIX " VER " hold=" XSTR(HOLD_MS) "ms type7-only"
#endif
#define TICKS_PER_MS 8u
#define HOLD_TICKS  (HOLD_MS * TICKS_PER_MS)
#define NSLOTS      16

/* firmware functions (addresses in v408_symbols.ld) */
extern void encoder_quadrature_tick(void);
/* our asm trampoline: runs the ORIGINAL key_event_process, bypassing the filter */
extern void key_event_orig(uint32_t col, uint32_t row, uint32_t layer, uint32_t pressed);

typedef struct {
    uint8_t  col, row, layer, used;
    uint32_t t;                 /* tick when the release arrived */
} slot_t;

/* All in .bss -> placed in the unused heap region, zeroed by crt0 at boot. */
volatile uint32_t chatter_ticks;
static slot_t slots[NSLOTS];
/* stats: [0] releases deferred, [1] glitches merged (re-press cancelled a release),
 *        [2] releases delivered, [3] slot overflows, [4] Snap Tap overrides (v4),
 *        [5] vendor commands rejected by the range checks (v4) */
volatile uint32_t chatter_stats[8];

__attribute__((used, section(".rodata.chatter_tag")))
const char chatter_tag[] = CHATTER_TAG;

#ifndef HOST_TEST
static inline uint32_t irq_save(void)
{
    uint32_t primask;
    __asm volatile ("mrs %0, primask\n cpsid i" : "=r"(primask) :: "memory");
    return primask;
}

static inline void irq_restore(uint32_t primask)
{
    __asm volatile ("msr primask, %0" :: "r"(primask) : "memory");
}
#else
static inline uint32_t irq_save(void) { return 0; }
static inline void irq_restore(uint32_t primask) { (void)primask; }
#endif

/* Replaces the single `bl encoder_quadrature_tick` inside fast_tick_8khz_handler. */
void chatter_tick_then_encoder(void)
{
    chatter_ticks++;
    encoder_quadrature_tick();
}

#ifdef CHATTER_DEBUG
static void dbg_key_event(uint32_t col, uint32_t row, uint32_t pressed);
#endif

/* Per-key switch type as set in the MonsGeek driver. Same byte MonsGeek's own type-7
 * checks read (hall_sensor_key_eval 0x0800DEA0, key_actuation_eval 0x0800F4C0:
 * ldrb [KS + 0x3000 + col*6 + row, #0x735]). Type 7 = mechanical/contact switch.
 * Only type-7 keys get the release hold; TMR keys pass straight through (stock timing). */
#define SW_TYPE_MECH 7u
#ifndef HOST_TEST
#define SW_TYPE(c, r) (*(volatile uint8_t *)(0x20001410u + 0x3735u + (c) * 6u + (r)))
#else
static uint8_t host_sw_type[16 * 6];
#define SW_TYPE(c, r) (host_sw_type[(c) * 6u + (r)])
#endif

static int hold_applies(uint32_t col, uint32_t row)
{
    if (col >= 16 || row >= 6)
        return 1;                               /* outside the 16x6 table: keep the hold */
    return SW_TYPE(col, row) == SW_TYPE_MECH;
}

#ifdef CHATTER_V4
/* ---- debounce time from the driver's debounce slider ------------------------------
 * SET_DEBOUNCE (0x06) stores its value in config header byte 9 (0x20007470 + 9), which
 * stock V408 never reads. 1-50 ms -> that hold; 0 or out of range -> HOLD_MS default. */
#ifndef HOST_TEST
#define CFG_DEBOUNCE (*(volatile uint8_t *)0x20007479u)
#else
static uint8_t host_debounce;
#define CFG_DEBOUNCE host_debounce
#endif
static uint32_t hold_ticks(void)
{
    uint32_t d = CFG_DEBOUNCE;
    if (d == 0 || d > 50)
        d = HOLD_MS;
    return d * TICKS_PER_MS;
}

/* ---- Snap Tap (SOCD last-input priority) for mechanical keys ----------------------
 * Uses MonsGeek's own per-key Snap Tap partner table (GET/SET_MULTI_MAGNETISM sub 9,
 * KS + 0x5245 + key, 0xFF = none; key = col*6 + row). Stock V408 ignores it for type-7
 * keys (key_actuation_eval forces their mode to Normal). A pair is active only when both
 * keys are type 7 and point at each other. Works on the debounced events. */
#define NKEYS 96
#ifndef HOST_TEST
#define SNAP_PARTNER(k) (*(volatile uint8_t *)(0x20006655u + (k)))
#else
static uint8_t host_partner[NKEYS];
#define SNAP_PARTNER(k) (host_partner[k])
#endif
#define ST_LOGICAL 1u   /* key is down after debounce */
#define ST_HOST    2u   /* the host currently sees it down */
#define ST_SUPP    4u   /* held, but released on the host because its partner won */
static uint8_t socd_st[NKEYS], socd_layer[NKEYS];

static int socd_partner(uint32_t col, uint32_t row)
{
    if (col >= 16 || row >= 6)
        return -1;
    uint32_t k = col * 6u + row, p = SNAP_PARTNER(k);
    if (p >= NKEYS || p == k || SNAP_PARTNER(p) != k)
        return -1;
    if (SW_TYPE(col, row) != SW_TYPE_MECH || SW_TYPE(p / 6u, p % 6u) != SW_TYPE_MECH)
        return -1;
    return (int)p;
}

/* Deliver a debounced event to the firmware, applying Snap Tap for paired keys. */
static void emit(uint32_t col, uint32_t row, uint32_t layer, uint32_t pressed)
{
    int pi = socd_partner(col, row);
    if (pi < 0) {
        key_event_orig(col, row, layer, pressed);
        return;
    }
    uint32_t k = col * 6u + row, p = (uint32_t)pi, pc = p / 6u, pr = p % 6u;
    if (pressed) {
        if (socd_st[p] & ST_HOST) {                 /* partner loses: release it on the host */
            key_event_orig(pc, pr, socd_layer[p], 0);
            socd_st[p] = (uint8_t)((socd_st[p] & ~ST_HOST) | ST_SUPP);
            chatter_stats[4]++;
        }
        key_event_orig(col, row, layer, 1);
        socd_st[k] = ST_LOGICAL | ST_HOST;
        socd_layer[k] = (uint8_t)layer;
    } else {
        uint8_t was_supp = socd_st[k] & ST_SUPP;
        socd_st[k] = 0;
        if (!was_supp)                              /* always release unless already released */
            key_event_orig(col, row, layer, 0);
        if ((socd_st[p] & (ST_SUPP | ST_LOGICAL)) == (ST_SUPP | ST_LOGICAL)) {
            key_event_orig(pc, pr, socd_layer[p], 1);   /* partner still held: it comes back */
            socd_st[p] = ST_LOGICAL | ST_HOST;
        }
    }
}

/* ---- range checks on vendor commands (b[0] = command byte, as sent by the host) ----
 * Stock V408 uses these fields unchecked as RAM/flash offsets (staging-buffer overflow,
 * stack overflow in flash_save_userpic, macro saves erasing other flash pages, profile or
 * LED mode out of range, reads past the end of flash). Offsets verified in V408. */
/* Chunked writes copy b[len] bytes to <512-byte buffer> + chunk*56. Instead of dropping
 * an oversized chunk (it may carry the "save" flag), trim its length to what fits and
 * keep the report checksum valid (sum of report bytes 0..7 must be 0xFF). */
static void clamp_len(volatile uint8_t *b, uint32_t li, uint32_t chunk)
{
    uint32_t off = chunk * 56u, max = off >= 512u ? 0u : 512u - off;
    if (b[li] > max) {
        uint8_t d = (uint8_t)(b[li] - max);
        b[li] = (uint8_t)max;
        b[7] = (uint8_t)(b[7] + d);
    }
}

/* 1 = pass on (possibly with a trimmed length), 0 = drop. */
int chatter_cmd_ok(volatile uint8_t *b)
{
    switch (b[0]) {
    case 0x04: return b[1] < 4;                                         /* SET_PROFILE */
    case 0x07: return b[1] < 32;                                        /* SET_LEDPARAM mode */
    case 0x0A:                                                          /* SET_KEYMATRIX */
        if (b[1] >= 4 || b[6] >= 4) return 0;                           /* profile, layer */
        if (b[2] != 0xFF) return b[2] < 128;                            /* single key */
        clamp_len(b, 4, b[3]);
        return 1;
    case 0x0B:                                                          /* SET_MACRO */
        if (b[1] >= 50) return 0;
        clamp_len(b, 3, b[2]);
        return 1;
    case 0x0C:                                                          /* SET_USERPIC */
        if (b[1] >= 5) return 0;
        if (b[2] != 0xFF) return b[2] < 128;
        clamp_len(b, 4, b[3]);
        return 1;
    case 0x10:                                                          /* SET_FN */
        if (b[2] >= 4) return 0;
        if (b[3] != 0xFF) return b[3] < 128;
        clamp_len(b, 5, b[4]);
        return 1;
    case 0x8A: return b[1] < 4 && b[4] < 4 && (b[2] == 0xFF ? b[3] < 8 : b[2] < 128);
    case 0x8B: return b[1] < 50 && b[2] < 4;                            /* GET_MACRO */
    case 0x8C: return b[1] < 5 && (b[2] == 0xFF ? b[3] < 6 : b[2] < 128); /* GET_USERPIC */
    default:   return 1;
    }
}

#ifndef HOST_TEST
#define VB ((volatile uint8_t *)0x200082DCu)    /* [0] pending, [2..] host report */
#ifdef CHATTER_DEBUG
int chatter_vendor_cmd(void);
#endif
/* Filter hook on vendor_command_dispatch. */
int chatter_vendor_filter(void)
{
    if (VB[0] == 0)
        return 0;
    if (!chatter_cmd_ok(VB + 2)) {
        VB[1] = 0;
        VB[0] = 0;                              /* drop the command */
        chatter_stats[5]++;
        return 1;
    }
#ifdef CHATTER_DEBUG
    return chatter_vendor_cmd();
#else
    return 0;
#endif
}

/* Replaces `bl flash_read_to_buffer` that loads the 0xEC-byte config header in
 * config_load_all. A corrupt profile (>= 4) or LED mode (>= 32) would otherwise index
 * past flash / the header and HardFault on every boot. */
extern void flash_read_to_buffer(uint32_t src, void *dst, uint32_t n);
void chatter_load_header(uint32_t src, uint8_t *dst, uint32_t n)
{
    flash_read_to_buffer(src, dst, n);
    if (dst[0] >= 4)
        dst[0] = 0;
    if (dst[8] >= 32)
        dst[8] = 0;
}
#endif /* !HOST_TEST */
#else  /* !CHATTER_V4 */
static uint32_t hold_ticks(void) { return HOLD_TICKS; }
#define emit key_event_orig
#endif

static int find_slot(uint32_t col, uint32_t row, uint32_t layer)
{
    for (int i = 0; i < NSLOTS; i++)
        if (slots[i].used && slots[i].col == col && slots[i].row == row && slots[i].layer == layer)
            return i;
    return -1;
}

/* Filter hook on key_event_process. Return 0 = run original, non-zero = swallow. */
int chatter_key_event_filter(uint32_t col, uint32_t row, uint32_t layer, uint32_t pressed)
{
    if (col >= 32 || row >= 8 || layer >= 8)
        return 0;                               /* unexpected args: never interfere */
#ifdef CHATTER_DEBUG
    dbg_key_event(col, row, pressed);
#endif

    uint32_t pm = irq_save();
    int i = find_slot(col, row, layer);

    if (i < 0 && !hold_applies(col, row)) {    /* TMR key, nothing pending: stock behaviour */
        irq_restore(pm);
        return 0;
    }

    if (pressed) {
        if (i >= 0) {                           /* glitch: release+re-press within HOLD */
            slots[i].used = 0;
            chatter_stats[1]++;
            irq_restore(pm);
            return 1;                           /* key is still down on the host side */
        }
        irq_restore(pm);
#ifdef CHATTER_V4
        if (socd_partner(col, row) >= 0) {      /* Snap Tap pair: deliver it ourselves */
            emit(col, row, layer, 1);
            return 1;
        }
#endif
        return 0;                               /* normal press: pass through immediately */
    }

    /* release */
    if (i >= 0) {                               /* duplicate release while pending */
        irq_restore(pm);
        return 1;
    }
    int f = -1, oldest = 0;
    for (int k = 0; k < NSLOTS; k++) {
        if (!slots[k].used) { f = k; break; }
        if ((int32_t)(slots[k].t - slots[oldest].t) < 0) oldest = k;
    }
    slot_t evict = { 0 };
    if (f < 0) {                                /* table full: deliver the oldest now */
        evict = slots[oldest];
        f = oldest;
        chatter_stats[3]++;
    }
    slots[f].col = (uint8_t)col;
    slots[f].row = (uint8_t)row;
    slots[f].layer = (uint8_t)layer;
    slots[f].t = chatter_ticks;
    slots[f].used = 1;
    chatter_stats[0]++;
    irq_restore(pm);

    if (evict.used) {
        emit(evict.col, evict.row, evict.layer, 0);
        chatter_stats[2]++;
    }
    return 1;
}

#ifdef CHATTER_DEBUG
/* ---------------------------------------------------------------------------
 * Diagnostic recorder (debug build only). Lives at 0x20009600 (bottom of the
 * 16 KB stack area, zeroed by crt0, see patch.ld). Read out with vendor cmd 0xED.
 *
 * Per-key state of the V408 depth pipeline (hall_sensor_key_eval, col = key_event
 * arg0, row = arg1, KS = 0x20001410):
 *   u16 raw    KS+0x2c60+col*12+row*2     u16 top   KS+0x2e58+...
 *   u16 d5c    KS+0x2d5c+...  (tracking)  u16 depth KS+0x0fc0+...  (last depth)
 *   u8  falling KS+0x4020+col*6+row       u8  stable KS+0x47c0+col*6+row
 * ------------------------------------------------------------------------- */
#define KS          0x20001410u
#define U16(off, c, r) (*(volatile uint16_t *)(KS + (off) + (c) * 12u + (r) * 2u))
#define U8(off, c, r)  (*(volatile uint8_t  *)(KS + (off) + (c) * 6u + (r)))
#define EP_N        40
#define EP_KEEP     4
#define GAP_BUCKETS 128             /* 0.5 ms each, 0..63.5 ms */
#define NKEYS       90              /* 15 columns x 6 rows */
#define EP_TICKS    480u            /* 60 ms */

typedef struct { uint16_t dt, raw, depth, d5c, top; uint8_t stable, flags; } dbg_ent_t;   /* 12 B */
typedef struct { uint8_t col, row, n, repress; uint32_t t0; dbg_ent_t e[EP_N]; } dbg_ep_t;
typedef struct {
    uint32_t magic;                 /* 'CFD1' */
    uint32_t loops;                 /* adc_sensor_process calls */
    uint16_t loop_hist[16];         /* ticks between calls: 0..14, 15 = 15+ */
    uint16_t loop_max;
    uint16_t episodes;              /* kept episodes (total, ring index = episodes % EP_KEEP) */
    uint32_t releases_seen;
    dbg_ep_t cur;
    uint8_t  active;
    uint8_t  version;               /* 2 */
    uint8_t  ep_keep;               /* EP_KEEP */
    uint8_t  pad;
    dbg_ep_t keep[EP_KEEP];
    /* v2: firmware-level release -> re-press gap of the same key, all keys */
    uint32_t presses;
    uint32_t gap_over;              /* gaps >= 64 ms */
    uint16_t gap_hist[GAP_BUCKETS];
    uint32_t last_rel[NKEYS];       /* tick of last release, 0 = none */
} dbg_t;

__attribute__((section(".dbg"))) volatile dbg_t dbg;
static uint32_t dbg_last_loop;

static void dbg_add(uint8_t c, uint8_t r, uint8_t flags_extra)
{
    volatile dbg_ep_t *ep = &dbg.cur;
    if (ep->n >= EP_N)
        return;
    dbg_ent_t e;
    e.dt = (uint16_t)(chatter_ticks - ep->t0);
    e.raw = U16(0x2c60, c, r);
    e.depth = U16(0x0fc0, c, r);
    e.d5c = U16(0x2d5c, c, r);
    e.top = U16(0x2e58, c, r);
    e.stable = U8(0x47c0, c, r);
    e.flags = (uint8_t)((U8(0x4020, c, r) ? 1 : 0) | flags_extra);
    if (ep->n && !(flags_extra & 0x80)) {             /* only log state changes */
        volatile dbg_ent_t *p = &ep->e[ep->n - 1];
        if (p->raw == e.raw && p->depth == e.depth && p->d5c == e.d5c &&
            p->stable == e.stable && (p->flags & 1) == (e.flags & 1))
            return;
    }
    ep->e[ep->n++] = e;
}

/* called from the key_event filter with the ORIGINAL firmware event */
static void dbg_key_event(uint32_t col, uint32_t row, uint32_t pressed)
{
    if (col >= 15 || row >= 6)
        return;
    dbg.version = 2;
    dbg.ep_keep = EP_KEEP;
    {
        uint32_t k = col * 6u + row;
        if (!pressed) {
            dbg.last_rel[k] = chatter_ticks | 1u;      /* never 0 */
        } else {
            dbg.presses++;
            if (dbg.last_rel[k]) {
                uint32_t gap = chatter_ticks - dbg.last_rel[k];
                if (gap < GAP_BUCKETS * 4u)
                    dbg.gap_hist[gap / 4u]++;
                else
                    dbg.gap_over++;
            }
        }
    }
    if (!pressed)
        dbg.releases_seen++;
    if (!dbg.active) {
        if (pressed)
            return;
        dbg.magic = 0x31444643;     /* "CFD1" */
        dbg.cur.col = (uint8_t)col;
        dbg.cur.row = (uint8_t)row;
        dbg.cur.n = 0;
        dbg.cur.repress = 0;
        dbg.cur.t0 = chatter_ticks;
        dbg.active = 1;
    } else if (dbg.cur.col != col || dbg.cur.row != row) {
        return;
    }
    if (pressed)
        dbg.cur.repress = 1;
    dbg_add((uint8_t)col, (uint8_t)row, (uint8_t)(0x80 | (pressed ? 0x40 : 0)));
}

static void dbg_loop(void)
{
    uint32_t now = chatter_ticks, dt = now - dbg_last_loop;
    dbg_last_loop = now;
    dbg.loops++;
    dbg.loop_hist[dt > 15 ? 15 : dt]++;
    if (dt > dbg.loop_max && dbg.loops > 1)
        dbg.loop_max = (uint16_t)(dt > 0xffff ? 0xffff : dt);
    if (!dbg.active)
        return;
    dbg_add(dbg.cur.col, dbg.cur.row, 0);
    if (now - dbg.cur.t0 >= EP_TICKS || dbg.cur.n >= EP_N) {
        if (dbg.cur.repress) {
            volatile dbg_ep_t *dst = &dbg.keep[dbg.episodes % EP_KEEP];
            volatile uint8_t *d = (volatile uint8_t *)dst;
            const volatile uint8_t *s = (const volatile uint8_t *)&dbg.cur;
            for (unsigned i = 0; i < sizeof(dbg_ep_t); i++)
                d[i] = s[i];
            dbg.episodes++;
        }
        dbg.active = 0;
    }
}

/* filter hook on vendor_command_dispatch (0x08013328). Buffer at 0x200082DC:
 * [0] = pending flag, [2] = command, [3..] = params / reply. GET_REPORT returns [2..65].
 * cmd 0xED, param p = page: reply [3..62] = 60 bytes of dbg at offset p*60. */
#define VBUF ((volatile uint8_t *)0x200082DCu)
int chatter_vendor_cmd(void)
{
    if (VBUF[0] == 0 || VBUF[2] != 0xED)
        return 0;
    VBUF[1] = 0;                                /* as the original dispatcher does first */
    uint32_t off = (uint32_t)VBUF[3] * 60u;
    const volatile uint8_t *src = (const volatile uint8_t *)&dbg;
    for (unsigned i = 0; i < 60; i++)
        VBUF[3 + i] = (off + i < sizeof(dbg_t)) ? src[off + i] : 0;
    VBUF[63] = (uint8_t)(sizeof(dbg_t) & 0xff);
    VBUF[64] = (uint8_t)(sizeof(dbg_t) >> 8);
    VBUF[0] = 0;
    return 1;
}
#endif

/* "before" hook on adc_sensor_process: runs every scan in the main loop. */
void chatter_flush(void)
{
#ifdef CHATTER_DEBUG
    dbg_loop();
#endif
    uint32_t now = chatter_ticks, hold = hold_ticks();
    for (int i = 0; i < NSLOTS; i++) {
        uint32_t pm = irq_save();
        if (!slots[i].used || (now - slots[i].t) < hold) {
            irq_restore(pm);
            continue;
        }
        slot_t s = slots[i];
        slots[i].used = 0;
        irq_restore(pm);
        emit(s.col, s.row, s.layer, 0);
        chatter_stats[2]++;
    }
}
