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
#if defined(CHATTER_V4) && defined(CHATTER_PALETTE)
#define VER "v5"
#elif defined(CHATTER_V4)
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
#ifdef CHATTER_PALETTE
    case 0x07: return b[1] < 25;        /* SET_LEDPARAM: only modes 0-24 render; 25-31 hold the palette */
#else
    case 0x07: return b[1] < 32;                                        /* SET_LEDPARAM mode */
#endif
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

#ifdef CHATTER_PALETTE
/* ---- custom "dazzle" palette --------------------------------------------------------
 * V408 has two kinds of dazzle colour: random picks from a 6-colour table (0x08025442)
 * and a red->green->blue colour wheel (0x08006EF6, 22 call sites). Both end up in
 * ws2812_set_pixel(led, brightness, R, G, B), which every effect uses (49 calls).
 * We hook it (0x080070EA, after its bounds check) and, when the colour option is dazzle
 * (LED state 0x20008528 + 30 == 7) and a palette is set, map each saturated colour's
 * hue onto a path through the user's colours (A -> B -> ... -> A, blended), keeping its
 * brightness. Whites/greys and the other colour options are untouched.
 * Stored in the config header in the colour slots of LED modes 25-31 (header+0xCD,
 * 21 bytes; the renderer only handles modes 0-24): [0x50, n, 0x50^n, n x RGB].
 * Vendor command 0xEE [n (0 = off, 0xFF = just read), 0..., chk, n x RGB @ b[8]]. */
#define PAL_SLOTS 0xCDu
#define PAL_MAGIC 0x50u
#define LED_COLOUR_OPTION (*(volatile uint8_t *)0x20008546u)   /* 7 = dazzle */
static uint8_t pal_n, pal_rgb[18];
#ifndef HOST_TEST
static uint8_t pal_ready;
#endif

/* Read the stored palette (n = 0 when none/invalid). */
uint32_t palette_load(const volatile uint8_t *slots, uint8_t *rgb)
{
    uint32_t n = slots[1];
    if (slots[0] != PAL_MAGIC || n < 1 || n > 6 || slots[2] != (PAL_MAGIC ^ n))
        return 0;
    for (uint32_t i = 0; i < n * 3; i++)
        rgb[i] = slots[3 + i];
    return n;
}

/* Map a saturated colour onto the palette path by hue. c = {R, G, B}. 1 if changed. */
int palette_remap(uint8_t *c, uint32_t n, const uint8_t *pal)
{
    int32_t r = c[0], g = c[1], b = c[2];
    int32_t mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
    int32_t mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
    if (n == 0 || mx == 0 || mn * 4 > mx)
        return 0;                               /* black, white, grey, pastel: leave */
    int32_t d = mx - mn, h;                     /* hue 0..1535, 256 per sector */
    if (mx == r)
        h = g >= b ? (g - b) * 256 / d : 1536 - (b - g) * 256 / d;
    else if (mx == g)
        h = 512 + (b - r) * 256 / d;
    else
        h = 1024 + (r - g) * 256 / d;
    if (h >= 1536)
        h -= 1536;
    uint32_t pos = (uint32_t)h * n;             /* 0 .. 1536*n */
    uint32_t i = pos / 1536, f = (pos % 1536) * 256 / 1536, j = (i + 1) % n;
    for (uint32_t k = 0; k < 3; k++) {
        uint32_t v = (pal[i * 3 + k] * (256 - f) + pal[j * 3 + k] * f) >> 8;
        c[k] = (uint8_t)(v * (uint32_t)mx / 255);
    }
    return 1;
}

/* Called from the ws2812_set_pixel hook. regs: saved r0-r3, r12, lr, then the caller's
 * stack argument (B). R = r2, G = r3. */
/* ---- per-effect palette behaviour (dazzle + palette set) ------------------------------
 * "Object" effects (ripple 5, peaks 9, sine 10, snow 17, meteor 18, train 23): our hooks
 * below make each object store its colour as weights, red = amount of A, blue = amount of
 * B (green 0); the output is A*red + B*blue. Fades scale both, so trails keep their colour.
 * Breathing (2): colour follows the breath, A when dim -> B at the peak -> A.
 * Everything else: hue remap (palette_remap). */
#define LED_MODE        (*(volatile uint8_t *)0x20007478u)   /* config header byte 8 */
#define LED_BRIGHTNESS  (*(volatile uint8_t *)0x20008548u)   /* LED state +0x20, percent */

