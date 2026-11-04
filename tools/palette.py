"""Custom colours for the "dazzle" lighting option (needs CHATTERFIX v5 firmware).

    python palette.py                        show the current palette
    python palette.py ff6600 00ccff          dazzle uses only these colours
    python palette.py ff0000 ff6600 ffcc00   1 to 6 colours (hex RRGGBB, or names below)
    python palette.py off                    back to MonsGeek's rainbow dazzle

Colour names: red green blue orange magenta yellow cyan purple pink white teal amber.

Dazzle normally picks randomly from red, green, blue, orange, magenta and yellow. With a
palette set, those 6 picks are replaced by your colours (repeated: 2 colours -> A B A B A B),
so every dazzle effect uses only your colours. Exceptions: the spiral and ripple effects keep
the original colours. While a palette is set, the fixed colour presets (Fn colour cycling)
also show your colours. The palette is saved on the keyboard.
"""
import sys
import time
from debounce import open_config

NAMES = {"red": "ff0000", "green": "00ff00", "blue": "0000ff", "orange": "ff6000",
         "magenta": "ff00ff", "yellow": "ffff00", "cyan": "00ffff", "purple": "8000ff",
         "pink": "ff40a0", "white": "ffffff", "teal": "00ff80", "amber": "ffa000"}


def palette_cmd(h, n, colours=()):
    msg = [0xEE, n, 0, 0, 0, 0, 0]
    msg.append(255 - (sum(msg) & 255))
    for c in colours:
        msg += list(bytes.fromhex(c))
    msg += [0] * (64 - len(msg))
    h.send_feature_report([0] + msg)
    time.sleep(0.1)
    r = bytes(h.get_feature_report(0, 65))[1:]
    # v5 answers [EE, n<=6, 0 x 6, colours]; older firmware just echoes the request (with checksum)
    if r[0] != 0xEE or r[1] > 6 or any(r[2:8]):
        sys.exit("The keyboard does not support palettes - CHATTERFIX v5 firmware is needed.")
    n = r[1]
    return [r[8 + 3 * i:11 + 3 * i].hex() for i in range(n)]


def parse(c):
    c = NAMES.get(c.lower(), c).lower().lstrip("#")
    if len(c) != 6 or any(ch not in "0123456789abcdef" for ch in c):
        sys.exit(f"not a colour: {c!r} (use RRGGBB hex like ff6600, or a name: {' '.join(NAMES)})")
    return c


def main():
    a = sys.argv[1:]
    h = open_config()
    try:
        if not a:
            cur = palette_cmd(h, 0xFF)
        elif a[0].lower() == "off":
            cur = palette_cmd(h, 0)
        else:
            if len(a) > 6:
                sys.exit("at most 6 colours")
            cur = palette_cmd(h, len(a), [parse(c) for c in a])
        print("dazzle palette: " + (" ".join(cur) if cur else "off (MonsGeek rainbow)"))
    finally:
        h.close()


if __name__ == "__main__":
    main()
