# M1 V5 TMR with mechanical switches: double-press root cause and firmware fix

> **Note (repository copy):** this report was first sent to MonsGeek as a zip file. In this repository the code is in `patch/`, the tools are in `tools/`, and the ready-made firmware is on the Releases page. The current release (v4) contains everything in both reports.

| | |
|---|---|
| Keyboard | MonsGeek M1 V5 TMR, USB VID 0x3151 / PID 0x5030, dev_id 2949 |
| MCU | RY5088 (AT32F405), application at 0x08005000 |
| Base firmware | V408 (the custom build MonsGeek support sent for mechanical switches) |
| Switches | plain mechanical contact switches in every position; no TMR switches fitted |
| Date | 2026-11-03 |

## 1. Summary

When mechanical switches are fitted to this board, single key presses are sometimes reported twice. V405 and V410 do this often. The custom V408 from MonsGeek support reduces it, but a measured **1.1–1.3 %** of presses were still doubled.

We traced the remaining double presses in V408 to three things working together:

1. **The firmware has no debounce on contact openings.** The debounce setting in the driver is stored but never used. Any brief opening of the contact is sent to the PC as a release in the same scan.
2. **The "stable > 3 scans" gate added for switch type 7 in V407/V408 runs on a 5 ms tick.** Its counters only advance on the 200 Hz TMR4 tick. After a short opening, the key is locked out for 15–20 ms. That turns a sub-millisecond contact flicker into a clearly visible release followed by a press 15–20 ms later, which is exactly the delay seen in every logged double press.
3. **Mechanical switches bounce at every press and release.** This is normal: we measured under 1.5 ms. Without debounce, each bounce is a chance to trigger point 2.

We patched V408 to fix this:

- the type-7 gate is disabled;
- an eager release debounce is added, only for keys set to switch type 7.

During normal typing the patched firmware produced **0 double presses in 1,015 presses**. Presses are reported with no added delay, and releases are delayed by 5 ms.

The patched image, its source code and the test tools are included in this folder. Section 6 suggests how MonsGeek could make the same fix in the official firmware.

## 2. Symptom measurements (stock V408)

Measured with `tools/chatter_test.py`, which timestamps the key-down and key-up events Windows receives:

- One test: **15 double presses in 1,339 presses (1.12 %)** on 14 different keys. A second test gave a similar rate (~1.3 %).
- The phantom re-press always arrived **15.0–27.5 ms** after the release, clustered around 16–18 ms. It is a fixed delay rather than a random one, which points to firmware timing and not a worn switch.
- It happens in two patterns:
  - **mid-press:** press → phantom release 1–16 ms later → phantom press ~17 ms after that;
  - **after release:** real release → phantom press ~17 ms later.
- Changing the debounce setting from 0 to 25 ms had **no effect**. Changing the polling rate had no effect either.

## 3. Root cause (V408)

Addresses refer to V408 as loaded at 0x08005000. Function names come from the community symbol file (from the open-source monsgeek-akko-linux project); we checked them against V408.

### 3.1 How contact switches are read
A contact switch on this board is read through the analog (RC) input that normally serves TMR sensors:

- contact open: raw ≈ 22,000–23,000;
- contact closed: raw ≈ 0–300;
- transitions between the two are exponential, with τ ≈ 1–1.5 ms.

When raw rises to near the key's top reference value, the computed depth is forced to 0 and the release is sent in the same scan. Nothing waits to see whether the contact closes again.

### 3.2 The debounce setting is not used
The debounce value (RAM 0x20007479) is written by SET_DEBOUNCE, read back by GET_DEBOUNCE and saved to flash. No key-scan code reads it. The function named `key_debounce_process` in the symbol file is not a per-key debounce.

### 3.3 The type-7 gate runs on a 5 ms tick
The V407/V408 mechanical fix added switch type 7 to the "stable > 3" gate in four places:

| Function | Address | Instruction |
|---|---|---|
| hall_sensor_key_eval (falling) | 0x0800DEA4 | `cmp r2, #7` |
| hall_sensor_key_eval (rising) | 0x0800DFFE | `cmp r2, #7` |
| key_analog_depth_calc (rising) | 0x0800E8F8 | `cmp.w ip, #7` |
| key_analog_depth_calc (falling) | 0x0800E9F2 | `cmp.w ip, #7` |

The gate blocks evaluation of the key until a per-key counter exceeds 3. The counters are at key-state offset +0x7C0 for falling and +0x742 for rising. They only advance in `key_per_switch_state_machine`, which runs on the **TMR4 scan tick: 216 MHz / 216 / 5000 = 200 Hz, i.e. every 5 ms**. Key evaluation itself runs about every 125 µs.