static int is_object_mode(uint32_t m)
{
    return m == 5 || m == 9 || m == 10 || m == 17 || m == 18 || m == 23;
}

/* c = {R, G, B} going to the LED; bright = set_pixel's brightness argument. */
void effect_colour(uint32_t mode, uint8_t *c, uint32_t bright, uint32_t gbright,
                   uint32_t n, const uint8_t *pal)
{
    const uint8_t *A = pal, *B = n >= 2 ? pal + 3 : pal;
    if (mode == 2) {                                    /* breathing */
        uint32_t t = gbright ? bright * 255u / gbright : 255u;
        if (t > 255)
            t = 255;
        for (uint32_t k = 0; k < 3; k++)
            c[k] = (uint8_t)((A[k] * (255u - t) + B[k] * t) / 255u);
    } else if (is_object_mode(mode)) {
        uint32_t wa = c[0], wb = c[2], tot = wa + wb;
        if (tot > 255) {                                /* mix: blend A/B by ratio, full intensity */
            wa = wa * 255u / tot;
            wb = 255u - wa;
        }
        for (uint32_t k = 0; k < 3; k++) {
            uint32_t v = (A[k] * wa + B[k] * wb) / 255u;
            c[k] = (uint8_t)(v > 255 ? 255 : v);
        }
    } else {
        palette_remap(c, n, pal);
    }
}

void chatter_pixel_remap(uint32_t *regs)
{
    if (pal_n == 0 || LED_COLOUR_OPTION != 7)
        return;
    uint8_t c[3] = { (uint8_t)regs[2], (uint8_t)regs[3], (uint8_t)regs[6] };
    effect_colour(LED_MODE, c, regs[1] & 0xFF, LED_BRIGHTNESS, pal_n, pal_rgb);
    regs[2] = c[0];
    regs[3] = c[1];
    regs[6] = c[2];
}

/* ---- object colours ---------------------------------------------------------------------
 * The effects pick dazzle colours with `rand() % 6` into the 6-colour table (index 0 = red,
 * 2 = blue). The rand() calls on those paths are redirected here and return 0 (A) or 2 (B).
 * Effects state: 0x20008528. */
#ifndef HOST_TEST
#define FX ((volatile uint8_t *)0x20008528u)
#define fw_rand ((int (*)(void))0x080054C5u)               /* firmware rand() */
#define led_ripple_effect ((uint32_t (*)(uint32_t, uint32_t, uint32_t))0x08012879u)
#define PAL_ACTIVE (pal_n != 0 && LED_COLOUR_OPTION == 7)

uint32_t snow_hist[16];                 /* per column: flake colours, newest in bit 0 */
static uint8_t snow_next, rip_next, rip_colour[16];
static uint32_t rip_prev;

/* meteor (18): one colour per meteor, alternating; a meteor starts at step 0 (+60) */
static uint8_t met_colour;
int chatter_rand_meteor(void)
{
    if (!PAL_ACTIVE)
        return fw_rand();
    if (FX[60] == 0)
        met_colour ^= 1;
    return met_colour ? 2 : 0;
}

/* peaks (9): blobs are separated by 0xFF in the script table (pointer at 0x08008A84),
 * indexed by +60. Colour = parity of the separators before the current entry. */
int chatter_rand_peaks(void)
{
    if (!PAL_ACTIVE)
        return fw_rand();
    const uint8_t *tbl = *(const uint8_t *const *)0x08008A84u;
    uint32_t idx = FX[60], seps = 0;
    for (uint32_t i = 0; i < idx; i++)
        if (tbl[i] == 0xFF)
            seps++;
    return (seps & 1) ? 2 : 0;
}

/* train (23): even rows run one way, odd rows the other -> row parity (+52, u32) */
int chatter_rand_train(void)
{
    if (!PAL_ACTIVE)
        return fw_rand();
    return (*(volatile uint32_t *)(FX + 52) & 1) ? 2 : 0;
}

/* snow (17): masks at +0x743+col, bit = row, shifted down each tick. On a new flake push
 * the next colour into the column history; an LED at (col,row) belongs to the flake with
 * popcount(newer flakes above it) as its history index. */
