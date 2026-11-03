#!/usr/bin/env python3
"""
CHATTERFIX build helper (self-contained).

    python3 patcher.py generate   -> hooks_gen.S (trampoline stubs)
    python3 patcher.py patch      -> firmware_patched.bin (needs hook.bin + hook.syms from the build)

Hooks:
  key_event  : filter hook on key_event_process   (0x08011D48)
  scan_flush : before hook on adc_sensor_process  (0x08005860)
Binary patch:
  0x08012EAE : `bl encoder_quadrature_tick` -> `bl chatter_tick_then_encoder`
"""
import hashlib
import os
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = 0x08005000                     # flash address of firmware file offset 0
PATCH_ZONE = 0x08025800
PATCH_ZONE_END = 0x08028000           # config region starts here
# Stock V408 application image for the M1 V5 TMR (dev_id 2949), e.g.
# firmwares/2949-v408/firmware_2949_v408.bin from github.com/echtzeit-solutions/monsgeek-akko-linux,
# saved next to this script as firmware_v408.bin (or set CHATTER_IN).
FW_IN = HERE / os.environ.get("CHATTER_IN", "firmware_v408.bin")
FW_SHA256_REAL = "f7f89d2d7dca3e4aa9a5a1b84fcdc4eada7cf332b72b199d29e0ab6c81b3f789"  # first 0x206F0 bytes
# firmware_v408.bin (from x1) carries 16 bytes of vendor trailer after the real image.
# ry_upgrade.exe sends exactly 0x206F0 bytes (end of the scatter-load data, 0x080256F0).
FW_REAL_SIZE = 0x206F0
FW_OUT = HERE / "firmware_patched.bin"
FW_SHA256 = None                      # filled in below for a sanity check

# name, target, mode, handler, expected first 4 bytes at target
HOOKS = [
    ("key_event", 0x08011D48, "filter", "chatter_key_event_filter", "2de9f84f"),
    ("scan_flush", 0x08005860, "before", "chatter_flush", "2de9f05f"),
]
DEBUG = bool(os.environ.get("CHATTER_DEBUG"))
NOGATE = bool(os.environ.get("CHATTER_NOGATE"))
V4 = bool(os.environ.get("CHATTER_V4"))
if V4:
    # v4: vendor-command range checks (+ debug recorder command when CHATTER_DEBUG)
    HOOKS.append(("vendor_cmd", 0x08013328, "filter", "chatter_vendor_filter", "2de9f047"))
elif DEBUG:
    # diagnostic build: vendor cmd 0xED reads the recorder (handlers.c, CHATTER_DEBUG)
    HOOKS.append(("vendor_cmd", 0x08013328, "filter", "chatter_vendor_cmd", "2de9f047"))
if DEBUG:
    FW_OUT = HERE / "firmware_debug.bin"

# MonsGeek's type-7 "stable > 3 scans" gate (V407 fix). Its counters only advance on the
# 200 Hz TMR4 scan tick (5 ms), so after a contact glitch it locks the key for 15-20 ms.
# Patch the four `cmp <switch_type>, #7` to compare against 255 (no such type) -> type-7
# keys skip the gate like every other type. The per-position special cases stay.
BYTE_PATCHES = []
if NOGATE:
    BYTE_PATCHES = [
        (0x0800DEA4, "072a", "ff2a"),          # hall_sensor_key_eval, falling gate  (cmp r2,#7)
        (0x0800DFFE, "072a", "ff2a"),          # hall_sensor_key_eval, rising gate   (cmp r2,#7)
        (0x0800E8F8, "bcf1070f", "bcf1ff0f"),  # key_analog_depth_calc, rising gate (cmp.w ip,#7)
        (0x0800E9F2, "bcf1070f", "bcf1ff0f"),  # key_analog_depth_calc, falling gate
    ]
    FW_OUT = HERE / ("firmware_debug2.bin" if DEBUG else "firmware_nogate.bin")
