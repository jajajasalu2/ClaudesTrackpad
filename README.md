# Claude's Trackpad

Use your phone as a wireless trackpad and keyboard for a Windows PC.

## Install

1. Download or clone this repository.
2. Double-click `build.bat`. The first run takes a minute or two.
   - Requires [Python 3.9+](https://www.python.org/downloads/) with tkinter.
   - Everything else is installed into a private virtual environment, so your own Python setup isn't touched.
3. When the app opens, Windows Firewall asks about network access. Tick **Private networks** and click **Allow**. Without this, your phone can't connect.

The app is installed to `%LOCALAPPDATA%\Programs\ClaudesTrackpad` and added to the Start menu.

## Use

- Press the Windows key, type **Claude's Trackpad**, and press Enter.
- Scan the QR code with your phone's camera. Your phone and PC need to be on the same network.
- On iPhone, **Share > Add to Home Screen** in Safari gives you an app icon. The link stays the same between restarts, so the icon keeps working.
- Closing the window keeps the app running in the system tray (the **^** arrow on the taskbar). Click the tray icon to see the QR code again; right-click > **Quit** to stop it.

### Buttons

| Button | What it does |
| --- | --- |
| **Copy link** | Copies the link, if you'd rather send it to your phone than scan it |
| **Other address** | Cycles through network adapters, if the PC has several and the link doesn't load |
| **New link** | Makes a new secret link; old links and Home Screen icons stop working |

## Updating

Pull the latest changes (or replace the `.py` files) and run `build.bat` again.

## Uninstall

Double-click `uninstall.bat`. You can delete this folder afterwards.

## Files

| File | Purpose |
| --- | --- |
| `claudes_trackpad.py` | The app (window + tray icon) |
| `phone_trackpad.py` | The trackpad server; also works on its own from a terminal |
| `build.bat` | Builds the app and adds the Start menu entry |
| `make_shortcut.ps1` | Creates the Start menu shortcut (used by `build.bat`) |
| `uninstall.bat` | Removes the app, shortcuts, and settings |
