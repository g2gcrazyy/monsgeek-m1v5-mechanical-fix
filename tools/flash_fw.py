"""
Flash a firmware image to the MonsGeek M1 V5 TMR (RY5088 / AT32F405) over USB.

    python flash_fw.py firmware_patched.bin          # dry run: validate image, find keyboard
    python flash_fw.py firmware_patched.bin --yes    # actually flash

Image = application firmware starting at flash 0x08005000 (begins with "AT32F405 8KMKB  ").
Protocol (as used by the official ry_upgrade.exe, documented by monsgeek-akko-linux):
  normal mode (3151:5030, usage page FFFF/usage 2):
     ISP_PREPARE  [C5 3A 0 0 0 0 0 chk]      ENTER_BOOTLOADER [7F 55 AA 55 AA 0 0 chk]
     -> keyboard erases its config, reboots into the bootloader (3151:502A, usage page FF01)
  bootloader:
     [BA C0 chunks(2) size(3)]  -> read ack
     N x 64-byte chunks (last padded with FF)
     [BA C2 chunks(2) checksum(4) size(4)]   checksum = sum of all bytes of all 64-byte chunks
  The bootloader only boots the new image if the checksum matches; otherwise it stays in
  bootloader mode and the official ry_upgrade.exe can still flash a stock package.

Recovery if anything goes wrong: run the vendor package's ry_upgrade.exe (it supports the
3151:502A bootloader device) to put stock V408 back.

v2 (2026-11-03), timing taken from a USBPcap capture of ry_upgrade.exe (capture/):
  The bootloader's USB ISR copies every SET_REPORT into ONE 64-byte buffer and sets a flag;
  the main loop then sums/programs/verifies it. There is no flow control, so a chunk that
  arrives before the previous one is processed overwrites it and is lost. v1 sent chunks
  back-to-back -> chunks dropped -> BA C2 was eaten as a data chunk -> stuck in bootloader.
  Now: 1 s pause after BA C0 (ry_upgrade: 1.05 s), >= CHUNK_INTERVAL between chunks
  (ry_upgrade: ~6 ms), last chunk padded with 0x00 (as ry_upgrade), and the BA C2 reply is
  checked: byte 4 = 0x55 means checksum OK and mailbox cleared, 0xAA means rejected.
"""
import argparse
import os
import subprocess
import hashlib
import struct
import sys
import time

sys.path.append(__import__("site").getusersitepackages())
import hid  # pip install hidapi

VID = 0x3151
NORMAL_PID, NORMAL_UP, NORMAL_USAGE = 0x5030, 0xFFFF, 0x02
BOOT_PID, BOOT_UP = 0x502A, 0xFF01
CHIP_ID = b"AT32F405 8KMKB  "
CHUNK = 64
PAD = 0x00                               # ry_upgrade pads the last chunk with zeros
CHUNK_INTERVAL = 0.008                   # s between chunks (ry_upgrade: ~6 ms median, 5.3 ms min)
START_SETTLE = 1.1                       # s after BA C0 before the first chunk (ry_upgrade: 1.05 s)
MAX_SIZE = 0x08028000 - 0x08005000      # must not reach the config region
DEV_ID = 2949                            # MonsGeek M1 V5 TMR (GET_USB_VERSION bytes 1-4)
ATTEMPTS = 3                             # a lost chunk makes the bootloader reject; resend
BOOT_SETTLE = 3.0                        # s to let the bootloader erase the app area after entering it
# stock V408 (to go back to MonsGeek's firmware without ry_upgrade.exe)
STOCK_V408_FILE_SIZE = 132864            # firmware_2949_v408.bin as published by monsgeek-akko-linux
STOCK_V408_SIZE = 0x206F0                # what ry_upgrade.exe sends (USBPcap capture)
STOCK_V408_SHA256 = "f7f89d2d7dca3e4aa9a5a1b84fcdc4eada7cf332b72b199d29e0ab6c81b3f789"


def bit7(cmd, *params):
    m = [cmd] + list(params)
    m += [0] * (7 - len(m))
    m.append(255 - (sum(m[:7]) & 255))
    return m + [0] * (64 - len(m))


def raw(payload):
    return list(payload) + [0] * (64 - len(payload))


def checksum(data):
    # the bootloader sums every byte of every 64-byte chunk it receives (incl. padding)
    total = sum(data)
    if len(data) % CHUNK:
        total += (CHUNK - len(data) % CHUNK) * PAD
    return total


