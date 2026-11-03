# M1 V5 TMR firmware V408: further bugs found and fixed (report 2)

> **Note (repository copy):** this report was first sent to MonsGeek as a zip file. In this repository the code is in `patch/`, the tools are in `tools/`, and the ready-made firmware is on the Releases page. The current release (v4) contains everything in both reports.

| | |
|---|---|
| Keyboard | MonsGeek M1 V5 TMR, USB VID 0x3151 / PID 0x5030, dev_id 2949 |
| MCU | RY5088 (AT32F405), application at 0x08005000 |
| Base firmware | V408 (custom build from MonsGeek support) |
| Switches | mechanical contact switches (switch type 7) |
| Date | 2026-11-03 |

This follows our first report (double presses with mechanical switches). While building that fix we found more bugs in V408 and fixed them in the same patched firmware. This report lists:

- each bug, with its address in V408;
- how we fixed it;
- how we tested it;
- what we suggest changing in the official firmware.

Addresses refer to V408 loaded at 0x08005000. In the command layouts below, `b[0]` is the command byte of the 64-byte report the host sends (Bit7 checksum at `b[7]`; for 0x07/0x08 the checksum is at `b[8]`).

Bugs 3.1, 3.2, 3.3 and 3.6 were first described for V407 by the open-source monsgeek-akko-linux project. We confirmed them in V408. Bugs 3.4, 3.5 and 3.7 are new findings.

## 1. Summary

| # | Bug | Effect | Status in our firmware |
|---|-----|--------|------------------------|
| 1 | GET_MACRO reads the wrong slot | Macros can't be read back (backups miss odd slots) | Fixed, verified on the keyboard |
| 2 | Debounce setting is never used | The debounce slider in the driver does nothing | Fixed: the slider now sets the mechanical-key debounce |
| 3 | Chunked writes have no size check (0x0A/0x0B/0x0C/0x10) | Host can overwrite RAM next to the staging buffers | Fixed: length trimmed to fit |
| 4 | SET_PROFILE (0x04) profile not checked | An invalid profile is saved, and the keyboard HardFaults on every boot | Fixed: rejected |
| 5 | SET_LEDPARAM (0x07) effect mode not checked | RAM written past the config header | Fixed: rejected |
| 6 | SET_USERPIC slot / SET_MACRO id not checked | Stack overflow; flash pages outside the macro area erased | Fixed: rejected |
| 7 | GET_KEYMATRIX / GET_MACRO / GET_USERPIC indexes not checked | Reads past the end of flash, causing a HardFault | Fixed: rejected |
| 8 | Profile / LED mode from flash not checked at boot | Corrupt config leaves the keyboard in a HardFault loop | Fixed: clamped when loaded |
| 9 | No Snap Tap for mechanical (type-7) keys | The feature is unavailable when mechanical switches are fitted | Added |

## 2. The patched firmware (v4)

`firmware/firmware_v4.bin` (sha256 in `firmware/SHA256SUMS.txt`) contains everything from report 1 plus the fixes here:

- MonsGeek's 15–20 ms type-7 lockout ("stable > 3" gate) is removed.
- Mechanical keys get a 5 ms eager release debounce: presses are sent immediately, and only releases are held.

A comparison against stock V408 shows 22 changed bytes at these addresses: 0x08005860, 0x0800DEA4, 0x0800DFFE, 0x0800E8FA, 0x0800E9F4, 0x08011D48, 0x08012372, 0x08012EAE, 0x08013328 and 0x080137CC. The new code (1,182 bytes) is appended at 0x08025800. RAM use is 356 bytes at 0x20009310, in the unused C-library heap.

## 3. Bugs and fixes

### 3.1 GET_MACRO (0x8B) reads the wrong slot
- **Where:** vendor_command_dispatch, GET_MACRO case at 0x080137B8.
- **The bug:** the read address is `0x0802B800 + index*512 + page*64` (`lsls r0, r0, #9` at 0x080137CC). Saving and playback use 256-byte slots: `flash_save_macro` writes `(index>>3)*2048 + (index&7)*256` (0x0800D3C2–0x0800D3DA).
- **Effect:**
  - GET_MACRO index N returns slot 2N, so odd slots can never be read.
  - High indexes read beyond the end of flash.
  - A user's backup of their macros silently missed half of them.
- **Fix:** the one instruction becomes `lsls r0, r0, #8`.
- **Verified on the keyboard:** after the fix, slots 0 and 2 read back byte-identical to what was saved, and slot 1 is readable.

