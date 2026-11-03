"""Logic tests for the v4 additions (build the harness with -DCHATTER_V4):
debounce slider -> hold time, Snap Tap for mechanical keys, vendor-command range checks."""
import os
import subprocess
import sys
from collections import defaultdict

HARNESS = "/tmp/chatterfix_harness"
HOLD_MS = float(os.environ.get("HOLD_MS", "5"))
ms = 1000
fails = 0


def run(events, debounce=0, pairs="-", tmr_col=-1):
    inp = "".join(f"{t} {c} {r} {l} {p}\n" for t, c, r, l, p in sorted(events, key=lambda e: e[0]))
    res = subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", HARNESS, "125", "0", str(tmr_col), str(debounce), pairs],
                         input=inp, capture_output=True, text=True)
    return [tuple(map(int, l.split())) for l in res.stdout.split("\n") if l.strip()], res.stderr.strip()


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{'  ' + detail if detail and not cond else ''}")


def host_down_at(out):
    """per-key alternation check + list of (t, set of keys down)"""
    st, good, timeline = defaultdict(int), True, []
    for t, c, r, l, p in out:
        if st[(c, r)] == p:
            good = False
        st[(c, r)] = p
        timeline.append((t, {k for k, v in st.items() if v}))
    return good, all(v == 0 for v in st.values()), timeline


def ev(t, k, p):
    return (int(t), k[0], k[1], 0, p)


A, D, W = (2, 3), (4, 3), (1, 2)          # key index = col*6+row: A=15, D=27
PAIR = "15:27"

print("== debounce slider")
for deb, expect in ((0, HOLD_MS), (20, 20), (1, 1), (99, HOLD_MS)):
    out, _ = run([ev(10*ms, A, 1), ev(100*ms, A, 0)], debounce=deb)
    rel = [t for t, c, r, l, p in out if not p]
    check(f"setting {deb} -> release {expect:g} ms late", rel and expect*ms <= rel[0] - 100*ms <= expect*ms + 250, str(out))
out, _ = run([ev(10*ms, A, 1), ev(100*ms, A, 0), ev(115*ms, A, 1), ev(200*ms, A, 0)], debounce=20)
check("setting 20: 15 ms contact gap is merged", sum(1 for o in out if o[4]) == 1, str(out))
out, _ = run([ev(10*ms, A, 1), ev(100*ms, A, 0), ev(115*ms, A, 1), ev(200*ms, A, 0)], debounce=0)
check(f"setting 0 ({HOLD_MS:g} ms): 15 ms gap stays two presses", sum(1 for o in out if o[4]) == 2, str(out))

print("== Snap Tap")
def snap(name, events, pairs=PAIR, tmr_col=-1, expect=None, exclusive=True):
    out, stats = run(events, pairs=pairs, tmr_col=tmr_col)
    alt, ends, tl = host_down_at(out)
    print(f" {name}   ({stats})")
    check("press/release alternate per key", alt, str(out))
    check("all keys end released", ends, str(out))
    if exclusive:
        check("A and D never down together on the host", not any({A, D} <= s for _, s in tl), str(out))
    if expect:
        expect(out)
    return out

snap("A then D, release D, release A",
     [ev(10*ms, A, 1), ev(50*ms, D, 1), ev(100*ms, D, 0), ev(200*ms, A, 0)],
     expect=lambda o: (check("A released the moment D is pressed", (50*ms, *A, 0, 0) in [(t, c, r, l, p) for t, c, r, l, p in o if t <= 50*ms + 130 and (c, r) == A and not p] or any(t - 50*ms <= 130 and (c, r) == A and not p for t, c, r, l, p in o)),
                       check("D press not delayed", any((c, r) == D and p and t - 50*ms <= 130 for t, c, r, l, p in o)),
                       check("A comes back when D is released", sum(1 for t, c, r, l, p in o if (c, r) == A and p) == 2)))
snap("A then D, release A first, then D",
     [ev(10*ms, A, 1), ev(50*ms, D, 1), ev(100*ms, A, 0), ev(200*ms, D, 0)],
     expect=lambda o: check("A pressed once, D pressed once",
                            sum(1 for x in o if x[1:3] == A and x[4]) == 1 and sum(1 for x in o if x[1:3] == D and x[4]) == 1, str(o)))
snap("fast alternating A/D strafing",
     [ev(10*ms, A, 1), ev(40*ms, D, 1), ev(60*ms, A, 0), ev(70*ms, A, 1), ev(90*ms, D, 0), ev(120*ms, A, 0)])
snap("contact glitch on the losing key does not steal focus",
     [ev(10*ms, A, 1), ev(50*ms, D, 1), ev(80*ms, A, 0), ev(81*ms, A, 1), ev(150*ms, D, 0), ev(200*ms, A, 0)],
     expect=lambda o: check("D stays down until D is released",
                            not any(x[1:3] == D and not x[4] and x[0] < 150*ms for x in o), str(o)))
