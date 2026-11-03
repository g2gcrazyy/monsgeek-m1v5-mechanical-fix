"""Snap Tap pairs for the mechanical keys (needs CHATTERFIX v4 firmware).

    python snaptap.py              show the current pairs
    python snaptap.py A D          pair A and D (pressing one releases the other)
    python snaptap.py W S
    python snaptap.py clear A      remove A's pair
    python snaptap.py clear all    remove every pair

Writes MonsGeek's own per-key Snap Tap partner table (SET_MULTI_MAGNETISM 0x65, sub 9),
the same setting the official app uses for magnetic switches. It is saved on the keyboard.
Note: Valve's CS2 servers kick players who use Snap Tap / SOCD tools - clear it there.
"""
import sys
import time
from debounce import open_config, command

UNBOUND = 0xFF
NAMES = {**{chr(ord("A") + i): 0x04 + i for i in range(26)},
         **{str(i): 0x1E + i - 1 for i in range(1, 10)}, "0": 0x27,
         "SPACE": 0x2C, "RIGHT": 0x4F, "LEFT": 0x50, "DOWN": 0x51, "UP": 0x52,
         "LCTRL": 0xE0, "LSHIFT": 0xE1, "LALT": 0xE2, "TAB": 0x2B, "CAPS": 0x39}


def send_partner(h, key, partner, commit):
    msg = [0x65, 9, 0, key, 1 if commit else 0, 0, 0]
    msg.append(255 - (sum(msg) & 255))
    msg += [partner]
    msg += [0] * (64 - len(msg))
    h.send_feature_report([0] + msg)
    time.sleep(0.25 if commit else 0.05)


def read_partners(h):
    return b"".join(command(h, 0xE5, 9, 1, pg) for pg in range(2))


def key_index(h, name):
    """find the key whose base-layer binding is this HID key (key index = col*6+row)"""
    name = name.upper()
    if name not in NAMES:
        sys.exit(f"unknown key name {name!r}; use one of: {' '.join(NAMES)}")
    profile = command(h, 0x84)[1] & 3
    km = b"".join(command(h, 0x8A, profile, 0xFF, pg, 0) for pg in range(8))
    hits = [k for k in range(96) if km[4 * k] == 0 and km[4 * k + 2] == NAMES[name]]
    if len(hits) != 1:
        sys.exit(f"could not find key {name} in the keymap ({hits})")
    return hits[0]


def show(h):
    st = command(h, 0xE5, 0xFC, 1, 0) + command(h, 0xE5, 0xFC, 1, 1)
    p = read_partners(h)
    profile = command(h, 0x84)[1] & 3
    km = b"".join(command(h, 0x8A, profile, 0xFF, pg, 0) for pg in range(8))
    rev = {v: k for k, v in NAMES.items()}
    name = lambda k: rev.get(km[4 * k + 2], f"key{k}") if km[4 * k] == 0 else f"key{k}"
    pairs = sorted({tuple(sorted((k, p[k]))) for k in range(96) if p[k] < 96 and p[p[k]] == k})
    if not pairs:
        print("no Snap Tap pairs set")
    for a, b in pairs:
        note = "" if st[a] == 7 and st[b] == 7 else "  (not both mechanical: handled by stock firmware)"
        print(f"Snap Tap pair: {name(a)} <-> {name(b)}{note}")


def main():
    a = sys.argv[1:]
    h = open_config()
    try:
        if not a:
            show(h)
            return
        p = read_partners(h)
        if a[0].lower() == "clear":
            if len(a) != 2:
                sys.exit("usage: snaptap.py clear <key|all>")
            keys = [k for k in range(96) if p[k] != UNBOUND] if a[1].lower() == "all" else \
                   [x for k in [key_index(h, a[1])] for x in (k, p[k]) if x < 96]
            for i, k in enumerate(keys):
                send_partner(h, k, UNBOUND, i == len(keys) - 1)
        elif len(a) == 2:
            ka, kb = key_index(h, a[0]), key_index(h, a[1])
            if ka == kb:
                sys.exit("pick two different keys")
            for k in (ka, kb):                  # unpair any old partners first
                if p[k] < 96 and p[k] not in (ka, kb):
                    send_partner(h, p[k], UNBOUND, False)
            send_partner(h, ka, kb, False)
            send_partner(h, kb, ka, True)
        else:
            sys.exit(__doc__)
        show(h)
    finally:
        h.close()


if __name__ == "__main__":
    main()
