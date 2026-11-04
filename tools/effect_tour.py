"""Show every lighting mode for a few seconds and print its number, then restore your lighting.

    python effect_tour.py          modes 1-24, 5 s each
    python effect_tour.py 2 18 23  only these modes
    python effect_tour.py --now    just print the current mode number

Each mode is shown with dazzle (so the custom palette is visible) and your current speed
and brightness; those become that mode's saved settings. Your active mode and its settings
are written back at the end (also on Ctrl+C).
"""
import sys
import time
from debounce import open_config, command


def set_led(h, p):
    """p = the 8 bytes of a GET_LEDPARAM reply: [87, mode, speed, brightness, option, R, G, B]"""
    msg = [0x07, *p[1:8], 0]
    msg[8] = (255 - sum(msg[:8])) & 255           # LED commands carry the checksum at byte 8
    msg += [0] * (64 - len(msg))
    h.send_feature_report([0] + msg)
    time.sleep(0.15)


def main():
    a = sys.argv[1:]
    h = open_config()
    try:
        orig = command(h, 0x87)[:8]
        if a == ["--now"]:
            print(f"current mode {orig[1]}, colour option {orig[4] & 15}")
            return
        modes = [int(x) for x in a] or list(range(1, 25))
        print(f"your lighting: mode {orig[1]} (will be restored)\n")
        try:
            for m in modes:
                # each mode keeps its own speed/brightness/option; the tour sets them to
                # your current speed and brightness with dazzle
                set_led(h, [0x87, m, *orig[2:4], (orig[4] & 0xF0) | 7, *orig[5:8]])
                print(f"  mode {m:2d}", flush=True)
                time.sleep(5)
        finally:
            set_led(h, orig)
            print("\nrestored your lighting")
    finally:
        h.close()


if __name__ == "__main__":
    main()
