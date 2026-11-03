"""Restore settings from a full_backup_*.json made by backup_full.py.

    python restore_settings.py full_backup_XXXX.json          show what would change
    python restore_settings.py full_backup_XXXX.json --apply  write it to the keyboard

Restores: LED effect/colour (0x07), side LED (0x08), key bindings for all profiles and
layers (0x0A, only keys that differ) and macros (0x0B). Macros are only restored when the
backup holds their real contents: backups made on stock / pre-v4 firmware could not read
odd macro slots (GET_MACRO bug), so keys bound to those slots are left as they are.
"""
import json
import sys
import time
from debounce import open_config, command


def send(h, frame, csum_at=7, delay=0.06):
    msg = list(frame) + [0] * (64 - len(frame))
    msg[csum_at] = 0
    msg[csum_at] = (255 - sum(msg[:csum_at + 1])) & 255
    h.send_feature_report([0] + msg)
    time.sleep(delay)


def current_keymatrix(h, p, l):
    return b"".join(command(h, 0x8A, p, 0xFF, pg, l) for pg in range(8))


def backup_macros(bk):
    """slot -> 256 bytes, only for slots whose real contents are in the backup"""
    if "macros_by_slot" in bk:
        return {int(s): bytes.fromhex(v) for s, v in bk["macros_by_slot"].items()}
    # old format: GET index i returned slot 2*i
    return {2 * int(i): bytes.fromhex(v) for i, v in bk["macros_by_get_index"].items() if 2 * int(i) < 50}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    bk = json.load(open(sys.argv[1]))
    apply = "--apply" in sys.argv
    macros = backup_macros(bk)
    h = open_config()
    try:
        plan = []
        cur_led, cur_sled = command(h, 0x87)[:8], command(h, 0x88)[:8]
        led, sled = bytes.fromhex(bk["led_params"])[:8], bytes.fromhex(bk["side_led_params"])[:8]
        if cur_led[1:] != led[1:]:
            plan.append(("LED effect/colour", [0x07, *led[1:8], 0], 8))
        if cur_sled[1:] != sled[1:]:
            plan.append(("side LED", [0x08, *sled[1:8], 0], 8))

        used_slots, skipped = set(), []
        keywrites = []
        for name, table in bk["keymatrix"].items():
            p, l = int(name[7]), int(name[-1])
            want, have = bytes.fromhex(table), current_keymatrix(h, p, l)
            for k in range(128):
                w = want[4 * k:4 * k + 4]
                if w == have[4 * k:4 * k + 4] or len(w) < 4:
                    continue
                if w[0] == 9:                                   # macro binding
                    if w[2] not in macros:
                        skipped.append(f"profile {p} layer {l} key {k}: macro slot {w[2]} not in backup")
                        continue
                    used_slots.add(w[2])
                keywrites.append((f"profile {p} layer {l} key {k}: {have[4*k:4*k+4].hex()} -> {w.hex()}",
                                  [0x0A, p, k, 0, 0, 0, l, 0, *w]))
        for slot in sorted(used_slots):
            data = macros[slot][:256].ljust(256, b"\0")
            for c in range(5):
                chunk = data[c * 56:(c + 1) * 56]
                plan.append((f"macro slot {slot} chunk {c}" if c == 0 else None,
                             [0x0B, slot, c, len(chunk), 1 if c == 4 else 0, 0, 0, 0, *chunk], 7))
        if keywrites:
            keywrites[-1][1][5] = 1                              # last write saves to flash
        for desc, frame in keywrites:
            plan.append((desc, frame, 7))

        for desc, _, _ in plan:
            if desc:
                print(("write  " if apply else "would write  ") + desc)
        for s in skipped:
            print("skip   " + s + "  (re-create it in the MonsGeek app)")
        if not plan:
            print("nothing to restore: keyboard already matches the backup")
        if not apply:
            print("\n(dry run - add --apply to write)")
            return
        for _, frame, at in plan:
            send(h, frame, at, delay=0.08)
        time.sleep(0.5)
        print("done")
    finally:
        h.close()


if __name__ == "__main__":
    main()
