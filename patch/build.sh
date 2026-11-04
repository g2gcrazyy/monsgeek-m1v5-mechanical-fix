#!/bin/bash
# Build CHATTERFIX (run inside WSL):  bash build.sh
set -euo pipefail
cd "$(dirname "$0")"
# arm-none-eabi toolchain must be on PATH (e.g. Ubuntu: sudo apt install gcc-arm-none-eabi),
# or set ARM_TOOLCHAIN=/path/to/gcc-arm-none-eabi/bin
if [ -n "${ARM_TOOLCHAIN:-}" ]; then export PATH="$ARM_TOOLCHAIN:$PATH"; fi
P=arm-none-eabi-
CFLAGS="-mcpu=cortex-m4 -mthumb -mfloat-abi=hard -mfpu=fpv4-sp-d16 -Os -nostdlib -ffreestanding -ffunction-sections -fdata-sections -Wall -Wextra"
# CHATTER_DEBUG=1 bash build.sh  -> firmware_debug.bin with the diagnostic recorder (vendor cmd 0xED)
if [ -n "${CHATTER_DEBUG:-}" ]; then CFLAGS="$CFLAGS -DCHATTER_DEBUG"; export CHATTER_DEBUG; fi
# CHATTER_HOLD_MS=15 -> release hold in ms (default 32). CHATTER_NOGATE=1 -> remove MonsGeek's
# type-7 gate (patcher.py). CHATTER_OUT=name.bin -> output file name.
if [ -n "${CHATTER_HOLD_MS:-}" ]; then CFLAGS="$CFLAGS -DHOLD_MS=${CHATTER_HOLD_MS}"; fi
# CHATTER_V4=1 -> debounce slider sets the hold, Snap Tap for mechanical keys, vendor-command
# range checks, boot config clamp (handlers.c + patcher.py)
if [ -n "${CHATTER_V4:-}" ]; then CFLAGS="$CFLAGS -DCHATTER_V4"; export CHATTER_V4; fi
# CHATTER_PALETTE=1 (with CHATTER_V4) -> custom dazzle palette, vendor command 0xEE (v5)
if [ -n "${CHATTER_PALETTE:-}" ]; then CFLAGS="$CFLAGS -DCHATTER_PALETTE"; export CHATTER_PALETTE; fi
# CHATTER_MACROFIX=1 -> fix the GET_MACRO read stride bug (patcher.py)

python3 patcher.py generate
${P}gcc $CFLAGS -c hooks_gen.S -o hooks_gen.o
${P}gcc $CFLAGS -c handlers.S  -o handlers_s.o
${P}gcc $CFLAGS -c handlers.c  -o handlers_c.o
${P}ld -T patch.ld -T v408_symbols.ld hooks_gen.o handlers_s.o handlers_c.o -o hook.elf -Map hook.map
${P}objcopy -O binary -j .text -j .rodata hook.elf hook.bin
${P}objdump -d hook.elf > hook.lst
${P}nm -n hook.elf > hook.syms
${P}size hook.elf
python3 patcher.py patch
