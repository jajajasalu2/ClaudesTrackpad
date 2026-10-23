"""
Claude's Trackpad: a Windows tray app around phone_trackpad.py.

Launching it opens a small window with the QR code and link for your phone. The trackpad
server keeps running in the background: closing the window just hides it to the tray.
Click the tray icon to bring the window back; right-click it and choose Quit to stop.

Build it into an app with build.bat (see README.txt). To run from source instead:
    pip install aiohttp qrcode pystray pillow
    pythonw claudes_trackpad.py
"""

import asyncio
import os
import queue
import secrets
import socket
import sys
import threading
import tkinter as tk
import traceback

import phone_trackpad as pt

APP_NAME = "Claude's Trackpad"
CONTROL_PORT = pt.PORT + 1  # localhost-only; a second launch uses it to wake the running copy
DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "ClaudesTrackpad")
TOKEN_FILE = os.path.join(DATA_DIR, "token.txt")

BG, PANEL, LINE, FG, DIM = "#0e0f12", "#1a1c21", "#2a2d34", "#e8e9ec", "#80848e"
ACCENT, GREEN, RED = "#4f8cff", "#3ecf6e", "#e5484d"


# ---------------------------------------------------------------- small helpers
def make_icon(size=256):
    """The app icon, drawn in code so there's no image file to lose: a blue tile with a trackpad and a fingertip."""
    from PIL import Image, ImageDraw
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([s * .04, s * .04, s * .96, s * .96], radius=s * .22, fill=ACCENT)
    d.rounded_rectangle([s * .24, s * .16, s * .76, s * .84], radius=s * .1, outline="white",
                        width=max(2, round(s * .07)))
    d.ellipse([s * .40, s * .38, s * .60, s * .58], fill="white")
    return img


def load_token(new=False):
    """The secret in the link. It's saved, so a bookmark or Home Screen icon on the phone keeps working."""
    if not new:
        try:
            with open(TOKEN_FILE) as f:
                token = f.read().strip()
            if token:
                return token
        except OSError:
            pass
    token = secrets.token_urlsafe(9)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(token)
    except OSError:
        pass  # still works this session, just won't be remembered
    return token


def wake_running_copy():
    """If the app is already running, ask it to show its window. Returns True if it was."""
    try:
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=1) as s:
            s.sendall(b"show")
        return True
    except OSError:
        return False