### 3.2 The debounce setting is never used
- **Where:** SET_DEBOUNCE (0x06) stores the value in config header byte 9 (0x20007479). It is read back by GET_DEBOUNCE and saved to flash, but no key-scan code reads it.
- **Fix:** our mechanical-key debounce reads this byte.
  - 1–50 → that many milliseconds;
  - 0 or out of range → 5 ms.
- The driver's debounce slider now works on mechanical keys.

### 3.3 Chunked writes have no size check
Chunked writes copy `len` bytes to `buffer + chunk*56`, and neither `chunk` nor `len` is checked.

| Command | Layout `b[1..]` | Destination buffer (512 bytes) |
|---|---|---|
| SET_KEYMATRIX 0x0A (handler 0x08006C98) | profile, idx\|FF, chunk, len, save, layer, chk, data@8 | 0x2000755C + layer*512 |
| SET_MACRO 0x0B (0x08006D90) | id, chunk, len, last | cmd buffer + 0x42 (staging) |
| SET_USERPIC 0x0C (0x080137E6) | slot, idx\|FF, chunk, len, last | cmd buffer + 0x42 (staging) |
| SET_FN 0x10 (0x08006B24) | sys, profile, idx\|FF, chunk, len, last | 0x20007D5C (Win) / 0x20007F5C (Mac) |

- **Effect:** a host can write past the end of these buffers into other RAM.
- **Fix:** the length is trimmed so `chunk*56 + len <= 512`, and the report checksum is adjusted to stay valid. The command is not rejected, because the last chunk carries the save/last flag. If the official app pads its last chunk to 56 bytes, that save must still happen.
- **Single-key writes** (`idx != 0xFF`) are rejected for index ≥ 128.

### 3.4 SET_PROFILE (0x04): profile not checked (new finding)
- **Where:** handler at 0x08013506.
- **The bug:** `b[1]` is written straight into config header byte 0 (0x20007470), the config is reloaded and then saved.
- **Effect:**
  - `config_load_all` uses the profile as `0x08028800 + profile*0x800`.
  - Magnetism calibration uses `0x08032000 + profile*0x1000`, including a flash erase.
  - Any value from 14 up goes past the end of flash. The keyboard HardFaults, and because the bad value has been saved, it HardFaults on every boot.
- **Fix:** profile ≥ 4 is rejected.

### 3.5 SET_LEDPARAM (0x07): effect mode not checked (new finding)
- **Where:** handler at 0x080135B4.
- **The bug:** the mode (`b[1]`) is stored at header+8 and used as an index into four arrays:
  - header+0x22, +0x42 and +0x62, 32 entries each;
  - header+0x82 + mode*3, the colours.
- **Effect:** modes ≥ 32 write past the 0xEC-byte config header into other RAM, and the bad value is saved to flash.
- **Fix:** mode ≥ 32 is rejected. The side-LED command (0x08) only writes fixed fields and is safe.

### 3.6 SET_USERPIC slot and SET_MACRO id not checked
- **SET_USERPIC:** `flash_save_userpic` copies the 2 KB flash page onto the stack and writes the slot at `slot*384`. Slot ≥ 6 writes past the stack buffer and overwrites the return address.
- **SET_MACRO:** `flash_save_macro` erases and rewrites `0x0802B800 + (id>>3)*2048`. Id ≥ 56 erases other configuration pages: userpics, keymaps and magnetism calibration.
- **Fix:** userpic slot ≥ 5 and macro id ≥ 50 are rejected. 50 is the maximum the official web app uses.

### 3.7 GET reads past the end of flash (new finding)
The GET commands compute flash addresses from unchecked host bytes:

| Command | Address | Fields | Effect |
|---|---|---|---|
| GET_KEYMATRIX 0x8A | `0x08028800 + profile*2048 + layer*512 + page*64` | profile, idx\|FF, page, layer | reads past the end of flash (0x08040000) → HardFault |
| GET_MACRO 0x8B | `0x0802B800 + …` | — | same |
| GET_USERPIC 0x8C | `0x0802F800 + slot*384 + block*64` | — | same |

- **Fix:** these are rejected:
  - profile or layer ≥ 4;
  - page ≥ 8;
  - macro ≥ 50 or page ≥ 4;
  - userpic slot ≥ 5 or block ≥ 6;
  - single index ≥ 128.
- **Verified on the keyboard:** an out-of-range GET_MACRO (index 60) is dropped, and normal reads still work.

