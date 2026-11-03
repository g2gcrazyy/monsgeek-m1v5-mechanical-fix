"""
Read or set the MonsGeek M1 V5 TMR USB polling rate.

    python polling.py          # show current rate
    python polling.py 1000     # set 1000 Hz  (allowed: 8000 4000 2000 1000 500 250 125)

Uses vendor commands GET_REPORT 0x83 / SET_REPORT 0x03 (rate code at byte 2).
"""
import sys

from debounce import open_config, command  # same HID helpers

RATES = [8000, 4000, 2000, 1000, 500, 250, 125]  # index = protocol code


def get_rate(h):
    r = command(h, 0x83)
    if r[0] != 0x83 or r[2] >= len(RATES):
        sys.exit(f"Unexpected reply: {r[:8].hex(' ')}")
    return RATES[r[2]]


def main():
    h = open_config()
    try:
        before = get_rate(h)
        if len(sys.argv) < 2:
            print(f"Polling rate: {before} Hz")
            return
        hz = int(sys.argv[1])
        if hz not in RATES:
            sys.exit(f"Rate must be one of {RATES}")
        command(h, 0x03, 0, RATES.index(hz))
    finally:
        h.close()
    # the keyboard may re-enumerate after a rate change; reopen to verify
    import time
    time.sleep(1.5)
    h = open_config()
    try:
        after = get_rate(h)
    finally:
        h.close()
    print(f"Polling rate: {before} Hz -> {after} Hz" + ("" if after == hz else "  (not applied!)"))


if __name__ == "__main__":
    main()