def find(pid, usage_page, usage=None):
    return [d for d in hid.enumerate(VID, pid)
            if d["usage_page"] == usage_page and (usage is None or d["usage"] == usage)]


def validate(data):
    problems = []
    if data[:16] != CHIP_ID:
        problems.append(f"chip ID header is {data[:16]!r}, expected {CHIP_ID!r}")
    if len(data) > MAX_SIZE:
        problems.append(f"image is {len(data)} bytes, would overwrite config (max {MAX_SIZE})")
    sp, reset = struct.unpack_from("<II", data, 0x200)
    if not 0x20000000 < sp <= 0x20018000:
        problems.append(f"initial SP {sp:#x} looks wrong")
    if not (0x08005200 <= (reset & ~1) < 0x08005000 + len(data)) or not reset & 1:
        problems.append(f"reset vector {reset:#x} looks wrong")
    return problems, sp, reset


def fw_info():
    """(device id, firmware version) from GET_USB_VERSION, or (None, None)"""
    devs = find(NORMAL_PID, NORMAL_UP, NORMAL_USAGE)
    if not devs:
        return None, None
    h = hid.device()
    h.open_path(devs[0]["path"])
    try:
        h.send_feature_report([0] + bit7(0x8F))
        time.sleep(0.06)
        r = bytes(h.get_feature_report(0, 65))[1:]
        if r[0] != 0x8F:
            return None, None
        return struct.unpack_from("<I", r, 1)[0], r[7] | (r[8] << 8)
    finally:
        h.close()


def fw_version():
    return fw_info()[1]


