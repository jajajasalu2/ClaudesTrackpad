"""
Phone Trackpad — use your phone's browser as a trackpad for Windows or Linux.

Setup (once):
    pip install aiohttp
    pip install qrcode        # optional, adds a scannable QR code

    Linux only: the script creates a virtual mouse and keyboard through /dev/uinput,
    which works on Wayland (GNOME, KDE...) and X11. If you're not allowed to use it,
    the script prints the commands that grant access.

Run:
    python phone_trackpad.py      (python3 on Linux)
    python phone_trackpad.py --ip 192.168.1.5    # force the address shown in the URL

Then scan the QR code (or open the printed URL) on your phone.
Phone and laptop must be on the same Wi-Fi.

Gestures:
    1 finger drag        -> move cursor
    1 finger tap         -> left click
    tap, then tap again  -> double-click
    tap, then touch+drag -> drag (select text, move windows); lift to release
    2 finger tap         -> right click
    2 finger drag        -> scroll (vertical + horizontal)
    2 finger pinch       -> zoom in/out (Ctrl + scroll)
    "Drag" button        -> holds left button down (select text / drag windows)
    "Keys" button        -> opens the iPhone keyboard; what you type goes to the laptop
"""

import asyncio
import ctypes
import json
import os
import secrets
import socket
import struct
import sys
import time

if sys.platform.startswith("linux"):
    import fcntl

from aiohttp import web, WSMsgType

PORT = 8765
TOKEN = secrets.token_urlsafe(6)  # stops other devices on the network from grabbing your mouse

# ---------------------------------------------------------------- input backends
# Every backend offers the same methods; Pointer and the server only talk to these.
#   move(dx, dy)          relative cursor move in pixels
#   button(name, down)    name is "left" | "right" | "middle"
#   scroll(v, h)          in 1/120ths of a wheel notch; v > 0 = up, h > 0 = right
#   type_text(text)       types a string
#   press_key(name, n)    name is "enter" | "backspace"


class WindowsInput:
    name = "Windows (SendInput)"
    MOVE, WHEEL, HWHEEL = 0x0001, 0x0800, 0x1000
    BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
    KEYUP, UNICODE = 0x0002, 0x0004
    VK = {"enter": 0x0D, "backspace": 0x08}

    def __init__(self):
        from ctypes import wintypes
        self.user32 = ctypes.windll.user32

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class _INPUTUNION(ctypes.Union):  # MOUSEINPUT is the largest member, so it sets INPUT's size
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

        self.KEYBDINPUT, self.INPUT = KEYBDINPUT, INPUT

    def _mouse(self, flags, dx=0, dy=0, data=0):
        self.user32.mouse_event(flags, int(dx), int(dy), ctypes.c_uint32(int(data) & 0xFFFFFFFF).value, 0)

    def _send_keys(self, events):
        """events: list of (vk, scan, flags) tuples, sent in one SendInput call."""
        arr = (self.INPUT * len(events))()
        for i, (vk, scan, flags) in enumerate(events):
            arr[i].type = 1  # INPUT_KEYBOARD
            arr[i].u.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
        self.user32.SendInput(len(events), arr, ctypes.sizeof(self.INPUT))

    def move(self, dx, dy):
        self._mouse(self.MOVE, dx, dy)

    def button(self, name, down):
        self._mouse(self.BUTTONS[name][0 if down else 1])

    def scroll(self, v, h):
        if v:
            self._mouse(self.WHEEL, data=v)
        if h:
            self._mouse(self.HWHEEL, data=h)

    def zoom(self, steps):
        """Ctrl + mouse wheel, which nearly every Windows app treats as zoom."""
        self._send_keys([(0x11, 0, 0)])  # VK_CONTROL down
        self._mouse(self.WHEEL, data=120 * steps)
        self._send_keys([(0x11, 0, self.KEYUP)])

    def type_text(self, text):
        """Unicode key events: any character, emoji included, regardless of keyboard layout."""
        events = []
        for ch in text:
            if ch == "\n":
                events += [(self.VK["enter"], 0, 0), (self.VK["enter"], 0, self.KEYUP)]
                continue
            b = ch.encode("utf-16-le")  # emoji become two UTF-16 units; Windows wants each one
            for i in range(0, len(b), 2):
                unit = int.from_bytes(b[i:i + 2], "little")
                events += [(0, unit, self.UNICODE), (0, unit, self.UNICODE | self.KEYUP)]
        if events:
            self._send_keys(events)

    def press_key(self, name, n=1):
        vk = self.VK[name]
        self._send_keys([(vk, 0, 0), (vk, 0, self.KEYUP)] * n)