snap("unpaired key W is not affected",
     [ev(10*ms, A, 1), ev(20*ms, W, 1), ev(50*ms, D, 1), ev(100*ms, W, 0), ev(150*ms, D, 0), ev(200*ms, A, 0)],
     expect=lambda o: check("W pressed once", sum(1 for x in o if x[1:3] == W and x[4]) == 1, str(o)))
snap("no pairs set: both keys can be down (stock behaviour)",
     [ev(10*ms, A, 1), ev(50*ms, D, 1), ev(100*ms, D, 0), ev(200*ms, A, 0)], pairs="-", exclusive=False,
     expect=lambda o: check("A never released early", not any(x[1:3] == A and not x[4] and x[0] < 200*ms for x in o), str(o)))
snap("pair with a TMR key is ignored (TMR has its own Snap Tap)",
     [ev(10*ms, A, 1), ev(50*ms, D, 1), ev(100*ms, D, 0), ev(200*ms, A, 0)], tmr_col=4, exclusive=False,
     expect=lambda o: check("A never released early", not any(x[1:3] == A and not x[4] and x[0] < 200*ms for x in o), str(o)))
snap("one-sided partner entry is ignored", [ev(10*ms, A, 1), ev(50*ms, D, 1), ev(100*ms, D, 0), ev(200*ms, A, 0)],
     pairs="15:27,27:9", exclusive=False,
     expect=lambda o: check("A never released early", not any(x[1:3] == A and not x[4] and x[0] < 200*ms for x in o), str(o)))

print("== vendor command range checks")
# (report bytes, expected accept, expected (length byte index, value) after trimming)
cases = [
    ("04 00", 1, None), ("04 03", 1, None), ("04 04", 0, None), ("04 ff", 0, None),
    ("07 01 04 03 ff 00 00", 1, None), ("07 1f", 1, None), ("07 20", 0, None),
    ("0a 00 ff 09 08 01 03", 1, (4, 8)), ("0a 00 05 00 00 00 00", 1, None), ("0a 04 ff 00 38 00 00", 0, None),
    ("0a 00 ff 00 38 00 04", 0, None), ("0a 00 80 00 00 00 00", 0, None),
    ("0a 00 ff 09 38 01 00", 1, (4, 8)),      # padded last chunk: trimmed, save flag kept
    ("0a 00 ff 0a 38 01 00", 1, (4, 0)),      # chunk past the buffer: nothing written
    ("0b 00 00 38 00", 1, (3, 56)), ("0b 31 04 20 01", 1, (3, 32)), ("0b 32 00 38 00", 0, None),
    ("0b 38 00 38 00", 0, None), ("0b 00 0a 38 00", 1, (3, 0)), ("0b 00 09 38 01", 1, (3, 8)),
    ("0c 04 ff 06 2a 01", 1, (4, 42)), ("0c 04 ff 06 38 01", 1, (4, 56)), ("0c 05 ff 00 38 00", 0, None),
    ("0c 06 ff 00 38 00", 0, None), ("0c 00 7d", 1, None), ("0c 00 80", 0, None),
    ("10 00 03 ff 09 08 01", 1, (5, 8)), ("10 00 03 ff 09 38 01", 1, (5, 8)), ("10 00 04 ff 00 38 00", 0, None),
    ("10 01 00 05", 1, None), ("10 01 00 90", 0, None),
    ("8a 03 ff 07 03", 1, None), ("8a 04 ff 00 00", 0, None), ("8a 00 ff 08 00", 0, None), ("8a 00 ff 00 04", 0, None),
    ("8b 31 03", 1, None), ("8b 32 00", 0, None), ("8b 00 04", 0, None),
    ("8c 04 ff 05", 1, None), ("8c 05 ff 00", 0, None), ("8c 00 ff 06", 0, None),
    ("8f", 1, None), ("83", 1, None), ("06 05", 1, None), ("e5 07 01 00", 1, None),
]


def with_checksum(h):
    b = ([int(x, 16) for x in h.split()] + [0] * 7)[:7]
    return b + [(255 - sum(b)) & 255]


reports = [with_checksum(h) for h, _, _ in cases]
res = subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", HARNESS, "guard"],
                     input="".join(" ".join(f"{x:02x}" for x in r) + "\n" for r in reports),
                     capture_output=True, text=True)
lines = [l.split() for l in res.stdout.split("\n") if l.strip()]
for (h, want, ln), l in zip(cases, lines):
    ok, out = int(l[0]), [int(x, 16) for x in l[1:]]
    good = ok == want
    if ok and ln:
        good = good and out[ln[0]] == ln[1]
    if ok:
        good = good and sum(out) & 255 == 255          # report checksum still valid
    check(f"{h:<24} -> {'accept' if want else 'reject'}{f' (len {ln[1]})' if ln else ''}", good, " ".join(l))
check("all guard cases ran", len(lines) == len(cases), f"{len(lines)}/{len(cases)}")

print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
