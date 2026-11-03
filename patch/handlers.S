/* Trampoline to the ORIGINAL key_event_process (0x08011D48), bypassing our hook.
 * The hook overwrote its first 4 bytes (push.w {r3-r11,lr} = 2d e9 f8 4f) with a
 * B.W, so we execute that instruction here and jump to the rest of the function.
 * lr still holds our caller's return address, so the original's final pop {...,pc}
 * returns straight to the C code that called us. */
    .syntax unified
    .cpu    cortex-m4
    .thumb

    .section .text.key_event_orig, "ax", %progbits
    .global key_event_orig
    .thumb_func
    .type   key_event_orig, %function
key_event_orig:
    push.w  {r3, r4, r5, r6, r7, r8, r9, r10, r11, lr}
    ldr     r12, =0x08011D4D
    bx      r12
    .ltorg
    .size   key_event_orig, . - key_event_orig