class LinuxUInput:
    """Virtual mouse + keyboard through /dev/uinput. Works on Wayland (GNOME, KDE...) and X11."""

    name = "Linux (uinput)"
    EV_SYN, EV_KEY, EV_REL = 0, 1, 2
    REL_X, REL_Y, REL_HWHEEL, REL_WHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0, 1, 6, 8, 11, 12
    BTN = {"left": 0x110, "right": 0x111, "middle": 0x112}
    KEY = {"enter": 28, "backspace": 14}
    KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_SPACE, KEY_TAB, KEY_U = 29, 42, 57, 15, 22
    UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT, UI_DEV_CREATE = 0x40045564, 0x40045565, 0x40045566, 0x5501
    EVENT = struct.Struct("llHHi")  # struct input_event: timeval, type, code, value

    # Character -> (keycode, needs shift), for a US layout
    US = {" ": (57, False), "\t": (15, False)}
    for _plain, _shifted, _first in [("1234567890-=", "!@#$%^&*()_+", 2), ("qwertyuiop[]", "QWERTYUIOP{}", 16),
                                     ("asdfghjkl;'`", 'ASDFGHJKL:"~', 30), ("\\", "|", 43),
                                     ("zxcvbnm,./", "ZXCVBNM<>?", 44)]:
        for _i, (_p, _s) in enumerate(zip(_plain, _shifted)):
            US[_p], US[_s] = (_first + _i, False), (_first + _i, True)
    del _plain, _shifted, _first, _i, _p, _s

    def __init__(self):
        rels = [self.REL_X, self.REL_Y, self.REL_WHEEL, self.REL_HWHEEL, self.REL_WHEEL_HI_RES, self.REL_HWHEEL_HI_RES]
        # Separate devices, because desktops can misclassify a single mouse-plus-keyboard device
        self.mouse_fd = self._create(b"Phone Trackpad Mouse", [self.EV_KEY, self.EV_REL], self.BTN.values(), rels)
        self.kbd_fd = self._create(b"Phone Trackpad Keyboard", [self.EV_KEY], range(1, 128), [])
        self.wheel_v = self.wheel_h = 0  # hi-res remainder until a full legacy wheel notch

    def _create(self, name, evbits, keys, rels):
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for ev in evbits:
            fcntl.ioctl(fd, self.UI_SET_EVBIT, ev)
        for k in keys:
            fcntl.ioctl(fd, self.UI_SET_KEYBIT, k)
        for r in rels:
            fcntl.ioctl(fd, self.UI_SET_RELBIT, r)
        # struct uinput_user_dev: name[80], input_id (bus, vendor, product, version), ff_effects_max, abs arrays
        os.write(fd, struct.pack("80sHHHHI", name, 0x06, 0x1209, 0x7ac0, 1, 0) + bytes(4 * 64 * 4))
        fcntl.ioctl(fd, self.UI_DEV_CREATE)
        return fd

    def _emit(self, fd, events):
        data = b"".join(self.EVENT.pack(0, 0, t, c, v) for t, c, v in events)
        os.write(fd, data + self.EVENT.pack(0, 0, self.EV_SYN, 0, 0))

    def move(self, dx, dy):
        self._emit(self.mouse_fd, [(self.EV_REL, self.REL_X, dx), (self.EV_REL, self.REL_Y, dy)])

    def button(self, name, down):
        self._emit(self.mouse_fd, [(self.EV_KEY, self.BTN[name], 1 if down else 0)])

    def scroll(self, v, h):
        # Hi-res events give GNOME smooth scrolling; legacy notches keep older apps working
        events = []
        if v:
            self.wheel_v += v
            notches = int(self.wheel_v / 120)
            self.wheel_v -= notches * 120
            events.append((self.EV_REL, self.REL_WHEEL_HI_RES, v))
            if notches:
                events.append((self.EV_REL, self.REL_WHEEL, notches))
        if h:
            self.wheel_h += h
            notches = int(self.wheel_h / 120)
            self.wheel_h -= notches * 120
            events.append((self.EV_REL, self.REL_HWHEEL_HI_RES, h))
            if notches:
                events.append((self.EV_REL, self.REL_HWHEEL, notches))
        if events:
            self._emit(self.mouse_fd, events)

    def zoom(self, steps):
        """Ctrl + mouse wheel, which GNOME apps and browsers treat as zoom."""
        k = self.EV_KEY
        self._emit(self.kbd_fd, [(k, self.KEY_LEFTCTRL, 1)])
        time.sleep(0.01)  # Ctrl and the wheel come from separate devices; make sure Ctrl lands first
        self._emit(self.mouse_fd, [(self.EV_REL, self.REL_WHEEL_HI_RES, 120 * steps),
                                   (self.EV_REL, self.REL_WHEEL, steps)])
        time.sleep(0.01)
        self._emit(self.kbd_fd, [(k, self.KEY_LEFTCTRL, 0)])

    def _tap(self, code, shift=False, ctrl=False):
        mods = ([self.KEY_LEFTCTRL] if ctrl else []) + ([self.KEY_LEFTSHIFT] if shift else [])
        k = self.EV_KEY
        self._emit(self.kbd_fd, [(k, m, 1) for m in mods] + [(k, code, 1)])
        self._emit(self.kbd_fd, [(k, code, 0)] + [(k, m, 0) for m in reversed(mods)])
        time.sleep(0.003)  # some apps drop keys that arrive in the same instant

    def type_text(self, text):
        for ch in text:
            if ch == "\n":
                self._tap(self.KEY["enter"])
            elif ch in self.US:
                self._tap(*self.US[ch])
            else:
                # Anything else (emoji, accented letters...): GNOME's built-in Unicode entry,
                # Ctrl+Shift+U, the hex code point, then Space
                self._tap(self.KEY_U, shift=True, ctrl=True)
                for d in f"{ord(ch):x}":
                    self._tap(*self.US[d])
                self._tap(self.KEY_SPACE)

    def press_key(self, name, n=1):
        for _ in range(n):
            self._tap(self.KEY[name])


