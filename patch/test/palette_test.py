"""Tests for the custom dazzle palette (harness built with -DCHATTER_V4 -DCHATTER_PALETTE)."""
import subprocess
import sys

HARNESS = "/tmp/chatterfix_harness"
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{'  ' + detail if detail and not cond else ''}")


def slots(colours, magic=0x50, chk=None):
    n = len(colours)
    b = [magic, n, (magic ^ n) if chk is None else chk] + [x for c in colours for x in bytes.fromhex(c)]
    return " ".join(f"{x:02x}" for x in (b + [0] * 21)[:21])


AB = slots(["ff6600", "00ccff"])            # A = orange, B = sky blue
OFF = "00 " * 21
# (name, slots, input RGB, check(n, rgb, changed))
cases = [
    ("off: colour unchanged", OFF, (255, 0, 0), lambda n, c, ch: n == 0 and c == (255, 0, 0) and not ch),
    ("red (hue 0) -> A", AB, (255, 0, 0), lambda n, c, ch: n == 2 and c == (255, 102, 0)),
    ("cyan (hue 180) -> B", AB, (0, 255, 255), lambda n, c, ch: c == (0, 204, 255)),
    ("green -> blend, mostly B", AB, (0, 255, 0),
     lambda n, c, ch: ch and c[2] > 128 and c[0] < 128),
    ("half-bright red -> half-bright A", AB, (128, 0, 0), lambda n, c, ch: c == (128, 51, 0)),
    ("wheel midpoint R=128 G=127 -> between A and B", AB, (128, 127, 0), lambda n, c, ch: ch and c != (128, 127, 0)),
    ("orange table colour -> on the palette path", AB, (255, 128, 0), lambda n, c, ch: ch),
    ("white untouched", AB, (255, 255, 255), lambda n, c, ch: not ch and c == (255, 255, 255)),
    ("grey untouched", AB, (150, 150, 150), lambda n, c, ch: not ch),
    ("pastel untouched", AB, (255, 200, 200), lambda n, c, ch: not ch),
    ("black untouched", AB, (0, 0, 0), lambda n, c, ch: not ch and c == (0, 0, 0)),
    ("one colour: blue -> A", slots(["112233"]), (0, 0, 255), lambda n, c, ch: n == 1 and c == (0x11, 0x22, 0x33)),
    ("six colours load", slots(["010101", "020202", "030303", "040404", "050505", "060606"]), (255, 0, 0),
     lambda n, c, ch: n == 6 and c == (1, 1, 1)),
    ("n = 7 ignored", slots(["ff6600"] * 7)[:62], (255, 0, 0), lambda n, c, ch: n == 0),
    ("bad magic ignored", slots(["ff6600", "00ccff"], magic=0x51), (255, 0, 0), lambda n, c, ch: n == 0),
    ("bad check byte ignored", slots(["ff6600", "00ccff"], chk=0), (255, 0, 0), lambda n, c, ch: n == 0),
    ("factory default slots (ff...) ignored", "ff " * 21, (255, 0, 0), lambda n, c, ch: n == 0),
]
inp = "".join(f"{s} | {r} {g} {b}\n" for _, s, (r, g, b), _ in cases)
res = subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", HARNESS, "palette"], input=inp, capture_output=True, text=True)
outs = [l.split() for l in res.stdout.split("\n") if l.strip()]
for (name, _, _, ok), o in zip(cases, outs):
    n, r, g, b, ch = map(int, o)
    check(name, ok(n, (r, g, b), ch), " ".join(o))
check("all palette cases ran", len(outs) == len(cases), f"{len(outs)}/{len(cases)}")

# a full trip around the colour wheel must stay on the A-B path (no third hue appears)
wheel = []
for x in range(0, 256, 15):
    wheel += [(255 - x, x, 0), (0, 255 - x, x), (x, 0, 255 - x)]
res = subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", HARNESS, "palette"],
                     input="".join(f"{AB} | {r} {g} {b}\n" for r, g, b in wheel), capture_output=True, text=True)
bad = []
for (r, g, b), o in zip(wheel, (l.split() for l in res.stdout.split("\n") if l.strip())):
    _, R, G, B, _ = map(int, o)
    # output = k * ((1-f)*A + f*B) with A = (255,102,0), B = (0,204,255):
    # R = 255k(1-f), B = 255kf  ->  predicted G = k*(102(1-f) + 204f)
    k = (R + B) / 255
    f = B / (R + B) if R + B else 0
    if R + B == 0 or abs(G - k * (102 * (1 - f) + 204 * f)) > 3:
        bad.append((r, g, b, R, G, B))
check("colour wheel stays on the A-B path", not bad, str(bad[:3]))

# per-effect colour: A = (255,102,0), B = (0,204,255)
ecases = [
    ("breathing dim -> A", (2, 0, 75, 255, 0, 0), (255, 102, 0)),
    ("breathing peak -> B", (2, 75, 75, 150, 150, 150), (0, 204, 255)),
    ("breathing half -> blend", (2, 37, 75, 255, 255, 0), None),
    ("meteor red weight -> A", (18, 75, 75, 255, 0, 0), (255, 102, 0)),
    ("meteor blue weight -> B", (18, 75, 75, 0, 0, 255), (0, 204, 255)),
    ("meteor trail half -> half A", (18, 75, 75, 128, 0, 0), (128, 51, 0)),
    ("ripple overlap A+B -> mix (clamped)", (5, 75, 75, 128, 0, 128), (128, 153, 128)),
    ("snow / train / peaks / sine use weights", (23, 75, 75, 0, 0, 255), (0, 204, 255)),
    ("black stays black", (17, 75, 75, 0, 0, 0), (0, 0, 0)),
    ("other modes keep the hue remap", (11, 75, 75, 255, 0, 0), (255, 102, 0)),
]
res = subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", HARNESS, "effect"],
                     input="".join(f"{AB} | {' '.join(map(str, i))}\n" for _, i, _ in ecases),
                     capture_output=True, text=True)
eo = [tuple(map(int, l.split())) for l in res.stdout.split("\n") if l.strip()]
for (name, i, want), got in zip(ecases, eo):
    if want is None:
        check(name, got != (255, 102, 0) and got != (0, 204, 255) and 0 < got[0] < 255, str(got))
    else:
        check(name, all(abs(a - b) <= 1 for a, b in zip(got, want)), str(got))
check("all effect cases ran", len(eo) == len(ecases), f"{len(eo)}/{len(ecases)}")

res = subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", HARNESS, "guard"],
                     input="07 18 00 00 00 00 00 e0\n07 19 00 00 00 00 00 df\n", capture_output=True, text=True)
g = [l.split()[0] for l in res.stdout.split("\n") if l.strip()]
check("SET_LEDPARAM mode 24 accepted, 25 rejected (palette storage)", g == ["1", "0"], str(g))

print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