The sequence is:

1. The contact opens briefly, by a bounce or a flicker.
2. With no debounce, the release is sent immediately.
3. The contact closes again within about 1 ms.
4. The gate refuses to re-evaluate the key until 4 ticks of 5 ms have passed.
5. The press is sent **15–20 ms after the release**.

The gate does not stop the double press. It makes it longer and easier to notice.

### 3.4 Evidence from diagnostic builds
We added a RAM recorder to the firmware. It stores per-scan raw / depth / state values around each release and re-press, and is read out through a spare vendor command.

- **Gate active (stock logic):** a re-press after a short opening is always held until the 5 ms counter reaches 4, giving 15–20 ms. This matches the host logs.
- **Gate disabled:** the re-press is sent **0.1–0.3 ms** after the contact closes again.
- **Bounce during normal typing** (gate disabled, 1,083 releases):

  | Gap from release to the next press of the same key | Count |
  |---|---|
  | 0–1.5 ms (switch bounce) | 41 |
  | 1.5–53 ms | **0** |
  | ≥ 53 ms (the user pressing again) | 1,042 |

  Healthy switches therefore settle in under 1.5 ms, and a 5 ms debounce removes every bounce with margin.
- Some bounces happen while the contact is still closed: the top reference drifts down (e.g. to ~2,800), and a small wobble in raw then reads as depth 0. A debounce covers this case too.
- Very hard key-mashing occasionally produced genuine 16–26 ms contact openings, as the keycap rebounds off the finger. Any keyboard registers those as a second press, so they are outside the scope of this fix.

### 3.5 Notes on other versions
- **V405** has no type-7 handling at all.
- **V410** keeps the type-7 forced actuation (press 1.80 mm / release 0.50 mm) and the `key_analog_depth_calc` gates. However, its rewritten `hall_sensor_key_eval` / `hall_eval_mag_state` no longer contain the two gate blocks. With mechanical switches the user saw frequent ~18 ms doubles on V410 as well.
  - We did not investigate V410 in depth.
  - As far as we can tell it has the same missing debounce, so the same fix (section 6) would apply.

## 4. The patch (based on V408)

**Image:** `firmware/firmware_v3_hold5_type7.bin`, 133,630 bytes, sha256 in `firmware/SHA256SUMS.txt`.

### What it changes compared with stock V408
1. **The type-7 gate is disabled.** At the four addresses in 3.3, the comparison constant is changed from 7 to 255, a switch type that doesn't exist. Type-7 keys now skip the gate, as all other types already did. The other special cases near those instructions are unchanged.
2. **An eager release debounce is added, for type-7 keys only:**
   - **Press:** sent to the PC immediately, with no added delay.
   - **Release:** held for 5 ms. If the same key and layer is pressed again within those 5 ms, both events are dropped, so the PC sees the key stay down. Otherwise the release is sent once the 5 ms have passed.
   - **Which keys:** the filter reads each key's switch type from the same byte MonsGeek's own type-7 checks use: `[key_state + 0x3000 + col*6 + row + 0x735]`, i.e. 0x20004B45 + col*6 + row.
     - Keys of any other type, such as TMR switches and the rotary knob (type 24), pass through unchanged with stock timing.
3. Nothing else is changed: actuation, RGB, the knob, Fn layers and connectivity all behave as in stock.

### Where the patch hooks in

| What | Address | Change |
|---|---|---|
| Filter on `key_event_process(col, row, layer, pressed)` | 0x08011D48 | first 4 bytes → `B.W` to a stub. The stub calls the filter, then runs the displaced instructions and continues. |
| Flush of pending releases, at the start of `adc_sensor_process` (main loop) | 0x08005860 | first 4 bytes → `B.W` to a stub |
| 125 µs time base | 0x08012EAE in `fast_tick_8khz_handler` | `bl encoder_quadrature_tick` → `bl chatter_tick_then_encoder`. This adds one to a counter, then tail-calls the original encoder routine. |
| Type-7 gate | 0x0800DEA4, 0x0800DFFE, 0x0800E8F8, 0x0800E9F2 | `#7` → `#255` |
| Patch code | 0x08025800–0x080259FD | 510 bytes, in free flash below the config area at 0x08028000 |
| Patch RAM | 0x20009310 | ~150 bytes in the unused C-library heap region, which is zeroed at reset |