def wait_for(pid, usage_page, usage=None, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = find(pid, usage_page, usage)
        if d:
            time.sleep(0.5)          # let enumeration settle
            return find(pid, usage_page, usage)[0]
        time.sleep(0.3)
    return None


def transfer(dev, data, chunks, csum):
    """send the image; True if the bootloader accepted it"""
    ok = False
    b = hid.device()
    b.open_path(dev["path"])
    print("bootloader found, transferring ...")
    size = len(data)
    b.send_feature_report([0] + raw([0xBA, 0xC0, chunks & 0xFF, chunks >> 8,
                                     size & 0xFF, (size >> 8) & 0xFF, (size >> 16) & 0xFF]))
    time.sleep(0.03)
    ack = bytes(b.get_feature_report(0, 65))[1:]
    print(f"start ack: {ack[:8].hex(' ')}")
    if ack[:4] != bytes([0xAB, 0xC0, chunks & 0xFF, chunks >> 8]):
        print("unexpected start ack; not sending data")
        b.close()
        return False
    time.sleep(START_SETTLE)
    t_next = time.perf_counter()
    for i in range(chunks):
        while time.perf_counter() < t_next:          # pace like ry_upgrade; see module docstring
            pass
        t_next = time.perf_counter() + CHUNK_INTERVAL
        chunk = data[i * CHUNK:(i + 1) * CHUNK]
        chunk = chunk + bytes([PAD]) * (CHUNK - len(chunk))
        b.send_feature_report([0] + list(chunk))
        if i % 128 == 0 or i == chunks - 1:
            print(f"\r  {i + 1}/{chunks} chunks", end="", flush=True)
    print()
    while time.perf_counter() < t_next:
        pass
    b.send_feature_report([0] + raw([0xBA, 0xC2, chunks & 0xFF, chunks >> 8]
                                    + list(struct.pack("<I", csum)) + list(struct.pack("<I", size))))
    time.sleep(0.03)
    try:
        fin = bytes(b.get_feature_report(0, 65))[1:]
        print(f"final ack: {fin[:8].hex(' ')}")
        if fin[:2] != b"\xab\xc2":
            print("-> bootloader did not process the complete command (stale reply)")
        elif fin[4] == 0x55:
            print("-> bootloader: checksum OK, image accepted")
            ok = True
        else:
            print(f"-> bootloader REJECTED the image (status {fin[4]:#04x}); "
                  f"its sum {fin[5] | fin[6] << 8 | fin[7] << 16:#08x} vs ours {csum & 0xFFFFFF:#08x}")
    except OSError:
        pass
    try:
        b.close()
    except OSError:
        pass
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--yes", action="store_true", help="really flash (otherwise dry run)")
    ap.add_argument("--any-model", action="store_true", help="skip the device id check (do not use)")
    ap.add_argument("--attempt", type=int, default=1, help=argparse.SUPPRESS)
    args = ap.parse_args()

    data = open(args.image, "rb").read()
    if len(data) == STOCK_V408_FILE_SIZE and hashlib.sha256(data[:STOCK_V408_SIZE]).hexdigest() == STOCK_V408_SHA256:
        # public stock image (firmware_2949_v408.bin) carries a 16-byte vendor trailer that
        # ry_upgrade.exe does not send: send exactly what ry_upgrade sends
        data = data[:STOCK_V408_SIZE]
        print("stock V408 image detected: sending the first 0x206F0 bytes, exactly as ry_upgrade.exe does")
    problems, sp, reset = validate(data)
    chunks = (len(data) + CHUNK - 1) // CHUNK
    csum = checksum(data)
    print(f"image    : {args.image}")
    print(f"size     : {len(data)} bytes, {chunks} chunks, checksum {csum:#x} (24-bit {csum & 0xFFFFFF:#08x})")
    print(f"sha256   : {hashlib.sha256(data).hexdigest()}")
    print(f"vectors  : SP {sp:#010x}, reset {reset:#010x}")
    if problems:
        sys.exit("REFUSING: " + "; ".join(problems))

    normal = find(NORMAL_PID, NORMAL_UP, NORMAL_USAGE)
    boot = find(BOOT_PID, BOOT_UP)
    if normal:
        dev_id, v = fw_info()
        print(f"keyboard : normal mode, device id {dev_id}, firmware v{v:x}" if v is not None
              else "keyboard : normal mode (no version reply)")
        if dev_id != DEV_ID and not args.any_model:
            sys.exit(f"REFUSING: this image is for the MonsGeek M1 V5 TMR (device id {DEV_ID}); "
                     f"this keyboard reports {dev_id}. Flashing another model's firmware can make it unusable.")
    elif boot:
        print("keyboard : already in bootloader mode (model cannot be checked here - only continue if "
              "this is an M1 V5 TMR that was being flashed)")
    else:
        sys.exit("keyboard not found (plug it in by USB cable)")
    if not args.yes:
        print("\nDry run only. Re-run with --yes to flash. Do not unplug the keyboard while flashing.")
        return

    if normal:
        h = hid.device()
        h.open_path(normal[0]["path"])
        print("entering bootloader (the keyboard's stored settings will be reset) ...")
        h.send_feature_report([0] + bit7(0xC5, 0x3A))
        time.sleep(0.05)
        try:
            h.send_feature_report([0] + bit7(0x7F, 0x55, 0xAA, 0x55, 0xAA))
        except OSError:
            pass                      # keyboard reboots immediately; an error here is expected
        try:
            h.close()
        except OSError:
            pass
        time.sleep(1.0)
        dev = wait_for(BOOT_PID, BOOT_UP, timeout=10.0)
        if not dev:
            sys.exit("bootloader did not appear; unplug/replug the keyboard and check its state")
        # The bootloader erases the application area when it starts. Transfers started
        # right after entering it failed several times; ones started later never did.
        print("waiting for the bootloader to finish erasing ...")
        time.sleep(BOOT_SETTLE)
        dev = wait_for(BOOT_PID, BOOT_UP, timeout=10.0) or dev
    else:
        dev = boot[0]

    try:
        ok = transfer(dev, data, chunks, csum)
    except OSError as e:
        print(f"USB error during transfer ({e}); the keyboard stays in bootloader mode")
        ok = False

    if not ok and args.attempt < ATTEMPTS:
        # Retrying inside the same process returned the same stale rejection every time;
        # a fresh run of this script has always worked, so retry as a new process.
        print(f"\nretrying (attempt {args.attempt + 1} of {ATTEMPTS}) - the keyboard waits safely in bootloader mode")
        time.sleep(2.0)
        sys.exit(subprocess.call([sys.executable, os.path.abspath(__file__), args.image, "--yes",
                                  "--attempt", str(args.attempt + 1)]))

    print("waiting for the keyboard to reboot into the new firmware ...")
    if wait_for(NORMAL_PID, NORMAL_UP, NORMAL_USAGE, timeout=20.0):
        v = fw_version()
        print(f"OK: keyboard is back in normal mode, firmware v{v:x}" if v is not None else "OK: keyboard is back")
    elif find(BOOT_PID, BOOT_UP):
        print("Keyboard is still in bootloader mode (checksum rejected or transfer error).")
        print("Run this script again (it works from bootloader mode), or flash stock firmware_2949_v408.bin.")
    else:
        print("Keyboard not detected yet; unplug and replug it, then check.")


if __name__ == "__main__":
    main()