class DryRunInput:
    """For an OS with no backend: prints events so the page can still be tried out."""

    name = "dry run (prints events, controls nothing)"

    def move(self, dx, dy): print("move", dx, dy)
    def button(self, name, down): print("button", name, "down" if down else "up")
    def scroll(self, v, h): print("scroll", v, h)
    def zoom(self, steps): print("zoom", steps)
    def type_text(self, text): print("type", repr(text))
    def press_key(self, name, n=1): print("key", name, "x", n)


UINPUT_HELP = """
Can't open /dev/uinput ({err}). The script needs it to create a virtual mouse and keyboard.

Quick fix, until the next reboot:
    sudo modprobe uinput
    sudo setfacl -m u:$USER:rw /dev/uinput

Permanent fix (only gives access to whoever is logged in at the screen):
    echo uinput | sudo tee /etc/modules-load.d/uinput.conf
    echo 'KERNEL=="uinput", TAG+="uaccess", OPTIONS+="static_node=uinput"' | \\
        sudo tee /etc/udev/rules.d/60-phone-trackpad.rules
    then reboot.
"""


def make_input():
    if sys.platform == "win32":
        return WindowsInput()
    if sys.platform.startswith("linux"):
        try:
            return LinuxUInput()
        except OSError as e:
            sys.exit(UINPUT_HELP.format(err=e.strerror or e))
    return DryRunInput()