A byte-for-byte comparison against stock V408 (as sent by ry_upgrade, 0x206F0 bytes) shows 15 changed bytes, at exactly the addresses above. Everything else is identical, and the patch code is appended after the image.

### How it was verified
- **Logic test:** `patch/test/` compiles the real `handlers.c` on a PC and runs it against scripted scenarios:
  - mid-press and after-release chatter;
  - fast intentional double taps;
  - rolls, layer changes, 20 simultaneous releases, duplicate releases, and 32-bit timer wraparound;
  - TMR-type keys bypassing the filter, and mixed mechanical/TMR keys.

  All scenarios pass.
- **On the keyboard:**

  | Firmware | Test | Double presses |
  |---|---|---|
  | Stock V408 | 16-minute typing session | 15 / 1,339 (1.12 %) |
  | Patch, 32 ms hold, gate active | mixed typing and mashing | 2 / 1,086 (0.18 %), both genuine re-presses |
  | Patch, 15 ms hold, gate disabled | heavy mashing | 9 / 3,354 (0.26 %), all physical 16–26 ms openings from mashing |
  | **Patch, 5 ms hold, gate disabled** | **normal typing** | **0 / 1,015** |

## 5. Flashing notes (bootloader finding)

Third-party flashers (for example `iot_driver` from monsgeek-akko-linux) left this keyboard stuck in the bootloader, even when flashing stock V408. We captured a normal `ry_upgrade.exe` update with USBPcap and disassembled the V408 bootloader:

- **Single buffer, no flow control:** the bootloader's SET_REPORT callback (0x080008A4) copies each 64-byte chunk into one buffer and sets a flag. The main loop (0x080007F0) then programs that chunk. Nothing tells the host to wait (no NAK), so chunks sent back-to-back overwrite each other.
  - The chunk count then never reaches the expected total.
  - The final BA C2 command gets treated as a data chunk.
- **Why ry_upgrade works:** it waits ~6 ms between chunks and 1.05 s after BA C0. It sends a size of 0x206F0 and pads with 0x00.

`tools/flash_fw.py` copies this timing (8 ms between chunks, 1.1 s after the start acknowledgement) and reports the bootloader's final verdict: 0x55 accepted, 0xAA rejected. Even so, one of about ten flashes was rejected with a checksum mismatch, meaning a chunk was lost. The bootloader handled this safely: the image was not marked valid, and an immediate retry succeeded.

**Suggestion:** have the bootloader NAK, or acknowledge each chunk, until the previous chunk has been programmed. That would make updates reliable for any host timing.

Flashing resets the keyboard's settings, and the polling rate comes back as 8000 Hz.

## 6. Suggested fix for the official firmware

For keys of switch type 7 (mechanical / contact):

1. **Debounce in the fast scan path, timed in milliseconds:**
   - send a press immediately;
   - send a release only after the contact has stayed open for N ms.
   - Use the existing debounce setting (0x20007479) as N, default about 5 ms.
   - Do not delay presses; that matters to gamers.
2. **Remove the "stable > 3" gate for type 7, or run its counter on the ~125 µs evaluation path instead of the 5 ms TMR4 tick.** With debounce in place the gate is not needed, and on the 5 ms tick it adds 15–20 ms to any re-press.
3. Apply both changes to future versions (V410 and later). Only type-7 keys need them, so TMR behaviour and rapid trigger are unaffected.

## 7. Folder contents

| Path | What |
|---|---|
| `REPORT.md` | this report |
| `firmware/firmware_v3_hold5_type7.bin` | patched V408 application image (flash at 0x08005000) |
| `firmware/SHA256SUMS.txt` | checksum of the image |
| `patch/handlers.c` | filter, flush and time-base code (C) |
| `patch/handlers.S` | trampoline that calls the original `key_event_process` |
| `patch/patcher.py` | generates the hook stubs and patches V408 (hooks, gate bytes, size cut to 0x206F0) |
| `patch/build.sh` | build script: arm-none-eabi-gcc 10.3; `CHATTER_NOGATE=1 CHATTER_HOLD_MS=5 bash build.sh` |
| `patch/patch.ld`, `v408_symbols.ld` | linker script and the two V408 addresses the patch calls |
| `patch/test/` | host logic test (`harness.c`, `logic_test.py`). Its last section replays private keystroke logs that are not included, so it stops there. |
| `tools/flash_fw.py` | paced USB flasher, timing copied from ry_upgrade |
| `tools/chatter_test.py` | Windows tool that measures double presses from host key events |

The patch needs V408 (`firmware_v408.bin`) as input, which is not included because MonsGeek already has it. This is an unofficial modification, provided for diagnosis.
