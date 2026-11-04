"""MonsGeek M1 V5 TMR companion app for the CHATTERFIX firmware.

    python companion.py        (or double-click "MonsGeek Companion.bat")

Tabs: effect colours (dazzle palette), keys (debounce, polling rate, Snap Tap),
backup & restore, firmware flasher. Uses the command-line tools in this folder.
"""
import colorsys
import datetime
import glob
import json
import math
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import debounce  # noqa: E402  (HID helpers)
import palette as palette_tool  # noqa: E402
import polling as polling_tool  # noqa: E402
import snaptap as snaptap_tool  # noqa: E402

def _find(*names):
    """first existing path among HERE/name and HERE/fwpatch/name"""
    for n in names:
        for d in (HERE, os.path.join(HERE, "fwpatch")):
            if os.path.exists(os.path.join(d, n)):
                return os.path.join(d, n)
    return os.path.join(HERE, names[0])


FLASHER = _find("flash_fw.py")
DEFAULT_FW = _find("firmware_v5.bin")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW for subprocesses
WHEEL = 220


class KeyboardError(Exception):
    pass


class Device:
    """with Device() as h: ... - opens the keyboard's config interface (one user at a time)."""
    lock = threading.Lock()

    def __enter__(self):
        Device.lock.acquire()
        try:
            self.h = debounce.open_config()
        except SystemExit as e:
            Device.lock.release()
            raise KeyboardError(str(e) or "Keyboard not found (plug it in by USB cable).")
        return self.h

    def __exit__(self, *exc):
        try:
            self.h.close()
        finally:
            Device.lock.release()


def guard(fn):
    """turn sys.exit() from the command-line tools into KeyboardError"""
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except SystemExit as e:
            raise KeyboardError(str(e))
    return wrapper


@guard
def read_palette(h):
    return palette_tool.palette_cmd(h, 0xFF)


@guard
def write_palette(h, colours):
    return palette_tool.palette_cmd(h, len(colours), colours)


def list_snaptap(h):
    """[(key_a, key_b, name_a, name_b, note)]"""
    st = debounce.command(h, 0xE5, 0xFC, 1, 0) + debounce.command(h, 0xE5, 0xFC, 1, 1)
    p = snaptap_tool.read_partners(h)
    profile = debounce.command(h, 0x84)[1] & 3
    km = b"".join(debounce.command(h, 0x8A, profile, 0xFF, pg, 0) for pg in range(8))
    rev = {v: k for k, v in snaptap_tool.NAMES.items()}

    def name(k):
        return rev.get(km[4 * k + 2], f"key {k}") if km[4 * k] == 0 else f"key {k}"
    pairs = sorted({tuple(sorted((k, p[k]))) for k in range(96) if p[k] < 96 and p[p[k]] == k})
    out = []
    for a, b in pairs:
        note = "" if st[a] == 7 and st[b] == 7 else "magnetic - turn on Snap Tap mode in the MonsGeek app"
        out.append((a, b, name(a), name(b), note))
    return out


@guard
def pair_snaptap(h, name_a, name_b):
    ka, kb = snaptap_tool.key_index(h, name_a), snaptap_tool.key_index(h, name_b)
    if ka == kb:
        raise KeyboardError("Pick two different keys.")
    p = snaptap_tool.read_partners(h)
    for k in (ka, kb):                                     # unpair old partners first
        if p[k] < 96 and p[k] not in (ka, kb):
            snaptap_tool.send_partner(h, p[k], snaptap_tool.UNBOUND, False)
    snaptap_tool.send_partner(h, ka, kb, False)
    snaptap_tool.send_partner(h, kb, ka, True)


def clear_snaptap(h, keys):
    keys = list(keys)
    for i, k in enumerate(keys):
        snaptap_tool.send_partner(h, k, snaptap_tool.UNBOUND, i == len(keys) - 1)


def hex_to_hsv(c):
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(r, g, b)