int chatter_rand_snow_spawn(void)
{
    int r = fw_rand();
    uint32_t col = (uint32_t)r & 15u;
    if (r >= 0 && !(FX[0x743 + col] & 1)) {
        snow_hist[col] = (snow_hist[col] << 1) | snow_next;
        snow_next ^= 1;
    }
    return r;
}

int chatter_snow_colour(uint32_t col, uint32_t row)
{
    if (!PAL_ACTIVE)
        return fw_rand();
    uint32_t above = FX[0x743 + (col & 15)] & ((1u << row) - 1u), k = 0;
    while (above) {
        above &= above - 1u;
        k++;
    }
    return ((snow_hist[col & 15] >> k) & 1) ? 2 : 0;
}

/* ripple (5) and sine (10) draw through led_ripple_effect(row, centre, radius), which lights
 * up to two idle LEDs: posTbl[row*16 + centre -/+ radius]. Our wrappers note which LEDs it
 * will write, call it, then overwrite their stored colour with our weights. */
#define POS_TBL   ((const uint8_t *)0x080250BDu)          /* 6 rows x 16 LED indices */
#define LED_BUSY  (FX + 0x5A9)                            /* per-LED: lit/fading */
#define LED_RGB   (FX + 0x5FB)                            /* per-LED R,G,B (stride 4) */

static uint8_t add_sat(uint32_t a, uint32_t b)
{
    return (uint8_t)(a + b > 255 ? 255 : a + b);
}

/* mix != 0 (ripple): an LED that is still lit by another object is not redrawn by the
 * firmware; we add our weights to its colour instead, so overlapping ripples mix. */
static uint32_t draw_with_colour(uint32_t row, uint32_t centre, uint32_t radius,
                                 uint8_t wa, uint8_t wb, int mix)
{
    if (!PAL_ACTIVE)
        return led_ripple_effect(row, centre, radius);
    int32_t leds[2] = { -1, -1 };
    uint8_t was_busy[2] = { 0, 0 };
    if (row < 64) {
        if (centre >= radius)
            leds[0] = POS_TBL[row * 16 + centre - radius];
        if (centre + radius <= 15)
            leds[1] = POS_TBL[row * 16 + centre + radius];
        if (radius == 0)
            leds[1] = -1;                           /* same LED twice */
        for (int i = 0; i < 2; i++) {
            if (leds[i] >= 82)
                leds[i] = -1;
            else if (leds[i] >= 0)
                was_busy[i] = LED_BUSY[leds[i]] != 0;
        }
    }
    uint32_t ret = led_ripple_effect(row, centre, radius);
    for (int i = 0; i < 2; i++) {
        if (leds[i] < 0)
            continue;
        volatile uint8_t *p = LED_RGB + leds[i] * 4;
        if (!was_busy[i] && LED_BUSY[leds[i]]) {    /* freshly lit by this draw */
            p[0] = wa;
            p[1] = 0;
            p[2] = wb;
            chatter_stats[6]++;
        } else if (was_busy[i] && mix) {            /* overlap: mix the colours */
            p[0] = add_sat(p[0], wa);
            p[2] = add_sat(p[2], wb);
            LED_BUSY[leds[i]] = 1;                  /* make the crossing light up again */
            chatter_stats[7]++;
        }
    }
    return ret;
}

/* ripple: slots 0-15, struct at +0x81E + s*4 = {row, centre, radius16}; stage masks at
 * +4..+24. New ripples (a slot bit appearing) get the next colour in chatter_flush. */
static void ripple_poll(void)
{
    uint32_t u = 0;
    for (uint32_t m = 0; m < 6; m++)
        u |= *(volatile uint32_t *)(FX + 4 + m * 4);
    u &= 0xFFFF;
    uint32_t fresh = u & ~rip_prev;
    for (uint32_t s = 0; s < 16; s++)
        if (fresh & (1u << s)) {
            rip_colour[s] = rip_next;
            rip_next ^= 1;
        }
    rip_prev = u;
}

uint32_t chatter_ripple_draw(uint32_t row, uint32_t centre, uint32_t radius)
{
    uint32_t colour = 0, dist = radius;
    for (uint32_t s = 0; s < 16; s++) {
        int32_t d = (int32_t)row - (int32_t)FX[0x81E + s * 4];
        if ((rip_prev & (1u << s)) && FX[0x81F + s * 4] == centre && d >= -3 && d <= 3) {
            colour = rip_colour[s];
            dist += (uint32_t)(d < 0 ? -d : d);         /* rows away count as distance too */
            break;
        }
    }
    /* fainter further from the key: 255 * ((9 - dist) / 9)^2, floor 12 */
    int32_t left = 9 - (int32_t)dist;
    int32_t dim = left > 0 ? 255 * left * left / 81 : 0;
    if (dim < 12)
        dim = 12;
    return draw_with_colour(row, centre, radius, colour ? 0 : (uint8_t)dim, colour ? (uint8_t)dim : 0, 1);
}

