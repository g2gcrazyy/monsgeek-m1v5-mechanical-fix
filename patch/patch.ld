/* CHATTERFIX patch layout for V408.
 * PATCH: free flash between end of firmware (0x080256F0) and config (0x08028000).
 * PATCH_SRAM: the C-library heap (0x20009310-0x2000950F). V408 never uses malloc and
 * nothing references this range; it is inside the ZI region so crt0 zeroes it at boot. */
MEMORY {
    PATCH (rx)      : ORIGIN = 0x08025800, LENGTH = 10240
    PATCH_SRAM (rw) : ORIGIN = 0x20009310, LENGTH = 512
    /* debug build only: bottom of the 16 KB stack (0x20009510-0x2000D510), zeroed by crt0.
     * The stack would have to be >12 KB deep to reach it. */
    DBG_SRAM (rw)   : ORIGIN = 0x20009600, LENGTH = 4096
}
SECTIONS {
    .text : {
        *(.text.hook_*)
        *(.text*)
    } > PATCH
    .rodata : {
        *(.rodata*)
    } > PATCH
    .bss (NOLOAD) : {
        __patch_bss_start = .;
        *(.bss*)
        *(COMMON)
        __patch_bss_end = .;
    } > PATCH_SRAM
    .dbg (NOLOAD) : {
        *(.dbg*)
    } > DBG_SRAM
    /DISCARD/ : {
        *(.ARM.*)
        *(.comment)
        *(.note*)
        *(.data*)
    }
}