# GET_MACRO (0x8B) read stride bug: reads FLASH_MACROS + idx*512 + page*64, but SET_MACRO
# (flash_save_macro: sector (idx>>3)*2048 + (idx&7)*256) and playback use idx*256.
# Fix: `lsls r0, r0, #9` -> `lsls r0, r0, #8`. Read path only; saving/playback untouched.
if os.environ.get("CHATTER_MACROFIX"):
    BYTE_PATCHES.append((0x080137CC, "4002", "0002"))
if os.environ.get("CHATTER_OUT"):
    FW_OUT = HERE / os.environ["CHATTER_OUT"]
BL_PATCHES = [
    # addr, expected bytes, symbol, expected original target
    (0x08012EAE, "07f075f8", "chatter_tick_then_encoder", 0x08019F9C),
]
if V4:
    # config_load_all: `bl flash_read_to_buffer` that loads the config header -> wrapper
    # that clamps a corrupt profile / LED mode (boot crash-loop fix)
    BL_PATCHES.append((0x08012372, "03f0a5fa", "chatter_load_header", 0x080158C0))


def enc_branch(frm, to, link):
    off = to - (frm + 4)
    if not -(1 << 24) <= off < (1 << 24) or off & 1:
        raise ValueError(f"branch out of range/odd: {frm:#x} -> {to:#x}")
    s = (off >> 24) & 1
    i1 = (off >> 23) & 1
    i2 = (off >> 22) & 1
    j1 = (~i1 & 1) ^ s
    j2 = (~i2 & 1) ^ s
    hw1 = 0xF000 | (s << 10) | ((off >> 12) & 0x3FF)
    hw2 = (0xD000 if link else 0x9000) | (j1 << 13) | (j2 << 11) | ((off >> 1) & 0x7FF)
    return struct.pack("<HH", hw1, hw2)


def dec_branch(frm, b):
    hw1, hw2 = struct.unpack("<HH", b)
    s = (hw1 >> 10) & 1
    j1, j2 = (hw2 >> 13) & 1, (hw2 >> 11) & 1
    i1, i2 = (~(j1 ^ s)) & 1, (~(j2 ^ s)) & 1
    off = (s << 24) | (i1 << 23) | (i2 << 22) | ((hw1 & 0x3FF) << 12) | ((hw2 & 0x7FF) << 1)
    if s:
        off -= 1 << 25
    return frm + 4 + off


def selftest():
    # the encoder must reproduce the original BL found in the firmware
    assert enc_branch(0x08012EAE, 0x08019F9C, True).hex() == "07f075f8"
    assert dec_branch(0x08012EAE, bytes.fromhex("07f075f8")) == 0x08019F9C
    for frm, to in [(0x08011D48, 0x08025800), (0x08025900, 0x08011D4C), (0x08005860, 0x08025F00)]:
        for link in (False, True):
            assert dec_branch(frm, enc_branch(frm, to, link)) == to


def words(b):
    return "\n".join(f"    .short 0x{v:04x}" for v in struct.unpack(f"<{len(b)//2}H", b))


def cmd_generate():
    fw = FW_IN.read_bytes()
    out = ["/* generated by patcher.py - do not edit */", "    .syntax unified",
           "    .cpu cortex-m4", "    .thumb", ""]
    for name, target, mode, handler, expect in HOOKS:
        disp = fw[target - BASE:target - BASE + 4]
        if disp.hex() != expect:
            sys.exit(f"{name}: bytes at {target:#x} are {disp.hex()}, expected {expect}")
        back = (target + 4) | 1
        out += [f"    .section .text.hook_{name}, \"ax\", %progbits",
                f"    .global _hook_{name}_stub", "    .thumb_func",
                f"    .type _hook_{name}_stub, %function", f"_hook_{name}_stub:"]
        if mode == "filter":
            out += ["    push {r0-r3, r12, lr}", f"    bl {handler}", "    cmp r0, #0",
                    "    pop {r0-r3, r12, lr}", f"    beq .L_{name}_pass", "    bx lr",
                    f".L_{name}_pass:"]
        else:  # before
            out += ["    push {r0-r3, r12, lr}", f"    bl {handler}", "    pop {r0-r3, r12, lr}"]
        out += [f"    /* displaced: {disp.hex()} */", words(disp),
                f"    ldr r12, =0x{back:08X}", "    bx r12", "    .ltorg",
                f"    .size _hook_{name}_stub, . - _hook_{name}_stub", ""]
    (HERE / "hooks_gen.S").write_text("\n".join(out) + "\n")
    print("wrote hooks_gen.S")