class Pointer:
    """Accumulates fractional movement so slow, precise moves don't get lost."""

    def __init__(self, inp):
        self.inp = inp
        self.rx = self.ry = self.sx = self.sy = self.zacc = 0.0
        self.dragging = False

    def zoom(self, spread_change):
        self.zacc += spread_change / 35  # pixels of finger spread per zoom step
        steps = int(self.zacc)
        if steps:
            self.zacc -= steps
            self.inp.zoom(steps)

    def move(self, dx, dy):
        self.rx += dx
        self.ry += dy
        ix, iy = int(self.rx), int(self.ry)
        if ix or iy:
            self.rx -= ix
            self.ry -= iy
            self.inp.move(ix, iy)

    def scroll(self, dx, dy):
        self.sx += dx * 4
        self.sy += dy * 4  # natural scrolling, like iOS
        iy, ix = int(self.sy), int(self.sx)
        self.sy -= iy
        self.sx -= ix
        if iy or ix:
            self.inp.scroll(iy, -ix)

    def click(self, button):
        self.inp.button(button, True)
        self.inp.button(button, False)

    def drag(self, on):
        if on != self.dragging:
            self.dragging = on
            self.inp.button("left", on)


# ---------------------------------------------------------------- web page
PAGE = r"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Trackpad</title>
<style>
  :root { --bg:#0e0f12; --pad:#1a1c21; --line:#2a2d34; --fg:#e8e9ec; --dim:#80848e; --acc:#4f8cff; }
  * { box-sizing:border-box; -webkit-user-select:none; user-select:none; -webkit-touch-callout:none;
      -webkit-tap-highlight-color:transparent; }
  html,body { margin:0; height:100%; background:var(--bg); color:var(--fg); overflow:hidden;
      font:15px -apple-system,system-ui,sans-serif; overscroll-behavior:none; }
  body { display:flex; flex-direction:column; height:100dvh;
      padding:env(safe-area-inset-top) 12px calc(env(safe-area-inset-bottom) + 10px); }
  header { display:flex; align-items:center; gap:10px; padding:10px 4px; }
  #dot { width:9px; height:9px; border-radius:50%; background:#d9534f; flex:none; }
  #dot.on { background:#3ecf6e; }
  #status { color:var(--dim); flex:1; }
  header input { width:110px; accent-color:var(--acc); }
  #pad { flex:1; background:var(--pad); border:1px solid var(--line); border-radius:22px;
      touch-action:none; position:relative; transition:border-color .1s; }
  #pad.dragging { border-color:var(--acc); box-shadow:inset 0 0 0 1px var(--acc); }
  #pad::after { content:"1 finger: move · tap: click · tap then drag: drag\A 2 fingers: scroll · pinch: zoom · tap: right-click";
      white-space:pre; position:absolute; bottom:14px; width:100%; text-align:center;
      color:var(--dim); font-size:12px; line-height:1.6; pointer-events:none; }
  .buttons { display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:10px; margin-top:10px; height:64px; }
  /* Invisible field that receives the iPhone keyboard. 16px font stops iOS from zooming in on focus. */
  #kb { position:fixed; top:0; left:0; width:1px; height:1px; opacity:0; border:0; padding:0;
      font-size:16px; pointer-events:none; }
  button { border:1px solid var(--line); background:var(--pad); color:var(--fg); border-radius:16px;
      font:inherit; font-weight:600; touch-action:none; }
  button.active { background:var(--acc); border-color:var(--acc); }
</style></head>
<body>
<header>
  <span id="dot"></span><span id="status">Connecting…</span>
  <span style="color:var(--dim);font-size:12px">Speed</span>
  <input id="sens" type="range" min="0.5" max="4" step="0.1" value="1.8">
</header>
<div id="pad"></div>
<div class="buttons">
  <button data-btn="left">Left</button>
  <button id="drag">Drag</button>
  <button id="keys">Keys</button>
  <button data-btn="right">Right</button>
</div>
<input id="kb" type="text" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
       enterkeyhint="enter" aria-label="Type on laptop">