/* sine: segment slot 0-29 (r6 at the call) -> gradient A -> B -> A over one wave period */
uint32_t chatter_sine_draw(uint32_t row, uint32_t centre, uint32_t radius, uint32_t slot)
{
    uint32_t t = slot < 15 ? slot * 17u : (30u - (slot > 30 ? 30 : slot)) * 17u;
    if (t > 255)
        t = 255;
    return draw_with_colour(row, centre, radius, (uint8_t)(255 - t), (uint8_t)t, 0);
}
#endif /* !HOST_TEST */
#endif

#ifndef HOST_TEST
#define VB ((volatile uint8_t *)0x200082DCu)    /* [0] pending, [2..] host report */
#ifdef CHATTER_PALETTE
#define HDR       ((volatile uint8_t *)0x20007470u)   /* config header (0xEC bytes, saved) */
#define CFG_DIRTY (*(volatile uint8_t *)0x200000FCu)  /* bit 2: save the config header */

static void palette_cmd(volatile uint8_t *b)
{
    uint32_t s = 0;
    for (uint32_t i = 0; i < 8; i++)
        s += b[i];
    uint32_t n = b[1];
    if ((s & 0xFF) == 0xFF && n <= 6) {
        volatile uint8_t *p = HDR + PAL_SLOTS;
        p[0] = n ? PAL_MAGIC : 0;
        p[1] = (uint8_t)n;
        p[2] = n ? (uint8_t)(PAL_MAGIC ^ n) : 0;
        for (uint32_t i = 0; i < 18; i++)
            p[3 + i] = i < n * 3 ? b[8 + i] : 0;
        CFG_DIRTY |= 4;                         /* same as SET_LEDPARAM: save the header */
        pal_ready = 0;
    }
    /* reply: [0xEE, n, 0 x 6, n x RGB] */
    volatile uint8_t *p = HDR + PAL_SLOTS;
    uint32_t cur = (p[0] == PAL_MAGIC && p[1] >= 1 && p[1] <= 6 && p[2] == (PAL_MAGIC ^ p[1])) ? p[1] : 0;
    b[0] = 0xEE;
    b[1] = (uint8_t)cur;
    for (uint32_t i = 2; i < 8; i++)
        b[i] = 0;
    for (uint32_t i = 0; i < 18; i++)
        b[8 + i] = i < cur * 3 ? p[3 + i] : 0;
}
#endif
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
#ifdef CHATTER_PALETTE
    if (VB[2] == 0xEE) {
        palette_cmd(VB + 2);
        VB[1] = 0;
        VB[0] = 0;
        return 1;
    }
    if (VB[2] == 0xEF) {                        /* diagnostic: read 56 bytes of RAM */
        volatile uint8_t *b = VB + 2;
        uint32_t a = b[1] | (uint32_t)b[2] << 8 | (uint32_t)b[3] << 16 | (uint32_t)b[4] << 24;
        for (uint32_t i = 1; i < 8; i++)
            b[i] = 0;
        if (a >= 0x20000000u && a <= 0x20018000u - 56u) {
            b[1] = 1;
            for (uint32_t i = 0; i < 56; i++)
                b[8 + i] = ((volatile uint8_t *)a)[i];
        }
        VB[1] = 0;
        VB[0] = 0;
        return 1;
    }
#endif
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
#ifdef CHATTER_PALETTE
    pal_ready = 0;                              /* header (re)loaded: rebuild the palette */
#endif
}

#ifdef CHATTER_PALETTE
static void palette_poll(void)
{
    if (!pal_ready) {
        pal_n = 0;                              /* the pixel hook may run in between */
        pal_n = (uint8_t)palette_load(HDR + PAL_SLOTS, pal_rgb);
        pal_ready = 1;
    }
    if (LED_MODE == 5)
        ripple_poll();
}
#endif
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
#if defined(CHATTER_PALETTE) && !defined(HOST_TEST)
    palette_poll();
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
