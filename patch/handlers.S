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

#ifdef CHATTER_PALETTE
/* Hook inside ws2812_set_pixel(led r0, brightness r1, R r2, G r3, B [sp]) at 0x080070EA,
 * right after its `cmp r0,#82; it cs; bxcs lr` bounds check (that IT block cannot be
 * moved, so we hook the next instruction). chatter_pixel_remap may rewrite the saved
 * r2/r3 and the caller's stack argument B (at sp+24 after our push). */
    .section .text.hook_pixel, "ax", %progbits
    .global _hook_pixel_stub
    .thumb_func
    .type   _hook_pixel_stub, %function
_hook_pixel_stub:
    push    {r0-r3, r12, lr}
    mov     r0, sp
    bl      chatter_pixel_remap
    pop     {r0-r3, r12, lr}
    stmdb   sp!, {r4, r5, r6, r7, r8, r9}       /* displaced: e92d 03f0 */
    ldr     r12, =0x080070EF
    bx      r12
    .ltorg
    .size   _hook_pixel_stub, . - _hook_pixel_stub
#endif

#ifdef CHATTER_PALETTE
/* snow (0x0800AD10, `bl rand` on the dazzle path): column is in r8, row in r7 */
    .section .text.snow_colour_stub, "ax", %progbits
    .global _snow_colour_stub
    .thumb_func
    .type   _snow_colour_stub, %function
_snow_colour_stub:
    mov     r0, r8
    mov     r1, r7
    b.w     chatter_snow_colour
    .size   _snow_colour_stub, . - _snow_colour_stub

/* sine (0x0800B7A0, `bl led_ripple_effect`): the segment slot is in r6 */
    .section .text.sine_draw_stub, "ax", %progbits
    .global _sine_draw_stub
    .thumb_func
    .type   _sine_draw_stub, %function
_sine_draw_stub:
    mov     r3, r6
    b.w     chatter_sine_draw
    .size   _sine_draw_stub, . - _sine_draw_stub
#endif
