"""
Keyboard chatter / double-press tester for Windows.

Hooks all keyboard input (WH_KEYBOARD_LL, read-only: keys still reach your
apps normally), timestamps every key-down / key-up with the high-resolution
performance counter, and flags any key that is released and pressed again
faster than a human can (default < 30 ms between release and re-press).

Usage:
    python chatter_test.py                 # run until Ctrl+C or Ctrl+Shift+Q
    python chatter_test.py --threshold 40  # flag re-presses faster than 40 ms
    python chatter_test.py --seconds 600   # stop automatically after 10 minutes

Just type normally (or hammer keys) in any window. Every flagged event is
printed live; a full report is printed and saved when you stop.
"""
import argparse
import collections
import csv
import ctypes
import datetime
import os
import queue
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WM_QUIT = 0x0012
LLKHF_EXTENDED, LLKHF_INJECTED = 0x01, 0x10
VK_SHIFT, VK_CONTROL, VK_Q = 0x10, 0x11, 0x51

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.SetTimer.argtypes = [wintypes.HWND, ULONG_PTR, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ULONG_PTR
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetKeyNameTextW.argtypes = [wintypes.LONG, wintypes.LPWSTR, ctypes.c_int]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

_name_cache = {}


def key_name(vk, scan, extended):
    k = (vk, scan, extended)
    if k not in _name_cache:
        buf = ctypes.create_unicode_buffer(64)
        n = user32.GetKeyNameTextW((scan << 16) | (extended << 24), buf, 64)
        _name_cache[k] = buf.value if n else f"VK_{vk:02X}"
    return _name_cache[k]


class KeyState:
    __slots__ = ("down", "last_down", "last_up", "presses", "chatter", "min_gap", "short_press")

    def __init__(self):
        self.down = False
        self.last_down = None
        self.last_up = None
        self.presses = 0
        self.chatter = 0
        self.min_gap = None
        self.short_press = 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threshold", type=float, default=30.0,
                    help="flag a re-press this many ms or less after release (default 30)")
    ap.add_argument("--short", type=float, default=5.0,
                    help="also flag presses held for less than this many ms (default 5)")
    ap.add_argument("--seconds", type=float, default=0, help="stop automatically after N seconds")
    ap.add_argument("--quiet", action="store_true", help="don't print every keystroke count update")
    ap.add_argument("--logdir", default=os.path.dirname(os.path.abspath(__file__)),
                    help="where to save the CSV log and report")
    ap.add_argument("--include-injected", action="store_true", help=argparse.SUPPRESS)  # self-test only
    args = ap.parse_args()

    events = queue.SimpleQueue()          # filled by the hook, drained by the analyser thread
    main_tid = kernel32.GetCurrentThreadId()

    @HOOKPROC
    def hook(n_code, w_param, l_param):
        if n_code == 0:
            t = time.perf_counter_ns()
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if args.include_injected or not (kb.flags & LLKHF_INJECTED):
                is_down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                events.put((t, kb.time, kb.vkCode, kb.scanCode, kb.flags & LLKHF_EXTENDED, is_down))
                if is_down and kb.vkCode == VK_Q and \
                        user32.GetAsyncKeyState(VK_CONTROL) < 0 and user32.GetAsyncKeyState(VK_SHIFT) < 0:
                    user32.PostThreadMessageW(main_tid, WM_QUIT, 0, 0)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(args.logdir, exist_ok=True)
    csv_path = os.path.join(args.logdir, f"chatter_log_{stamp}.csv")
    report_path = os.path.join(args.logdir, f"chatter_report_{stamp}.txt")

    keys = collections.defaultdict(KeyState)
    flagged = []                      # (wall time, key, up->down ms, down->down ms, held ms)
    dd_hist = collections.Counter()   # same-key down->down intervals < 100 ms, 5 ms buckets
    totals = {"down": 0, "up": 0, "autorepeat": 0}
    stop = threading.Event()
    t0 = time.perf_counter_ns()

    csv_file = open(csv_path, "w", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    writer.writerow(["t_ms", "event_time_ms", "key", "vk", "scan", "ext", "action",
                     "held_ms", "up_to_down_ms", "down_to_down_ms", "flag"])

    def analyse():
        last_status = 0.0
        while not (stop.is_set() and events.empty()):
            try:
                t, etime, vk, scan, ext, is_down = events.get(timeout=0.2)
            except queue.Empty:
                continue
            name = key_name(vk, scan, ext)
            ks = keys[(vk, ext)]
            tms = (t - t0) / 1e6
            held = up_down = down_down = None
            flag = ""
            if is_down:
                if ks.down:              # OS auto-repeat while held: not a new press
                    totals["autorepeat"] += 1
                    writer.writerow([f"{tms:.3f}", etime, name, vk, scan, ext, "repeat", "", "", "", ""])
                    continue
                ks.down = True
                ks.presses += 1
                totals["down"] += 1
                if ks.last_up is not None:
                    up_down = (t - ks.last_up) / 1e6
                if ks.last_down is not None:
                    down_down = (t - ks.last_down) / 1e6
                    if down_down < 100:
                        dd_hist[int(down_down // 5) * 5] += 1
                if up_down is not None and up_down <= args.threshold:
                    ks.chatter += 1
                    flag = "CHATTER"
                    ks.min_gap = up_down if ks.min_gap is None else min(ks.min_gap, up_down)
                    flagged.append((datetime.datetime.now().strftime("%H:%M:%S"), name,
                                    up_down, down_down, (ks.last_up - ks.last_down) / 1e6))
                    print("\r" + " " * 78 + "\r", end="")
                    print(f"  !! CHATTER  {name:<12} released {(ks.last_up - ks.last_down) / 1e6:7.2f} ms after "
                          f"press, re-pressed {up_down:6.2f} ms later  (down->down {down_down:6.2f} ms)"
                          + " " * 8)
                ks.last_down = t
            else:
                if not ks.down:
                    continue             # key-up for a key pressed before the test started
                ks.down = False
                totals["up"] += 1
                held = (t - ks.last_down) / 1e6
                if held < args.short:
                    ks.short_press += 1
                    flag = "SHORT"
                    print("\r" + " " * 78 + "\r", end="")
                    print(f"  !  SHORT    {name:<12} held only {held:6.2f} ms")
                ks.last_up = t
            writer.writerow([f"{tms:.3f}", etime, name, vk, scan, ext, "down" if is_down else "up",
                             f"{held:.3f}" if held is not None else "",
                             f"{up_down:.3f}" if up_down is not None else "",
                             f"{down_down:.3f}" if down_down is not None else "", flag])
            now = time.monotonic()
            if not args.quiet and now - last_status > 0.25:
                last_status = now
                sys.stdout.write(f"\r  presses: {totals['down']:6d}   chatter: {len(flagged):4d}   "
                                 f"last key: {name:<12}")
                sys.stdout.flush()

    worker = threading.Thread(target=analyse, daemon=True)
    worker.start()

    h_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook, kernel32.GetModuleHandleW(None), 0)
    if not h_hook:
        print("Failed to install keyboard hook, error", ctypes.get_last_error())
        return 1
    user32.SetTimer(None, 0, 200, None)   # wake the message loop so Ctrl+C / --seconds work

    print("=" * 78)
    print(" Keyboard chatter test")
    print(f" Flags: re-press <= {args.threshold:g} ms after release (CHATTER), "
          f"presses held < {args.short:g} ms (SHORT)")
    print(" Type normally in any window. Stop with Ctrl+C here, or Ctrl+Shift+Q anywhere.")
    print(f" Log: {csv_path}")
    print("=" * 78)

    started = time.monotonic()
    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        user32.UnhookWindowsHookEx(h_hook)
        stop.set()
        worker.join(timeout=5)
        csv_file.close()

    duration = time.monotonic() - started
    lines = []
    lines.append("")
    lines.append("=" * 78)
    lines.append(f" RESULT  ({duration / 60:.1f} min, {totals['down']} key presses, "
                 f"threshold {args.threshold:g} ms)")
    lines.append("=" * 78)
    chatter_total = sum(k.chatter for k in keys.values())
    short_total = sum(k.short_press for k in keys.values())
    if totals["down"] == 0:
        lines.append(" No key presses recorded.")
    elif chatter_total == 0 and short_total == 0:
        lines.append(f" CLEAN: no double presses detected in {totals['down']} presses.")
    else:
        rate = 100.0 * chatter_total / max(1, totals["down"])
        lines.append(f" DOUBLE PRESSES: {chatter_total}  ({rate:.2f}% of presses)   "
                     f"very short presses: {short_total}")
    lines.append("")
    bad = sorted(((n, k) for n, k in keys.items() if k.chatter or k.short_press),
                 key=lambda x: -x[1].chatter)
    if bad:
        lines.append(f" {'key':<14}{'presses':>8}{'chatter':>9}{'short':>7}{'fastest re-press':>18}")
        for (vk, ext), k in bad:
            nm = next((key_name(vk, s, ext) for (v, s, e) in _name_cache if v == vk and e == ext), f"VK_{vk:02X}")
            lines.append(f" {nm:<14}{k.presses:>8}{k.chatter:>9}{k.short_press:>7}"
                         f"{(f'{k.min_gap:.2f} ms' if k.min_gap is not None else '-'):>18}")
        lines.append("")
    if dd_hist:
        lines.append(" Same-key press-to-press intervals under 100 ms (5 ms buckets):")
        peak = max(dd_hist.values())
        for b in range(0, 100, 5):
            c = dd_hist.get(b, 0)
            if c:
                lines.append(f"   {b:3d}-{b + 5:<3d} ms {c:6d}  " + "#" * max(1, int(40 * c / peak)))
        lines.append("   (fast intentional double-taps are usually 60+ ms; your old fault was ~18 ms)")
        lines.append("")
    if flagged:
        lines.append(" Last flagged events:")
        for w, nm, ud, dd, held in flagged[-15:]:
            lines.append(f"   {w}  {nm:<12} held {held:6.2f} ms, gap {ud:6.2f} ms, press-to-press {dd:6.2f} ms")
        lines.append("")
    lines.append(f" CSV log: {csv_path}")
    lines.append(f" Report:  {report_path}")
    text = "\n".join(lines)
    print(text)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