<script>
const key = new URLSearchParams(location.search).get("k") || "";
const dot = document.getElementById("dot"), statusEl = document.getElementById("status");
const sens = document.getElementById("sens");
try { const s = localStorage.getItem("sens"); if (s) sens.value = s; } catch (e) {}
sens.oninput = () => { try { localStorage.setItem("sens", sens.value); } catch (e) {} };

let ws;
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws?k=${encodeURIComponent(key)}`);
  ws.onopen = () => { dot.className = "on"; statusEl.textContent = "Connected"; };
  ws.onclose = () => { dot.className = ""; statusEl.textContent = "Reconnecting…"; setTimeout(connect, 1000); };
}
connect();
const send = o => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); };

// Batch movement into one message per frame
let mx = 0, my = 0, sx = 0, sy = 0, zoom = 0;
(function flush() {
  if (mx || my) { send({t:"m", x:mx, y:my}); mx = my = 0; }
  if (sx || sy) { send({t:"s", x:sx, y:sy}); sx = sy = 0; }
  if (zoom) { send({t:"z", d:zoom}); zoom = 0; }
  requestAnimationFrame(flush);
})();

const pad = document.getElementById("pad");
let last = null, startT = 0, maxTouches = 0, travel = 0, vel = 0;

// Double-tap-to-drag: a tap's click is held back briefly. If a second touch
// lands in that window, the gesture becomes a drag (if it moves) or a double-click.
const TAP_MS = 250, TAP_MOVE = 8, DOUBLE_TAP_MS = 220;
let pendingClick = null, armed = false, tapDragging = false;

function setTapDrag(on) {
  tapDragging = on;
  pad.classList.toggle("dragging", on);
  send({t:"d", on});
}

function centroid(touches) {
  let x = 0, y = 0;
  for (const t of touches) { x += t.clientX; y += t.clientY; }
  const spread = touches.length >= 2   // distance between the first two fingers, for pinch
    ? Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY) : 0;
  return { x: x / touches.length, y: y / touches.length, n: touches.length, spread };
}

// Two fingers either scroll or pinch-zoom, never both: the first ~10px of
// movement decides which, then it stays locked until the fingers change.
let twoMode = null, pinchAcc = 0, panAcc = 0;

pad.addEventListener("touchstart", e => {
  e.preventDefault();
  if (maxTouches === 0) {
    startT = performance.now(); travel = 0; vel = 0;
    if (pendingClick && e.touches.length === 1) {   // second touch soon after a tap
      clearTimeout(pendingClick); pendingClick = null; armed = true;
    }
  }
  maxTouches = Math.max(maxTouches, e.touches.length);
  last = centroid(e.touches);
  twoMode = null; pinchAcc = panAcc = 0;
}, {passive:false});

pad.addEventListener("touchmove", e => {
  e.preventDefault();
  const c = centroid(e.touches);
  if (last && c.n === last.n) {
    const dx = c.x - last.x, dy = c.y - last.y;
    travel += Math.abs(dx) + Math.abs(dy);
    if (c.n === 1) {
      if (armed && !tapDragging && travel > TAP_MOVE) setTapDrag(true);
      // Smoothed speed drives acceleration, so one noisy touch sample can't cause a jump
      vel = vel * 0.75 + Math.hypot(dx, dy) * 0.25;
      const accel = 1 + Math.min(vel / 10, 1.5);    // faster swipes go further
      mx += dx * sens.value * accel; my += dy * sens.value * accel;
    } else {
      const dSpread = c.spread - last.spread;
      travel += Math.abs(dSpread);                   // a pinch is not a right-click tap
      if (!twoMode) {
        pinchAcc += Math.abs(dSpread); panAcc += Math.hypot(dx, dy);
        if (pinchAcc > 10 && pinchAcc > panAcc * 1.5) twoMode = "zoom";
        else if (panAcc > 10) twoMode = "scroll";
      }
      if (twoMode === "zoom") zoom += dSpread;       // fingers apart = zoom in
      else if (twoMode === "scroll") { sx += dx; sy += dy; }
    }
  }
  last = c;
}, {passive:false});

function end(e) {
  e.preventDefault();
  if (e.touches.length) { last = centroid(e.touches); return; }
  const isTap = travel < TAP_MOVE && performance.now() - startT < TAP_MS;
  if (tapDragging) {
    setTapDrag(false);                               // lift finger = release button
  } else if (armed) {
    send({t:"c", b:"left"});                         // the first tap's held-back click
    if (isTap && maxTouches === 1) send({t:"c", b:"left"});   // tap-tap = double-click
  } else if (isTap) {
    if (maxTouches >= 2) send({t:"c", b:"right"});
    else pendingClick = setTimeout(() => { pendingClick = null; send({t:"c", b:"left"}); }, DOUBLE_TAP_MS);
  }
  armed = false; maxTouches = 0; last = null;
}
pad.addEventListener("touchend", end, {passive:false});
pad.addEventListener("touchcancel", end, {passive:false});

document.querySelectorAll("[data-btn]").forEach(b =>
  b.addEventListener("touchstart", e => { e.preventDefault(); send({t:"c", b:b.dataset.btn}); }, {passive:false}));

const dragBtn = document.getElementById("drag");
let dragging = false;
dragBtn.addEventListener("touchstart", e => {
  e.preventDefault();
  dragging = !dragging;
  dragBtn.classList.toggle("active", dragging);
  send({t:"d", on:dragging});
}, {passive:false});

// ---- Keyboard ----
// The hidden field always holds a run of invisible zero-width characters with the
// cursor at the end. After each edit we compare against that: characters added at
// the end get typed, sentinel characters removed become Backspaces, then we reset.
// This catches normal typing, QuickType suggestions and dictation alike.
const kb = document.getElementById("kb"), keysBtn = document.getElementById("keys");
const SENTINEL = "​".repeat(32);
let kbOpen = false;

function resetKb() {
  kb.value = SENTINEL;
  kb.setSelectionRange(SENTINEL.length, SENTINEL.length);
}
resetKb();

function flushKb() {
  const v = kb.value;
  let i = 0;
  while (i < v.length && i < SENTINEL.length && v[i] === SENTINEL[i]) i++;
  const removed = SENTINEL.length - i;
  const added = v.slice(i).replace(/​/g, "");
  if (removed) send({t:"key", k:"backspace", n:removed});
  if (added) send({t:"k", text:added});
  resetKb();
}

kb.addEventListener("input", e => { if (!e.isComposing) flushKb(); });
kb.addEventListener("compositionend", flushKb);  // for keyboards that compose (e.g. Hindi, Japanese)
kb.addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); send({t:"key", k:"enter"}); }
});
kb.addEventListener("focus", () => { kbOpen = true; keysBtn.classList.add("active"); resetKb(); });
kb.addEventListener("blur", () => { kbOpen = false; keysBtn.classList.remove("active"); });

// iOS only shows the keyboard when focus() runs in a tap's click handler, so
// opening uses click; closing uses touchstart so the tap can't refocus the field.
keysBtn.addEventListener("touchstart", e => { if (kbOpen) { e.preventDefault(); kb.blur(); } }, {passive:false});
keysBtn.addEventListener("click", () => { if (!kbOpen) kb.focus(); });

document.addEventListener("gesturestart", e => e.preventDefault());
</script>
</body></html>"""


# ---------------------------------------------------------------- server
async def index(request):
    if request.query.get("k") != TOKEN:
        return web.Response(status=403, text="Wrong or missing key. Use the URL printed by the server.")
    return web.Response(text=PAGE, content_type="text/html")


async def ws_handler(request):
    if request.query.get("k") != TOKEN:
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    inp = request.app["input"]
    notify = request.app.get("on_status") or (lambda *a: None)  # lets a GUI show connection status
    p = Pointer(inp)
    print(f"Phone connected: {request.remote}")
    notify("connected", request.remote)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
                t = m.get("t")
                if t == "m":
                    p.move(float(m["x"]), float(m["y"]))
                elif t == "s":
                    p.scroll(float(m["x"]), float(m["y"]))
                elif t == "c":
                    p.click(m["b"])
                elif t == "d":
                    p.drag(bool(m["on"]))
                elif t == "z":
                    await asyncio.to_thread(p.zoom, float(m["d"]))
                elif t == "k":
                    # Typing can take a moment on Linux, so it runs off the event loop
                    await asyncio.to_thread(inp.type_text, str(m["text"])[:2000])
                elif t == "key" and m["k"] in ("enter", "backspace"):
                    await asyncio.to_thread(inp.press_key, m["k"], max(1, min(int(m.get("n", 1)), 200)))
            except (ValueError, KeyError, TypeError):
                pass
    finally:
        p.drag(False)  # never leave the button stuck down
        print("Phone disconnected")
        notify("disconnected", request.remote)
    return ws


def route_ip():
    """The address the OS would send outside traffic from. With a VPN on, that's the VPN's."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # UDP connect sends nothing; it only picks a route
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def physical_ips():
    """(adapter, IPv4) pairs for real network hardware only, skipping VPN tunnels,
    Docker, Hyper-V and other virtual adapters, so a VPN can't hijack the URL."""
    found = []
    if sys.platform == "win32":
        import subprocess
        ps = ("Get-NetAdapter -Physical | Where-Object Status -eq Up | "
              "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
              "ForEach-Object { $_.InterfaceAlias + '|' + $_.IPAddress }")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                                 text=True, timeout=15, creationflags=0x08000000).stdout  # no console flash
            found = [tuple(line.strip().split("|", 1)) for line in out.splitlines() if "|" in line]
        except (OSError, subprocess.SubprocessError):
            pass
    elif sys.platform.startswith("linux"):
        # Only hardware NICs have a "device" link in sysfs; tun/wg/docker/bridges don't
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _, name in socket.if_nameindex():
                if not os.path.exists(f"/sys/class/net/{name}/device"):
                    continue
                try:
                    req = struct.pack("256s", name[:15].encode())
                    addr = fcntl.ioctl(s.fileno(), 0x8915, req)[20:24]  # SIOCGIFADDR
                    found.append((name, socket.inet_ntoa(addr)))
                except OSError:
                    pass  # adapter has no IPv4 address (e.g. unplugged Ethernet)
        finally:
            s.close()
    return [(n, ip) for n, ip in found if not ip.startswith(("127.", "169.254."))]


def pick_ips():
    """Best LAN address first, then any other candidates."""
    cands = physical_ips()
    if not cands:
        ip = route_ip()
        return [("default route", ip or "127.0.0.1")]
    best = route_ip()  # with no VPN, this tells us which adapter is actually in use
    rank = lambda c: (c[1] != best, not c[1].startswith("192.168."), not c[1].startswith("172."))
    return sorted(cands, key=rank)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Use your phone as a trackpad and keyboard.")
    ap.add_argument("--ip", help="laptop address to show in the URL (find it with ipconfig / ip addr)")
    args = ap.parse_args()

    inp = make_input()
    ips = [("--ip", args.ip)] if args.ip else pick_ips()
    url = f"http://{ips[0][1]}:{PORT}/?k={TOKEN}"
    print(f"\nPhone Trackpad running, input: {inp.name}. Open this on your phone:\n")
    print("   ", url, f"   ({ips[0][0]})\n")
    if len(ips) > 1:
        print("If that doesn't load, try:")
        for name, ip in ips[1:]:
            print(f"    http://{ip}:{PORT}/?k={TOKEN}   ({name})")
        print()
    try:
        import qrcode  # optional: pip install qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)  # light-on-dark, for dark terminals
        print()
    except ImportError:
        print("(pip install qrcode to also get a scannable QR code here)\n")
    except Exception:
        pass  # e.g. a terminal that can't show block characters; the URL above still works
    print("Ctrl+C to stop.\n")

    app = web.Application()
    app["input"] = inp
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
