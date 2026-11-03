"""Logic test for CHATTERFIX: runs the real handlers.c (compiled with HOST_TEST) on scenarios."""
import csv, os, subprocess, sys
from collections import defaultdict

HARNESS = "/tmp/chatterfix_harness"   # build in WSL from patch/: gcc -O1 -I . -DHOLD_MS=5 -o /tmp/chatterfix_harness test/harness.c
# optional: chatter_test.py logs recorded on STOCK firmware to replay, e.g. CHATTER_LOGS="a.csv;b.csv"
LOGS = [p for p in os.environ.get("CHATTER_LOGS", "").split(";") if p]
HOLD_MS = float(os.environ.get('HOLD_MS', '32'))   # must match the build (-DHOLD_MS)
HOLD_US = int(HOLD_MS * 1000)
G = HOLD_MS * 0.7                 # a chatter gap safely inside the hold window
fails = 0


def run(events, scan_us=125, tick0=0, tmr_col=-1):
    inp = "".join(f"{t} {c} {r} {l} {p}\n" for t, c, r, l, p in sorted(events, key=lambda e: e[0]))
    res = subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", HARNESS, str(scan_us), str(tick0), str(tmr_col)],
                         input=inp, capture_output=True, text=True)
    out = [tuple(map(int, line.split())) for line in res.stdout.split("\n") if line.strip()]
    return out, res.stderr.strip()


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")


def generic_invariants(events, out, scan_us):
    """alternating per key, ends released, presses not delayed, releases delayed ~32 ms"""
    st = defaultdict(int)
    good = True
    for t, c, r, l, p in out:
        k = (c, r, l)
        if p == st[k]:
            good = False
        st[k] = p
    check("per-key press/release alternate", good)
    check("all keys end released", all(v == 0 for v in st.values()))
    in_press = {(t, c, r, l) for t, c, r, l, p in events if p}
    late = [o for o in out if o[4] and not any(o[0] - scan_us - 1 <= t <= o[0] and (c, r, l) == o[1:4]
                                              for t, c, r, l in in_press)]
    check("presses are never delayed", not late, str(late[:3]))
    return st


def host_view(out):
    """returns list of (key, press_t, release_t)"""
    open_, presses = {}, []
    for t, c, r, l, p in out:
        k = (c, r, l)
        if p:
            open_[k] = t
        else:
            presses.append((k, open_.pop(k), t))
    return sorted(presses, key=lambda x: x[1])


def scenario(name, events, scan_us=125, tick0=0, expect=None, tmr_col=-1):
    print(f"\n== {name} (scan {scan_us} us, tick0 {tick0:#x})")
    out, stats = run(events, scan_us, tick0, tmr_col)
    print("  " + stats)
    generic_invariants(events, out, scan_us)
    hv = host_view(out)
    if expect:
        expect(hv, out, stats)
    return hv, out, stats


K = (3, 2, 0)
ms = 1000


def ev(t, k, p):
    return (int(t), *k, p)


for scan in (125, 1000):
    for tick0 in (0, 0xFFFFFF00):  # second run wraps the 32-bit tick counter mid-test
        # 1. mid-press chatter: press, phantom release 5 ms later, re-press 17 ms after that
        scenario("mid-press chatter", [ev(10*ms, K, 1), ev(15*ms, K, 0), ev((15+G)*ms, K, 1), ev(120*ms, K, 0)],
                 scan, tick0, lambda hv, o, s: (
                     check("one host press", len(hv) == 1, str(hv)),
                     check(f"release ~{HOLD_MS:g} ms after real release",
                           hv and HOLD_US <= hv[0][2] - 120*ms <= HOLD_US + scan + 250, str(hv))))
        # 2. after-release chatter: release, phantom press 17 ms later, phantom release 2 ms after
        scenario("after-release chatter", [ev(10*ms, K, 1), ev(90*ms, K, 0), ev((90+G)*ms, K, 1), ev((92+G)*ms, K, 0)],
                 scan, tick0, lambda hv, o, s: check("one host press", len(hv) == 1, str(hv)))
        # 2b. gap just inside the hold window
        scenario(f"after-release chatter, {HOLD_MS-2.5:g} ms gap",
                 [ev(10*ms, K, 1), ev(90*ms, K, 0), ev((90+HOLD_MS-2.5)*ms, K, 1), ev((92+HOLD_MS-2.5)*ms, K, 0)],
                 scan, tick0, lambda hv, o, s: check("one host press", len(hv) == 1, str(hv)))

# 3. fast intentional double taps (release->press gap 35/45/60 ms) must stay two presses
for gap in (HOLD_MS + 3, HOLD_MS + 13, 60):
    scenario(f"intentional double tap, {gap} ms gap",
             [ev(10*ms, K, 1), ev(50*ms, K, 0), ev((50+gap)*ms, K, 1), ev((90+gap)*ms, K, 0)],
             expect=lambda hv, o, s: check("two host presses", len(hv) == 2, str(hv)))
# 3b. a gap under 32 ms is merged (by design)
scenario(f"very fast tap, {HOLD_MS*0.8:g} ms gap (merged by design)",
         [ev(10*ms, K, 1), ev(50*ms, K, 0), ev((50+HOLD_MS*0.8)*ms, K, 1), ev((90+HOLD_MS*0.8)*ms, K, 0)],
         expect=lambda hv, o, s: check("merged into one press", len(hv) == 1, str(hv)))

# 4. held key 3 s, no chatter
scenario("held key", [ev(10*ms, K, 1), ev(3010*ms, K, 0)],
         expect=lambda hv, o, s: check(f"held 3 s + {HOLD_MS:g} ms", len(hv) == 1 and abs(hv[0][2]-hv[0][1]-(3000+HOLD_MS)*ms) < 300, str(hv)))

