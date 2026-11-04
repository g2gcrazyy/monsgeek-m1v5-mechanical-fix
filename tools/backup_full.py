"""Read-only full snapshot of the keyboard's settings -> full_backup_<time>.json

Saves: version, polling, profile, LED on/off, debounce, LED params (0x87), side-LED params
(0x88), keyboard options (0x89), keymatrix for 4 profiles x 4 layers (0x8A), Fn layer for
4 profiles x Win/Mac (0x90), macros (0x8B), per-key RGB pictures 0-4 (0x8C) and the
magnetism tables (0xE5), as raw replies. Only GET commands are sent; nothing is changed.

Needs CHATTERFIX v4 firmware for macros: stock V407/V408 has a GET_MACRO bug (index N
reads slot 2N), fixed in v4, so macros are saved per slot ("macros_by_slot").
"""
import datetime
import json
from debounce import open_config, command


def raw(h, cmd, *p):
    return command(h, cmd, *p).hex()


def main():
    h = open_config()
    snap = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "format": "raw 64-byte replies, hex"}
    try:
        for name, cmd in (("usb_version", 0x8F), ("polling", 0x83), ("profile", 0x84), ("led_onoff", 0x85),
                          ("debounce", 0x86), ("led_params", 0x87), ("side_led_params", 0x88),
                          ("kb_options", 0x89)):
            snap[name] = raw(h, cmd)
        print("basic settings read")

        snap["keymatrix"] = {f"profile{p}_layer{l}": "".join(raw(h, 0x8A, p, 0xFF, pg, l) for pg in range(8))
                             for p in range(4) for l in range(4)}
        print("keymatrix read (4 profiles x 4 layers)")
        snap["fn_layer"] = {f"profile{p}_{'win' if s == 0 else 'mac'}": "".join(raw(h, 0x90, s, p, 0xFF, pg) for pg in range(8))
                            for p in range(4) for s in (0, 1)}
        print("Fn layers read")

        snap["macros_by_slot"] = {str(i): "".join(raw(h, 0x8B, i, pg) for pg in range(4)) for i in range(50)}
        print("macros read")
        snap["userpic"] = {str(s): "".join(raw(h, 0x8C, s, 0xFF, b) for b in range(6)) for s in range(5)}
        print("per-key RGB pictures read")

        def mag(sub, pages):
            return "".join(command(h, 0xE5, sub, 1, pg).hex() for pg in range(pages))
        snap["magnetism"] = {name: mag(sub, pages) for name, sub, pages in
                             (("key_mode", 7, 2), ("switch_type", 0xFC, 4), ("press_travel", 0, 4),
                              ("lift_travel", 1, 4), ("rt_press", 2, 4), ("rt_lift", 3, 4),
                              ("bottom_deadzone", 6, 4), ("top_deadzone", 0xFB, 4))}
        print("magnetism tables read")
        r = command(h, 0xEE, 0xFF)                      # v5 dazzle palette (older firmware echoes)
        if r[0] == 0xEE and r[1] <= 6 and not any(r[2:8]):
            snap["dazzle_palette"] = [r[8 + 3 * i:11 + 3 * i].hex() for i in range(r[1])]
        snap["snaptap_partners"] = "".join(command(h, 0xE5, 9, 1, pg).hex() for pg in range(2))
    finally:
        h.close()

    # which macro slots are bound to keys ([9, type, slot, 0] entries)
    used = set()
    for tables in (snap["keymatrix"], snap["fn_layer"]):
        for t in tables.values():
            b = bytes.fromhex(t)
            for k in range(0, len(b) - 3, 4):
                if b[k] == 9:
                    used.add(b[k + 2])
    used.discard(0xFF)
    snap["macro_slots_bound_to_keys"] = sorted(used)

    path = f"full_backup_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"
    with open(path, "w") as f:
        json.dump(snap, f, indent=1)
    st = bytes.fromhex(snap["magnetism"]["switch_type"])[:128]
    print(f"\nsaved {path}")
    print("switch types:", {v: st.count(v) for v in set(st)})
    print("active profile reply:", snap["profile"][:8], "| LED params:", snap["led_params"][:24])
    print("macro slots used by keys:", sorted(used) or "none found")


if __name__ == "__main__":
    main()
