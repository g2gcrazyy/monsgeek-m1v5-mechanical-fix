/* V408 firmware addresses used by the patch (Thumb functions: bit 0 set).
 * Verified against MonsGeek M1 V5 TMR firmware V408 (dev_id 2949). */
encoder_quadrature_tick = 0x08019f9d;   /* called from fast_tick_8khz_handler at 0x08012EAE */
flash_read_to_buffer    = 0x080158c1;   /* (src, dst, len); used by config_load_all, GET_MACRO */