def hsv_to_hex(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return f"{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MonsGeek Companion - CHATTERFIX")
        self.minsize(720, 640)
        self.q = queue.Queue()
        self.busy = False
        self.colours = [hex_to_hsv("ff6600"), hex_to_hsv("00ccff")]
        self.slot = tk.IntVar(value=0)

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        self.status = ttk.Label(top, text="Looking for the keyboard ...", font=("Segoe UI", 10, "bold"))
        self.status.pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="right")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10)
        self.build_colours(nb)
        self.build_keys(nb)
        self.build_backup(nb)
        self.build_firmware(nb)

        logf = ttk.LabelFrame(self, text="Log", padding=4)
        logf.pack(fill="both", padx=10, pady=(6, 10))
        self.log_box = tk.Text(logf, height=9, wrap="word", font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=sb.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.refresh_backups()
        self.after(100, self.pump)
        self.after(200, self.refresh)

    # ---------------------------------------------------------------- helpers
    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text.rstrip("\n") + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.after(100, self.pump)

    def ui(self, fn):
        """run fn on the Tk thread"""
        self.q.put(("call", fn))

    def background(self, title, work, done=None):
        """run work() in a thread; one task at a time"""
        if self.busy:
            messagebox.showinfo("Busy", "Please wait for the current task to finish.")
            return
        self.busy = True
        self.log(f"--- {title}")

        def run():
            ok = True
            try:
                work()
            except KeyboardError as e:
                ok = False
                self.q.put(("log", f"ERROR: {e}"))
            except Exception as e:                       # keep the app alive
                ok = False
                self.q.put(("log", f"ERROR: {type(e).__name__}: {e}"))
            finally:
                self.busy = False
                if done:
                    self.ui(lambda: done(ok))
        threading.Thread(target=run, daemon=True).start()

    def run_tool(self, *args):
        """run one of the command-line tools, streaming its output to the log; returns (code, text)"""
        cmd = [sys.executable, "-u", *args]
        p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, creationflags=NO_WINDOW)
        out = []
        for line in p.stdout:
            out.append(line)
            self.q.put(("log", line))
        return p.wait(), "".join(out)

    # ---------------------------------------------------------------- status
    def refresh(self):
        def work():
            with Device() as h:
                r = debounce.command(h, 0x8F)
                dev_id = int.from_bytes(r[1:5], "little")
                ver = r[7] | r[8] << 8
                try:
                    pal = read_palette(h)
                    fw = "CHATTERFIX v5"
                except KeyboardError:
                    pal, fw = None, "stock / older CHATTERFIX (no palette support)"
                deb = debounce.get_debounce(h)
                rate = polling_tool.get_rate(h)
                pairs = list_snaptap(h)
            name = "M1 V5 TMR" if dev_id == 2949 else f"device id {dev_id}"
            self.q.put(("log", f"{name}, firmware v{ver:x} ({fw}); debounce {deb} ms; polling {rate} Hz"))

            def show():
                self.status.configure(text=f"Connected: MonsGeek {name}  -  firmware v{ver:x}  -  {fw}")
                self.debounce_var.set(deb)
                self.rate_var.set(str(rate))
                self.show_pairs(pairs)
                if pal:
                    self.colours = [hex_to_hsv(c) for c in (pal + pal)[:2]]
                    self.pal_state.configure(text="Palette on the keyboard: " + "  ".join("#" + c for c in pal))
                    self.redraw_wheel()
                elif pal is not None:
                    self.pal_state.configure(text="Palette on the keyboard: off (MonsGeek rainbow)")
                else:
                    self.pal_state.configure(text="This firmware has no palette support (flash CHATTERFIX v5).")
                self.update_swatches()
                self.refresh_backups()
            self.ui(show)

        def done(ok):
            if not ok:
                self.status.configure(text="Keyboard not found - plug it in by USB cable and press Refresh")
        self.background("reading the keyboard", work, done)

    # ---------------------------------------------------------------- colours tab
    def build_colours(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Effect colours")
        ttk.Label(f, wraplength=640, text=(
            "The two colours used when an effect's colour is set to Dazzle in the MonsGeek app "
            "(breathing, meteor, ripple, train, snow, peaks, sine wave and the rest). "
            "Pick which colour to edit, then click or drag on the wheel.")).pack(anchor="w")
        body = ttk.Frame(f)
        body.pack(fill="x", pady=8)

        left = ttk.Frame(body)
        left.pack(side="left")
        self.wheel = tk.Canvas(left, width=WHEEL, height=WHEEL, highlightthickness=0)
        self.wheel.pack()
        self.wheel_img = tk.PhotoImage(width=WHEEL, height=WHEEL)
        self.wheel.create_image(0, 0, image=self.wheel_img, anchor="nw")
        self.marker = self.wheel.create_oval(0, 0, 0, 0, outline="black", width=2)
        self.wheel.bind("<Button-1>", self.wheel_pick)
        self.wheel.bind("<B1-Motion>", self.wheel_pick)
        ttk.Label(left, text="Brightness").pack(anchor="w", pady=(6, 0))
        self.value = tk.DoubleVar(value=100)
        ttk.Scale(left, from_=10, to=100, variable=self.value, command=lambda _v: self.value_changed()).pack(fill="x")

        right = ttk.Frame(body, padding=(20, 0))
        right.pack(side="left", fill="y")
        self.swatch = []
        self.hex_var = [tk.StringVar(), tk.StringVar()]
        for i, label in enumerate(("Colour A", "Colour B")):
            row = ttk.Frame(right)
            row.pack(anchor="w", pady=6)
            ttk.Radiobutton(row, text=label, variable=self.slot, value=i,
                            command=self.slot_changed).pack(side="left")
            sw = tk.Label(row, width=6, height=2, relief="ridge")
            sw.pack(side="left", padx=8)
            sw.bind("<Button-1>", lambda _e, i=i: (self.slot.set(i), self.slot_changed()))
            self.swatch.append(sw)
            ttk.Label(row, text="#").pack(side="left")
            e = ttk.Entry(row, textvariable=self.hex_var[i], width=8)
            e.pack(side="left")
            e.bind("<Return>", lambda _e, i=i: self.hex_entered(i))
        ttk.Label(right, text="(type a hex colour and press Enter)", foreground="gray").pack(anchor="w")
        ttk.Button(right, text="Swap A and B", command=self.swap).pack(anchor="w", pady=(12, 2))
        ttk.Button(right, text="Apply to keyboard", command=self.apply_palette).pack(anchor="w", pady=2)
        ttk.Button(right, text="Turn palette off (rainbow)", command=self.palette_off).pack(anchor="w", pady=2)
        self.pal_state = ttk.Label(f, text="")
        self.pal_state.pack(anchor="w", pady=(4, 0))
        self.redraw_wheel()
        self.update_swatches()

    def redraw_wheel(self):
        v = self.value.get() / 100
        r0 = WHEEL / 2
        bg = self.cget("bg")
        try:
            bg = "#%02x%02x%02x" % tuple(c // 256 for c in self.winfo_rgb(bg))
        except tk.TclError:
            bg = "#f0f0f0"
        rows = []
        for y in range(WHEEL):
            row = []
            dy = y - r0 + 0.5
            for x in range(WHEEL):
                dx = x - r0 + 0.5
                d = math.hypot(dx, dy) / (r0 - 1)
                if d > 1:
                    row.append(bg)
                else:
                    hue = (math.atan2(-dy, dx) / (2 * math.pi)) % 1.0
                    row.append("#" + hsv_to_hex(hue, d, v))
            rows.append("{" + " ".join(row) + "}")
        self.wheel_img.put(" ".join(rows))
        self.place_marker()

    def place_marker(self):
        h, s, _v = self.colours[self.slot.get()]
        r0 = WHEEL / 2
        x = r0 + math.cos(h * 2 * math.pi) * s * (r0 - 1)
        y = r0 - math.sin(h * 2 * math.pi) * s * (r0 - 1)
        self.wheel.coords(self.marker, x - 6, y - 6, x + 6, y + 6)

    def wheel_pick(self, ev):
        r0 = WHEEL / 2
        dx, dy = ev.x - r0, ev.y - r0
        s = min(1.0, math.hypot(dx, dy) / (r0 - 1))
        h = (math.atan2(-dy, dx) / (2 * math.pi)) % 1.0
        self.colours[self.slot.get()] = (h, s, self.value.get() / 100)
        self.place_marker()
        self.update_swatches()

    def value_changed(self):
        h, s, _ = self.colours[self.slot.get()]
        self.colours[self.slot.get()] = (h, s, self.value.get() / 100)
        self.update_swatches()
        if getattr(self, "_redraw_pending", None):
            self.after_cancel(self._redraw_pending)
        self._redraw_pending = self.after(150, self.redraw_wheel)

    def slot_changed(self):
        self.value.set(round(self.colours[self.slot.get()][2] * 100))
        self.redraw_wheel()

    def update_swatches(self):
        for i in range(2):
            c = hsv_to_hex(*self.colours[i])
            self.swatch[i].configure(bg="#" + c)
            self.hex_var[i].set(c)

    def hex_entered(self, i):
        c = self.hex_var[i].get().strip().lstrip("#").lower()
        if len(c) != 6 or any(ch not in "0123456789abcdef" for ch in c):
            messagebox.showerror("Colour", "Enter a colour as 6 hex digits, e.g. ff6600")
            return
        self.colours[i] = hex_to_hsv(c)
        self.slot.set(i)
        self.slot_changed()
        self.update_swatches()

    def swap(self):
        self.colours.reverse()
        self.slot_changed()
        self.update_swatches()

    def apply_palette(self):
        cols = [hsv_to_hex(*c) for c in self.colours]

        def work():
            with Device() as h:
                cur = write_palette(h, cols)
            self.q.put(("log", "palette set: " + "  ".join("#" + c for c in cur)))
            self.ui(lambda: self.pal_state.configure(text="Palette on the keyboard: " + "  ".join("#" + c for c in cur)))
        self.background("setting the dazzle palette", work)

    def palette_off(self):
        def work():
            with Device() as h:
                guard(palette_tool.palette_cmd)(h, 0)
            self.q.put(("log", "palette off - dazzle uses MonsGeek's rainbow again"))
            self.ui(lambda: self.pal_state.configure(text="Palette on the keyboard: off (MonsGeek rainbow)"))
        self.background("turning the palette off", work)

    # ---------------------------------------------------------------- keys tab
    def build_keys(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Keys")

        d = ttk.LabelFrame(f, text="Debounce (mechanical keys)", padding=8)
        d.pack(fill="x")
        ttk.Label(d, wraplength=640, text=(
            "How long a key release is held back to hide switch bounce. Presses are never delayed. "
            "0 = default (5 ms). Higher values hide more bounce but releases reach the PC later.")).pack(anchor="w")
        row = ttk.Frame(d)
        row.pack(fill="x", pady=4)
        self.debounce_var = tk.IntVar(value=0)
        ttk.Scale(row, from_=0, to=50, variable=self.debounce_var,
                  command=lambda v: self.debounce_var.set(round(float(v)))).pack(side="left", fill="x", expand=True)
        ttk.Label(row, textvariable=self.debounce_var, width=3).pack(side="left", padx=4)
        ttk.Label(row, text="ms").pack(side="left")
        ttk.Button(row, text="Apply", command=self.apply_debounce).pack(side="left", padx=8)

        p = ttk.LabelFrame(f, text="Polling rate", padding=8)
        p.pack(fill="x", pady=8)
        row = ttk.Frame(p)
        row.pack(fill="x")
        self.rate_var = tk.StringVar(value="1000")
        ttk.Combobox(row, textvariable=self.rate_var, width=8, state="readonly",
                     values=[str(r) for r in polling_tool.RATES]).pack(side="left")
        ttk.Label(row, text="Hz   (flashing firmware resets this to 8000)").pack(side="left", padx=6)
        ttk.Button(row, text="Apply", command=self.apply_rate).pack(side="left", padx=8)

        s = ttk.LabelFrame(f, text="Snap Tap", padding=8)
        s.pack(fill="both", expand=True)
        ttk.Label(s, wraplength=640, text=(
            "Pressing one key of a pair releases the other (last key pressed wins). Not allowed in "
            "some games, e.g. CS2 on Valve servers.")).pack(anchor="w")
        row = ttk.Frame(s)
        row.pack(fill="x", pady=4)
        names = list(snaptap_tool.NAMES)
        self.st_a, self.st_b = tk.StringVar(value="A"), tk.StringVar(value="D")
        ttk.Combobox(row, textvariable=self.st_a, values=names, width=8, state="readonly").pack(side="left")
        ttk.Label(row, text="<->").pack(side="left", padx=4)
        ttk.Combobox(row, textvariable=self.st_b, values=names, width=8, state="readonly").pack(side="left")
        ttk.Button(row, text="Pair", command=self.pair).pack(side="left", padx=8)
        self.pairs_box = tk.Listbox(s, height=5)
        self.pairs_box.pack(fill="both", expand=True, pady=4)
        row = ttk.Frame(s)
        row.pack(fill="x")
        ttk.Button(row, text="Remove selected pair", command=self.unpair).pack(side="left")
        ttk.Button(row, text="Remove all", command=self.unpair_all).pack(side="left", padx=8)
        self.pairs = []

    def show_pairs(self, pairs):
        self.pairs = pairs
        self.pairs_box.delete(0, "end")
        for _a, _b, na, nb_, note in pairs:
            self.pairs_box.insert("end", f"{na} <-> {nb_}" + (f"     ({note})" if note else ""))
        if not pairs:
            self.pairs_box.insert("end", "(no pairs)")

    def apply_debounce(self):
        ms = int(self.debounce_var.get())

        def work():
            with Device() as h:
                debounce.command(h, 0x06, ms)
                after = debounce.get_debounce(h)
            self.q.put(("log", f"debounce set to {after} ms" + (" (= default 5 ms)" if after == 0 else "")))
        self.background("setting debounce", work)

    def apply_rate(self):
        hz = int(self.rate_var.get())

        def work():
            with Device() as h:
                debounce.command(h, 0x03, 0, polling_tool.RATES.index(hz))
                now = polling_tool.get_rate(h)
            self.q.put(("log", f"polling rate {now} Hz"))
        self.background("setting the polling rate", work)

    def pair(self):
        a, b = self.st_a.get(), self.st_b.get()

        def work():
            with Device() as h:
                pair_snaptap(h, a, b)
                pairs = list_snaptap(h)
            self.q.put(("log", f"Snap Tap: paired {a} and {b}"))
            self.ui(lambda: self.show_pairs(pairs))
        self.background("pairing Snap Tap keys", work)

    def unpair(self):
        sel = self.pairs_box.curselection()
        if not sel or sel[0] >= len(self.pairs):
            return
        a, b, na, nb_, _ = self.pairs[sel[0]]

        def work():
            with Device() as h:
                clear_snaptap(h, (a, b))
                pairs = list_snaptap(h)
            self.q.put(("log", f"Snap Tap: removed {na} <-> {nb_}"))
            self.ui(lambda: self.show_pairs(pairs))
        self.background("removing a Snap Tap pair", work)

    def unpair_all(self):
        def work():
            with Device() as h:
                p = snaptap_tool.read_partners(h)
                clear_snaptap(h, [k for k in range(96) if p[k] != snaptap_tool.UNBOUND])
                pairs = list_snaptap(h)
            self.q.put(("log", "Snap Tap: all pairs removed"))
            self.ui(lambda: self.show_pairs(pairs))
        self.background("removing all Snap Tap pairs", work)

    # ---------------------------------------------------------------- backup tab
    def build_backup(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Backup & restore")
        ttk.Label(f, wraplength=640, text=(
            "A backup saves lighting, key bindings, macros, Fn layers, Snap Tap, the dazzle palette and "
            "the magnetic switch settings. Restore writes back lighting, key bindings, macros, Snap Tap "
            "and the palette (only what differs). Switch types are never changed.")).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="Back up now", command=self.backup_now).pack(side="left")
        ttk.Button(row, text="Open folder", command=lambda: os.startfile(HERE)).pack(side="left", padx=8)
        self.backups_box = tk.Listbox(f, height=10)
        self.backups_box.pack(fill="both", expand=True)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="Show what would change", command=lambda: self.restore(False)).pack(side="left")
        ttk.Button(row, text="Restore selected backup", command=lambda: self.restore(True)).pack(side="left", padx=8)
        self.backup_files = []

    def refresh_backups(self):
        self.backup_files = sorted(glob.glob(os.path.join(HERE, "full_backup_*.json")), reverse=True)
        self.backups_box.delete(0, "end")
        for p in self.backup_files:
            stamp = os.path.basename(p)[12:27]
            try:
                when = datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d  %H:%M:%S")
            except ValueError:
                when = stamp
            extra = ""
            try:
                bk = json.load(open(p))
                led = bytes.fromhex(bk.get("led_params", "00"))
                extra = f"   lighting mode {led[1]}" if len(led) > 1 else ""
                if bk.get("dazzle_palette"):
                    extra += "   palette " + " ".join("#" + c for c in bk["dazzle_palette"])
            except Exception:
                pass
            self.backups_box.insert("end", f"{when}{extra}")
        if not self.backup_files:
            self.backups_box.insert("end", "(no backups yet)")

    def backup_now(self):
        def work():
            code, _ = self.run_tool("backup_full.py")
            if code:
                raise KeyboardError("backup failed")
        self.background("backing up the keyboard settings", work, lambda ok: self.refresh_backups())

    def selected_backup(self):
        sel = self.backups_box.curselection()
        if not sel or sel[0] >= len(self.backup_files):
            messagebox.showinfo("Restore", "Select a backup in the list first.")
            return None
        return self.backup_files[sel[0]]

    def restore(self, apply):
        path = self.selected_backup()
        if not path:
            return
        if apply and not messagebox.askyesno("Restore", f"Restore settings from\n{os.path.basename(path)}?"):
            return

        def work():
            self.run_tool("restore_settings.py", path, *(["--apply"] if apply else []))
        self.background(("restoring " if apply else "checking ") + os.path.basename(path), work,
                        (lambda ok: self.refresh()) if apply else None)

    # ---------------------------------------------------------------- firmware tab
    def build_firmware(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Firmware")
        ttk.Label(f, wraplength=640, text=(
            "Flash a firmware image over USB (M1 V5 TMR only - the flasher checks the model). Flashing "
            "resets the keyboard's lighting, macros, Snap Tap and polling rate; with the box below ticked "
            "the app backs up first and restores everything afterwards. Do not unplug the keyboard while "
            "flashing. To go back to stock, pick firmware_2949_v408.bin (from the monsgeek-akko-linux project).")).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=8)
        self.fw_path = tk.StringVar(value=DEFAULT_FW if os.path.exists(DEFAULT_FW) else "")
        ttk.Entry(row, textvariable=self.fw_path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=self.browse_fw).pack(side="left", padx=6)
        self.keep_settings = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Back up settings before flashing and restore them afterwards",
                        variable=self.keep_settings).pack(anchor="w")
        self.set_1000 = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Set the polling rate back to 1000 Hz after flashing",
                        variable=self.set_1000).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="Check image and keyboard", command=self.check_fw).pack(side="left")
        ttk.Button(row, text="Flash...", command=self.flash).pack(side="left", padx=8)

    def browse_fw(self):
        p = filedialog.askopenfilename(title="Firmware image", initialdir=os.path.dirname(DEFAULT_FW),
                                       filetypes=[("Firmware", "*.bin"), ("All files", "*.*")])
        if p:
            self.fw_path.set(p)

    def check_fw(self):
        path = self.fw_path.get()
        if not os.path.isfile(path):
            messagebox.showerror("Firmware", "Choose a firmware file first.")
            return
        self.background("checking " + os.path.basename(path),
                        lambda: self.run_tool(FLASHER, path))

    def flash(self):
        path = self.fw_path.get()
        if not os.path.isfile(path):
            messagebox.showerror("Firmware", "Choose a firmware file first.")
            return
        if not messagebox.askyesno("Flash firmware", (
                f"Flash {os.path.basename(path)} to the keyboard?\n\n"
                "It takes about 30 seconds. Do not unplug the keyboard while flashing.")):
            return
        keep, rate = self.keep_settings.get(), self.set_1000.get()

        def work():
            backup = None
            if keep:
                before = set(glob.glob(os.path.join(HERE, "full_backup_*.json")))
                code, _ = self.run_tool("backup_full.py")
                new = sorted(set(glob.glob(os.path.join(HERE, "full_backup_*.json"))) - before)
                if code or not new:
                    raise KeyboardError("backup before flashing failed - nothing was flashed")
                backup = new[-1]
            _code, out = self.run_tool(FLASHER, path, "--yes")
            if "OK: keyboard is back in normal mode" not in out:
                raise KeyboardError("flashing did not finish - see the log. You can run it again; it also "
                                    "works when the keyboard is stuck in update mode.")
            if rate:
                self.run_tool("polling.py", "1000")
            if backup:
                self.run_tool("restore_settings.py", backup, "--apply")
                self.run_tool("restore_settings.py", backup)
            self.q.put(("log", "flash finished"))
        self.background("flashing " + os.path.basename(path), work, lambda ok: self.refresh())


if __name__ == "__main__":
    App().mainloop()
