# Installing the fixed firmware (MonsGeek M1 V5 TMR with mechanical switches)

These steps are for owners of the **MonsGeek M1 V5 TMR** (device ID 2949) who get double key presses after fitting mechanical (contact) switches.

This is an **unofficial** firmware: MonsGeek's V408 plus the fixes described in `docs/`. Installing it is at your own risk. The steps below show how to go back to stock (section 5).

**Tested setup:** one keyboard, on Windows 11, over a USB cable.

## 1. What you need
- **Windows PC** and the keyboard's **USB cable**. Flashing does not work over the 2.4 GHz dongle or Bluetooth.
- **Python 3** from python.org. Tick "Add Python to PATH" when installing.
- **The hidapi package:** open a Command Prompt and run
  ```
  pip install hidapi
  ```
- **The files:**
  - `firmware_v4.bin` from this repository's **Releases** page. Check that its SHA-256 matches the one shown on the release.
  - The `tools` folder. Use **Code → Download ZIP** on the repository page and unzip it.
  - Put `firmware_v4.bin` in the `tools` folder.
- **Optional, for recovery:** MonsGeek's official V408 update package (`ry_upgrade.exe`), from MonsGeek support. Keep it handy just in case.

## 2. Before flashing
1. **Close MonsGeek's driver/app and its web page,** so nothing else talks to the keyboard.
2. **Your settings will be reset.** Lighting, macros, macro key bindings and polling rate go back to defaults; switch types are kept. Write down or screenshot your macros.
   - `python backup_full.py` saves a copy of the keyboard's settings.
   - On stock firmware it cannot read macros in odd-numbered slots, because of a bug that this firmware fixes.
3. **Check the image and the keyboard (dry run):** open a Command Prompt in the `tools` folder and run
   ```
   python flash_fw.py firmware_v4.bin
   ```
   It must show `device id 2949`. On any other model the tool refuses to flash. Do not force it.

## 3. Flash
```
python flash_fw.py firmware_v4.bin --yes
```
- It takes about 30 seconds. **Do not unplug the keyboard while it runs.**
- It may say `bootloader REJECTED the image` once. That means a piece of data got lost on the way. It is harmless: the keyboard waits in update mode, and the tool sends the image again automatically, up to 3 times.
- It has finished when it shows `OK: keyboard is back in normal mode, firmware v408`. The keyboard still reports v408, because the fixes are added on top of V408.

## 4. After flashing
1. **Set the polling rate** in MonsGeek's app, or with `python polling.py 1000`. Flashing sets it to 8000 Hz.
2. **Check your switch types:** run `python backup_full.py` and look at the `switch types:` line.
   - Mechanical keys must be **type 7**, which is MonsGeek's mechanical-switch type from the V408 build. For example, a fully mechanical board shows `{0: 43, 24: 3, 7: 82}`.
   - The double-press fix only applies to type-7 keys. Keys set to a TMR type keep stock behaviour.
3. **Re-create your lighting and macros** in MonsGeek's app. If you made a backup on this firmware, `python restore_settings.py <backup file>` shows what it would restore, and adding `--apply` writes it.
4. **Optional:**
   - **Debounce:** MonsGeek's debounce slider (or `python debounce.py <ms>`) now sets how long a key release is held on mechanical keys. 0 means the default of 5 ms.
   - **Snap Tap:** `python snaptap.py A D` pairs A and D. Use `python snaptap.py clear all` to turn it off. Some games, for example CS2 on Valve's servers, do not allow Snap Tap.
5. **Do not accept firmware updates** in MonsGeek's app. An update would replace this firmware, and later official versions (V410) have the double-press problem again.

## 5. If something goes wrong
- **Keyboard stuck in update mode, or no typing after a failed flash:** run the same `flash_fw.py ... --yes` command again. It also works when the keyboard is already in update mode. Or run MonsGeek's `ry_upgrade.exe` to put official V408 back.
- **Going back to stock:** run `ry_upgrade.exe` from MonsGeek's V408 package.
- **Last resort, keyboard completely dead (not seen so far):** the MCU has a built-in USB DFU mode. Bridge the BOOT0 pin to 3.3 V while plugging in, then write stock firmware at 0x08005000 with `dfu-util`. See the monsgeek-akko-linux project, `docs/HARDWARE.md`. This needs opening the keyboard.