# 5. rolling / overlapping keys: release of A must not be merged with press of B
A, B = (1, 1, 0), (2, 1, 0)
scenario("roll A->B", [ev(10*ms, A, 1), ev(40*ms, B, 1), ev(45*ms, A, 0), ev(80*ms, B, 0)],
         expect=lambda hv, o, s: check("two presses, A before B", [x[0] for x in hv] == [A, B], str(hv)))
# 5b. same key on different layers is a different key for the filter
scenario("layer change between release and re-press",
         [ev(10*ms, K, 1), ev(40*ms, K, 0), ev(50*ms, (3, 2, 1), 1), ev(90*ms, (3, 2, 1), 0)],
         expect=lambda hv, o, s: check("two presses (different layer)", len(hv) == 2, str(hv)))

# 6. many simultaneous releases (20 > 16 slots), plus chatter on one of them
keys = [(c, r, 0) for r in range(4) for c in range(5)]
evs = [ev(10*ms, k, 1) for k in keys] + [ev(100*ms, k, 0) for k in keys]
scenario("20 simultaneous releases", evs,
         expect=lambda hv, o, s: (check("20 presses, all released", len(hv) == 20),
                                  check("overflow counted (4)", "overflow=4" in s, s)))
evs2 = [ev(10*ms, k, 1) for k in keys] + [ev(100*ms, k, 0) for k in keys] + \
       [ev((100+G)*ms, keys[-1], 1), ev((102+G)*ms, keys[-1], 0)]
scenario("20 releases + chatter on the newest", evs2,
         expect=lambda hv, o, s: check("still 20 presses", len(hv) == 20, str(len(hv))))

# 7. duplicate release while pending (firmware double-release)
scenario("duplicate release", [ev(10*ms, K, 1), ev(50*ms, K, 0), ev(52*ms, K, 0)],
         expect=lambda hv, o, s: check("one press, one release", len(hv) == 1 and len(o) == 2, str(o)))

# 7b. TMR keys (switch type != 7) bypass the hold entirely: stock timing
T = (14, 1, 0)
scenario("TMR key: release not delayed", [ev(10*ms, T, 1), ev(80*ms, T, 0)], tmr_col=14,
         expect=lambda hv, o, s: check("release within one scan", len(hv) == 1 and hv[0][2] - 80*ms <= 125, str(hv)))
scenario("TMR key: rapid re-press stays two presses",
         [ev(10*ms, T, 1), ev(50*ms, T, 0), ev(52*ms, T, 1), ev(90*ms, T, 0)], tmr_col=14,
         expect=lambda hv, o, s: check("two host presses", len(hv) == 2, str(hv)))
scenario("mixed: mech chatter merged, TMR untouched",
         [ev(10*ms, K, 1), ev(10*ms, T, 1), ev(40*ms, K, 0), ev((40+G)*ms, K, 1), ev(60*ms, T, 0), ev(100*ms, K, 0)],
         tmr_col=14,
         expect=lambda hv, o, s: (check("mech key one press", sum(1 for x in hv if x[0] == K) == 1, str(hv)),
                                  check("TMR release not delayed",
                                        any(x[0] == T and x[2] - 60*ms <= 125 for x in hv), str(hv))))

# 8. replay the recorded chatter logs (host-observed events from stock V408 = firmware-side events)
for path in LOGS:
    rows = list(csv.DictReader(open(path, newline="")))
    scan_codes = sorted({(r["scan"], r["ext"]) for r in rows})
    kmap = {sc: (i % 20, i // 20, 0) for i, sc in enumerate(scan_codes)}
    evs, st = [], {}
    for r in rows:
        k = kmap[(r["scan"], r["ext"])]
        p = 1 if r["action"] == "down" else 0
        if st.get(k, 0) == p:      # Windows auto-repeat: skip repeated downs
            continue
        st[k] = p
        evs.append(ev(float(r["t_ms"]) * ms, k, p))
    for k, v in st.items():       # close anything still held at end of log
        if v:
            evs.append(ev(evs[-1][0] + 100*ms, k, 0))
    flagged = sum(1 for r in rows if r["flag"] == "CHATTER")
    def replay_check(hv, out, stats, flagged=flagged, evs=evs):
        # classify every input press by the gap since the same key's previous release
        last_rel, chatter, normal = {}, set(), set()
        for t, c, r, l, p in sorted(evs):
            k = (c, r, l)
            if p:
                gap = t - last_rel[k] if k in last_rel else 10**12
                (chatter if gap < HOLD_US else normal).add((t, k))
            else:
                last_rel[k] = t
        host = defaultdict(list)
        for k, pt, rt in hv:
            host[k].append(pt)
        seen = lambda t, k: any(0 <= pt - t <= 1001 for pt in host[k])   # 1 ms scan quantisation
        leaked = {x for x in chatter if seen(*x)}
        lost = {x for x in normal if not seen(*x)}
        merged = int(stats.split("merged=")[1].split()[0])
        print(f"  input presses {len(chatter)+len(normal)}, presses <32 ms after a release {len(chatter)}, "
              f"flagged CHATTER in log {flagged}, host presses {len(hv)}, merged {merged}")
        check("every chatter re-press is dropped", not leaked, str(sorted(leaked)[:5]))
        check("every normal press reaches the host", not lost, str(sorted(lost)[:5]))
    scenario(f"replay {path.split(chr(92))[-1]}", evs, scan_us=1000, expect=replay_check)

print(f"\n{'ALL PASSED' if fails == 0 else f'{fails} FAILURE(S)'}")
sys.exit(1 if fails else 0)