def read_syms():
    syms = {}
    for line in (HERE / "hook.syms").read_text().splitlines():
        parts = line.split()
        if len(parts) == 3:
            syms[parts[2]] = int(parts[0], 16)
    return syms


def cmd_patch():
    selftest()
    fw = bytearray(FW_IN.read_bytes()[:FW_REAL_SIZE])
    if hashlib.sha256(fw).hexdigest() != FW_SHA256_REAL:
        sys.exit(f"{FW_IN.name} is not stock V408 for the M1 V5 TMR (sha256 of the first 0x206F0 bytes differs)")
    hook = (HERE / "hook.bin").read_bytes()
    syms = read_syms()
    if PATCH_ZONE + len(hook) > PATCH_ZONE_END:
        sys.exit("hook.bin does not fit in the patch zone")
    zone_off = PATCH_ZONE - BASE
    if any(b not in (0x00, 0xFF) for b in fw[zone_off:]) if len(fw) > zone_off else False:
        sys.exit("patch zone in input firmware is not empty")
    fw.extend(b"\xff" * (zone_off - len(fw)))
    fw[zone_off:zone_off + len(hook)] = hook
    print(f"spliced hook.bin: {len(hook)} bytes at {PATCH_ZONE:#010x}")

    for name, target, mode, handler, expect in HOOKS:
        off = target - BASE
        if fw[off:off + 4].hex() != expect:
            sys.exit(f"{name}: unexpected bytes at {target:#x}")
        stub = syms[f"_hook_{name}_stub"]
        if not PATCH_ZONE <= stub < PATCH_ZONE_END:
            sys.exit(f"stub {name} outside patch zone: {stub:#x}")
        fw[off:off + 4] = enc_branch(target, stub, False)
        print(f"hook {name:10s} {target:#010x}: B.W -> {stub:#010x} ({fw[off:off+4].hex()})")

    for addr, expect, sym, orig_target in BL_PATCHES:
        off = addr - BASE
        if fw[off:off + 4].hex() != expect or dec_branch(addr, bytes.fromhex(expect)) != orig_target:
            sys.exit(f"unexpected bytes at {addr:#x}")
        fw[off:off + 4] = enc_branch(addr, syms[sym], True)
        print(f"bl patch     {addr:#010x}: BL  -> {syms[sym]:#010x} ({fw[off:off+4].hex()}) [{sym}]")

    for addr, expect, new in BYTE_PATCHES:
        off = addr - BASE
        n = len(expect) // 2
        if fw[off:off + n].hex() != expect:
            sys.exit(f"byte patch: unexpected bytes at {addr:#x}: {fw[off:off+n].hex()}")
        fw[off:off + n] = bytes.fromhex(new)
        print(f"byte patch   {addr:#010x}: {expect} -> {new}")

    # RAM placement check
    for s in ("chatter_ticks", "chatter_stats"):
        if not 0x20009310 <= syms[s] < 0x20009510:
            sys.exit(f"{s} not in heap region: {syms[s]:#x}")
    FW_OUT.write_bytes(fw)
    print(f"wrote {FW_OUT.name}: {len(fw)} bytes, sha256 {hashlib.sha256(fw).hexdigest()[:16]}")


if __name__ == "__main__":
    selftest()
    {"generate": cmd_generate, "patch": cmd_patch}[sys.argv[1]]()