def listen_for_wakeups(events):
    """Returns False if another copy already owns the control port."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: stop anything else sharing the port
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        srv.bind(("127.0.0.1", CONTROL_PORT))
    except OSError:
        return False
    srv.listen(4)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                conn.close()
                events.put(("show",))
            except OSError:
                return

    threading.Thread(target=loop, daemon=True).start()
    return True


class Server(threading.Thread):
    """The aiohttp trackpad server from phone_trackpad.py, on its own thread and event loop."""

    def __init__(self, inp, events):
        super().__init__(daemon=True)
        self.inp, self.events = inp, events
        self.loop = self.runner = None

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        app = pt.web.Application()
        app["input"] = self.inp
        app["on_status"] = lambda what, who: self.events.put((what, who))
        app.router.add_get("/", pt.index)
        app.router.add_get("/ws", pt.ws_handler)
        self.runner = pt.web.AppRunner(app)
        self.loop.run_until_complete(self.runner.setup())
        try:
            self.loop.run_until_complete(pt.web.TCPSite(self.runner, "0.0.0.0", pt.PORT).start())
        except OSError:
            self.events.put(("error", f"Port {pt.PORT} is already in use. Is phone_trackpad.py "
                                      "running in a terminal? Close it, then reopen this app."))
            return
        self.loop.run_forever()

    def stop(self):
        if self.loop and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(timeout=3)
            except Exception:
                pass  # quitting anyway
            self.loop.call_soon_threadsafe(self.loop.stop)


# ---------------------------------------------------------------- the window + tray icon
class TrackpadApp:
    def __init__(self):
        if sys.platform == "win32":
            try:
                ctypes = __import__("ctypes")
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
                # Own taskbar identity, so Windows shows our icon rather than Python's
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClaudesTrackpad")
            except Exception:
                pass

        self.events = queue.Queue()  # every other thread talks to the window through this
        if not listen_for_wakeups(self.events):
            sys.exit(0)  # lost a race with another copy starting at the same moment

        pt.TOKEN = load_token()
        self.ips = []  # [(adapter, ip)], best first
        self.ip_idx = 0
        self.phones = set()
        self.told_about_tray = False
        self.error = None

        self.root = tk.Tk()
        self.root.withdraw()  # stay hidden until laid out, so it doesn't flash at the wrong size
        self.scale = self.root.winfo_fpixels("1i") / 96
        self._build_window()

        self.server = Server(pt.make_input(), self.events)
        self.server.start()
        self._start_tray()
        self.refresh_addresses()
        self.show()
        self.root.after(100, self._poll)

    def px(self, n):
        return round(n * self.scale)

    def _build_window(self):
        r = self.root
        r.title(APP_NAME)
        r.configure(bg=BG)
        r.resizable(False, False)
        r.protocol("WM_DELETE_WINDOW", self.hide)
        try:
            from PIL import ImageTk
            self._icon_img = ImageTk.PhotoImage(make_icon(64))
            r.iconphoto(True, self._icon_img)
        except Exception:
            pass

        font = "Segoe UI" if sys.platform == "win32" else "TkDefaultFont"
        mono = "Consolas" if sys.platform == "win32" else "TkFixedFont"
        pad = self.px(24)
        body = tk.Frame(r, bg=BG, padx=pad, pady=pad)
        body.pack()

        tk.Label(body, text="Scan with your phone's camera", bg=BG, fg=FG,
                 font=(font, 14, "bold")).pack()
        tk.Label(body, text="Your phone and this laptop need to be on the same Wi-Fi", bg=BG, fg=DIM,
                 font=(font, 9)).pack(pady=(self.px(2), self.px(16)))

        self.qr_size = self.px(264)
        self.qr = tk.Canvas(body, width=self.qr_size, height=self.qr_size, bg="white",
                            highlightthickness=0)
        self.qr.pack()

        self.url_var = tk.StringVar()
        url = tk.Entry(body, textvariable=self.url_var, state="readonly", readonlybackground=PANEL,
                       fg=FG, relief="flat", justify="center", font=(mono, 10), width=38,
                       highlightthickness=1, highlightbackground=LINE, highlightcolor=LINE)
        url.pack(pady=(self.px(16), self.px(4)), ipady=self.px(6), fill="x")
        self.adapter_label = tk.Label(body, text="Finding your network address…", bg=BG, fg=DIM,
                                      font=(font, 9))
        self.adapter_label.pack()

        status = tk.Frame(body, bg=BG)
        status.pack(pady=(self.px(12), self.px(14)))
        d = self.px(10)
        self.dot = tk.Canvas(status, width=d, height=d, bg=BG, highlightthickness=0)
        self.dot_id = self.dot.create_oval(0, 0, d - 1, d - 1, fill=DIM, outline="")
        self.dot.pack(side="left", padx=(0, self.px(8)))
        self.status_label = tk.Label(status, text="Starting…", bg=BG, fg=FG, font=(font, 10),
                                     wraplength=self.px(300), justify="left")
        self.status_label.pack(side="left")

        buttons = tk.Frame(body, bg=BG)
        buttons.pack(fill="x")

        def button(text, cmd, primary=False):
            b = tk.Button(buttons, text=text, command=cmd, font=(font, 10, "bold" if primary else "normal"),
                          bg=ACCENT if primary else PANEL, fg="white" if primary else FG,
                          activebackground="#3d78e6" if primary else LINE,
                          activeforeground="white" if primary else FG,
                          relief="flat", bd=0, cursor="hand2", padx=self.px(12), pady=self.px(7))
            b.pack(side="left", expand=True, fill="x", padx=self.px(3))
            return b

        self.copy_btn = button("Copy link", self.copy_link, primary=True)
        self.next_btn = button("Other address", self.next_address)
        button("New link", self.new_link)

        tk.Label(body, text="Closing this window keeps the trackpad running in the tray.",
                 bg=BG, fg=DIM, font=(font, 9)).pack(pady=(self.px(16), 0))

    # ---- tray
    def _start_tray(self):
        try:
            import pystray
        except ImportError:
            self.tray = None  # running from source without pystray: closing the window quits
            return
        menu = pystray.Menu(
            pystray.MenuItem("Show QR code", lambda *_: self.events.put(("show",)), default=True),
            pystray.MenuItem("Quit", lambda *_: self.events.put(("quit",))),
        )
        self.tray = pystray.Icon("ClaudesTrackpad", make_icon(64), APP_NAME, menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    # ---- state -> screen
    def current_url(self):
        ip = self.ips[self.ip_idx][1] if self.ips else "127.0.0.1"
        return f"http://{ip}:{pt.PORT}/?k={pt.TOKEN}"

    def render(self):
        url = self.current_url()
        self.url_var.set(url)
        if self.ips:
            name, ip = self.ips[self.ip_idx]
            self.adapter_label.config(text=f"{name}  ·  {ip}")
        self.next_btn.config(state="normal" if len(self.ips) > 1 else "disabled")
        self._draw_qr(url)
        self._render_status()

    def _draw_qr(self, url):
        import qrcode
        qr = qrcode.QRCode(border=3, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()  # includes the quiet zone
        n = len(matrix)
        cell = self.qr_size // n
        off = (self.qr_size - cell * n) // 2
        self.qr.delete("all")
        for y, row in enumerate(matrix):
            x = 0
            while x < n:  # one rectangle per run of dark cells keeps the canvas light
                if row[x]:
                    start = x
                    while x < n and row[x]:
                        x += 1
                    self.qr.create_rectangle(off + start * cell, off + y * cell, off + x * cell,
                                             off + (y + 1) * cell, fill="black", width=0)
                else:
                    x += 1

    def _render_status(self):
        if self.error:
            color, text = RED, self.error
        elif self.phones:
            color = GREEN
            text = "Phone connected" if len(self.phones) == 1 else f"{len(self.phones)} phones connected"
        else:
            color, text = DIM, "Waiting for your phone…"
        self.dot.itemconfig(self.dot_id, fill=color)
        self.status_label.config(text=text, fg=RED if self.error else FG)

    # ---- buttons
    def copy_link(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.current_url())
        self.copy_btn.config(text="Copied!")
        self.root.after(1500, lambda: self.copy_btn.config(text="Copy link"))

    def next_address(self):
        if self.ips:
            self.ip_idx = (self.ip_idx + 1) % len(self.ips)
            self.render()

    def new_link(self):
        pt.TOKEN = load_token(new=True)
        self.render()
        if not self.error:  # never hide an error behind this note
            self.status_label.config(text="New link made. Links scanned before this stop working.")

    def refresh_addresses(self):
        """Looking up adapters takes ~1s on Windows, so it happens off the UI thread."""
        threading.Thread(target=lambda: self.events.put(("ips", pt.pick_ips())), daemon=True).start()

    # ---- window visibility
    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)  # Windows won't always bring a window forward otherwise
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def hide(self):
        if not self.tray:
            return self.quit()
        self.root.withdraw()
        if not self.told_about_tray:
            self.told_about_tray = True
            try:
                self.tray.notify("Still running in the tray. Right-click the icon and choose Quit to stop.",
                                 APP_NAME)
            except Exception:
                pass

    def quit(self):
        if self.tray:
            self.tray.stop()
        self.server.stop()
        self.root.destroy()

    # ---- messages from other threads
    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "show":
                    self.show()
                    self.refresh_addresses()  # you may have switched networks since
                elif kind == "quit":
                    return self.quit()
                elif kind == "ips":
                    old = self.ips[self.ip_idx] if self.ips else None
                    self.ips = ev[1]
                    self.ip_idx = self.ips.index(old) if old in self.ips else 0
                    self.render()
                elif kind == "connected":
                    self.phones.add(ev[1])
                    self._render_status()
                elif kind == "disconnected":
                    self.phones.discard(ev[1])
                    self._render_status()
                elif kind == "error":
                    self.error = ev[1]
                    self._render_status()
                    self.show()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def run(self):
        self.root.mainloop()


def main():
    if "--make-icon" in sys.argv:  # used by build.bat to create the .exe icon
        path = sys.argv[sys.argv.index("--make-icon") + 1]
        make_icon(256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return
    if wake_running_copy():
        return  # already running: we just brought its window to the front
    try:
        TrackpadApp().run()
    except SystemExit:
        raise
    except Exception:
        # A windowed app has no console, so show crashes instead of vanishing silently
        msg = traceback.format_exc()
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "crash.log"), "w") as f:
                f.write(msg)
        except OSError:
            pass
        try:
            from tkinter import messagebox
            messagebox.showerror(APP_NAME, "Claude's Trackpad hit an error and has to close.\n\n" + msg[-1500:])
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