### 3.8 Corrupt config at boot causes a crash loop
- **The bug:** `config_load_all` (0x08012354) loads the 0xEC-byte header from 0x08028000 and uses profile (byte 0) and LED mode (byte 8) as indexes without checking them.
- **Effect:** a header corrupted by a power loss during a save, or one written through bugs 3.4 or 3.5, puts the keyboard in a HardFault loop that only ISP/DFU can recover from.
- **Fix:** the `bl flash_read_to_buffer` at 0x08012372 now calls a wrapper that reads the header the same way, then clamps profile ≥ 4 and mode ≥ 32 to 0.

### 3.9 Snap Tap for mechanical keys (new feature)
- **The gap:** V408 stores a per-key Snap Tap partner (GET/SET_MULTI_MAGNETISM sub 9, at RAM `0x20001410 + 0x5245 + key`, 0xFF = none; key index = col*6 + row). However, `key_actuation_eval` forces type-7 keys to mode Normal, so Snap Tap is unavailable with mechanical switches.
- **What we added:** last-input priority (SOCD) for type-7 keys, using the same partner table.
  - A pair is active only when both keys are type 7 and each points at the other.
  - It works on the debounced events: pressing one key releases its partner on the host, and releasing it brings the partner back if it is still held.
- **Tool:** `tools/snaptap.py` sets pairs using the same command the official app uses for magnetic keys.
- **Tested on the keyboard by the user** with A/D: it works.

## 4. Verification

- **Host logic tests:** `patch/test/` compiles the real `handlers.c` on a PC and runs it against scripted scenarios.
  - `v4_test.py` (82 checks):
    - debounce slider values;
    - Snap Tap: ordering, fast alternation, contact glitches, unpaired keys, one-sided entries, pairs that include a TMR key;
    - 45 vendor-command cases checking accept/reject, the trimmed length, and that the checksum stays valid.
  - `logic_test.py`: all scenarios from report 1 still pass.
- **On the keyboard:**
  - normal typing;
  - macro read-back after the fix;
  - out-of-range command dropped;
  - Snap Tap on A/D.

## 5. Other observations

- **Flashing resets most user settings.** After flashing, the LED effect and colour, macro key bindings and macro contents return to defaults, and the polling rate returns to 8000 Hz. Switch types and magnetism settings are kept, apart from one key's top-deadzone value.
- **Bootloader:** even when chunks are paced like ry_upgrade (8 ms apart), about 1 flash in 6 was rejected with a checksum error (a lost chunk). The bootloader correctly refused the image, and a retry succeeded. The single-buffer bootloader without flow control (report 1, section 5) is the likely cause; acknowledging or NAKing each chunk would fix it.

## 6. Suggestions for the official firmware

1. Change the GET_MACRO stride to 256.
2. Range-check every host-supplied index and length in vendor_command_dispatch before use:
   - profile < 4;
   - layer < 4;
   - LED mode < 32;
   - macro < 50;
   - userpic slot < 5;
   - key index < 128;
   - `chunk*56 + len` within the destination buffer.
3. Validate the config header after loading it from flash, and fall back to defaults when profile or mode is out of range.
4. Use the debounce setting for mechanical (type-7) keys. Report 1 recommends a press-immediate debounce timed in milliseconds.
5. Allow Snap Tap (and possibly other key modes) for type-7 keys, using the debounced on/off state.

## 7. Folder contents

| Path | What |
|---|---|
| `REPORT.md` | this report |
| `firmware/firmware_v4.bin` | patched V408 image (flash at 0x08005000) with all fixes from reports 1 and 2 |
| `firmware/SHA256SUMS.txt` | checksum of the image |
| `patch/handlers.c`, `handlers.S` | patch code (v4 parts under `CHATTER_V4`) |
| `patch/patcher.py`, `build.sh`, `patch.ld`, `v408_symbols.ld` | build: `CHATTER_V4=1 CHATTER_MACROFIX=1 CHATTER_NOGATE=1 CHATTER_HOLD_MS=5 bash build.sh` |
| `patch/test/` | host tests (`v4_test.py`; `logic_test.py`, whose final section replays private keystroke logs that are not included) |
| `HOW_TO_FLASH.md` | step-by-step guide for other owners of this keyboard |
| `tools/flash_fw.py` | paced USB flasher (checks the model is device id 2949, retries automatically) |
| `tools/snaptap.py` | set or clear Snap Tap pairs |
| `tools/backup_full.py`, `restore_settings.py`, `debounce.py`, `polling.py` | settings backup/restore over the vendor protocol; debounce get/set |

This is an unofficial modification, provided for diagnosis. The patch needs stock V408 as input, which MonsGeek already has.
