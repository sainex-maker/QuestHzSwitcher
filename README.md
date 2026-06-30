# QuestHzSwitcher

A cross-platform desktop app (Windows / macOS / Linux) for switching your
Meta Quest's rendering Hz on the fly — either with a button in the app
window, or by holding the Oculus/Meta button (or another mapped button)
on the headset itself.

## Files

- `hz_core.py` — connection logic over ADB and Hz switching (no GUI).
- `hz_gui.py` — the app window (tkinter). Run this file to launch the app.
- `button_monitor.py` — standalone diagnostic tool that shows a live log
  of every button press/release event from the headset and controllers,
  useful for figuring out raw event codes for buttons you want to use.

## Requirements

1. **Python 3.8+** (usually already installed on macOS/Linux; on Windows,
   download from python.org and check "Add to PATH" during install).
2. **tkinter** — normally bundled with the standard Python install.
   If missing on Linux:
   ```
   sudo apt install python3-tk      # Debian/Ubuntu
   sudo dnf install python3-tkinter # Fedora
   ```
3. **ADB (Android Platform Tools)** — download here:
   https://developer.android.com/tools/releases/platform-tools
   Unzip it, and add the folder (containing `adb`/`adb.exe`) to your PATH.

## Running

```
cd sainex
python3 hz_gui.py
```

(on Windows you may need `python` instead of `python3`)

## How to use

1. Connect your Quest via USB (allow debugging on the headset if prompted).
2. Click **Start / Connect**. The app will switch the connection over to
   Wi-Fi by itself — you can unplug the cable after that.
3. **Direct Hz setting**: click 60 / 72 / 90 / 120 Hz in the "Display
   refresh rate" panel — this changes the headset's actual base display
   refresh rate (`debug.oculus.refreshRate`). Not every app supports
   120 Hz. If the change doesn't apply immediately, turn the headset
   display off and on with the power button.
4. **Swap interval (frame divider)**: a separate, independent feature —
   pick a value, then either:
   - click **Toggle Hz** in the window,
   - or hold the Oculus/Meta button (or your chosen trigger button) on
     the headset for 0.25–1.5 seconds (release right as the recenter
     circle appears — that's the trigger).
5. **Force Normal** / **Force Target** let you force a specific swap
   state directly, without waiting for a toggle.

### What's the difference between these two mechanisms?

- **Refresh rate (60/72/90/120 buttons)** — sets the headset's actual
  screen refresh rate.
- **Swap interval (toggle/preset)** — doesn't touch the screen, it tells
  the renderer to skip frames (e.g. interval=2 on a 60 Hz base effectively
  renders at 30 fps, while the screen itself stays at 60 Hz). Useful as a
  quick in-game "turbo mode" without changing the actual display.

You can combine both: set the base to 90 Hz with a button, then use
toggle for extra load reduction mid-game.

### Tip

To make triggering the Hz toggle easier during gameplay, you can remap
another controller button (e.g. A) to the Oculus button in the headset's
settings — that's more convenient than reaching for the actual system
menu button.

### Binding the trigger to a different button (not Oculus/Meta)

By default the trigger is the Oculus/Meta button. The Trigger button
dropdown also includes these confirmed-working options, picked from a
live diagnostic session:

- Meta button
- Right Trigger
- Right Grip
- Left Trigger
- Left Grip
- Power Button
- Volume Up
- Volume Down

**Note:** not every physical Quest controller button (e.g. A/B/X/Y on
Touch controllers) is necessarily visible through `getevent` the same
way the system Oculus button is — this depends on how the OS routes
controller Bluetooth events. On Quest 2, A/B/X/Y buttons do not appear
through this method at all; they're handled by the VR runtime
(OpenXR/VrApi) directly, bypassing the standard Linux input subsystem.
If you want to investigate this yourself, use `button_monitor.py` to
watch raw events live while pressing buttons.

You can also adjust the **hold duration window** (min/max seconds) in
the same panel, with a "Reset to default" button to go back to 0.25s–1.5s.

## Building a portable .exe (Windows)

This bundles `adb.exe` and the required DLLs directly inside a single
file — no need to keep a `platform-tools` folder around.

1. Install PyInstaller (one-time):
   ```
   pip install pyinstaller
   ```

2. In a command prompt, navigate to the folder containing `hz_core.py`,
   `hz_gui.py`, and the files from `platform-tools`
   (`adb.exe`, `AdbWinApi.dll`, `AdbWinUsbApi.dll`):
   ```
   cd C:\path\to\your\folder
   ```

3. Run the build:
   ```
   pyinstaller --onefile --noconsole --name "SaineX" ^
     --add-binary "adb.exe;." ^
     --add-binary "AdbWinApi.dll;." ^
     --add-binary "AdbWinUsbApi.dll;." ^
     hz_gui.py
   ```
   (`^` is a line continuation in cmd; in PowerShell use `` ` `` instead,
   or just type it all on one line.)

   If `pyinstaller` isn't recognized as a command, use:
   ```
   python -m PyInstaller --onefile --noconsole --name "SaineX" --add-binary "adb.exe;." --add-binary "AdbWinApi.dll;." --add-binary "AdbWinUsbApi.dll;." hz_gui.py
   ```

4. The finished file will be here:
   ```
   dist\SaineX.exe
   ```
   This is a fully portable exe — you can move it anywhere on its own,
   no `platform-tools` folder required. On launch, it extracts `adb.exe`
   to a temporary folder and uses it from there.

### Troubleshooting

- **"adb not found on PATH"** when running the exe — make sure the build
  command was run in the same folder as `adb.exe` and both `.dll` files,
  and double-check the `--add-binary` paths for typos.
- Antivirus / Windows Defender sometimes flags PyInstaller-built exe
  files as suspicious (a known false positive) — this is a common
  PyInstaller quirk, not something specific to this code.
- Want a console for debugging errors? Drop `--noconsole` from the
  command and rebuild.

## Notes

- Don't set Hz too low — it'll just slow down your game/video.
- If the connection drops and doesn't recover on its own, click Stop,
  then Start again.
