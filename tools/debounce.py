"""
Read or set the MonsGeek M1 V5 TMR firmware debounce time (ms).

    python debounce.py        # show current value
    python debounce.py 25     # set to 25 ms (0-50 allowed)
    python debounce.py 0      # back to the factory value you had (0 ms)

Uses the keyboard's vendor config interface (GET_DEBOUNCE 0x86 / SET_DEBOUNCE 0x06),
the same commands the official driver uses. The setting is stored on the keyboard.
"""
import sys
import time

sys.path.append(__import__("site").getusersitepackages())
import hid  # pip install hidapi

VID, PID = 0x3151, 0x5030


def open_config():
    devs = [d for d in hid.enumerate(VID, PID) if d["usage_page"] == 0xFFFF and d["usage"] == 2]
    if not devs:
        sys.exit("Keyboard config interface not found (is it plugged in by USB?).")
    h = hid.device()
    h.open_path(devs[0]["path"])
    return h


def command(h, cmd, *params):
    msg = [cmd] + list(params)
    msg += [0] * (7 - len(msg))
    msg.append(255 - (sum(msg[:7]) & 255))   # "Bit7" checksum
    msg += [0] * (64 - len(msg))
    h.send_feature_report([0] + msg)
    time.sleep(0.06)
    return bytes(h.get_feature_report(0, 65))[1:]


def get_debounce(h):
    r = command(h, 0x86)
    if r[0] != 0x86:
        sys.exit(f"Unexpected reply: {r[:8].hex(' ')}")
    return r[1]


def main():
    h = open_config()
    try:
        before = get_debounce(h)
        if len(sys.argv) < 2:
            print(f"Debounce: {before} ms")
            return
        ms = int(sys.argv[1])
        if not 0 <= ms <= 50:
            sys.exit("Debounce must be 0-50 ms.")
        command(h, 0x06, ms)
        time.sleep(0.2)
        after = get_debounce(h)
        print(f"Debounce: {before} ms -> {after} ms" + ("" if after == ms else "  (keyboard did not accept it!)"))
    finally:
        h.close()


if __name__ == "__main__":
    main()
