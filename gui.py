"""Tray icon, settings window and controller setup wizard for Controller Keyboard.

Everything here runs on the app's Tk thread except the tray icon's own loop; its menu actions are
handed to the Tk thread through app.commands.
"""

import math
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

try:
    import pystray
except ImportError:  # the app still works without a tray icon
    pystray = None

ACCENT = (76, 141, 255)

# The controls, in setup-wizard order: (settings key, name, what it does, "stick" = needs an axis)
CONTROLS = [
    ("stick_x", "Joystick left / right", "Mouse, turning the wheel", "stick"),
    ("stick_y", "Joystick up / down", "Mouse, turning the wheel", "stick"),
    ("a", "A", "Click; type the highlighted character", ""),
    ("b", "B", "Escape; backspace while typing", ""),
    ("z", "Z", "Hold to type; tap = right click", ""),
    ("start", "Start", "Windows key; Shift while typing", ""),
    ("home", "Home", "Switch controller / desktop mode", ""),
    ("stick_click", "Joystick press", "Next wheel while typing", ""),
    ("l", "L", "Browser Back", ""),
    ("r", "R", "Browser Forward", ""),
    ("c_up", "C-up", "Scroll up", ""),
    ("c_down", "C-down", "Scroll down; Enter while typing", ""),
    ("c_left", "C-left", "Scroll left", ""),
    ("c_right", "C-right", "Scroll right", ""),
    ("dpad_up", "D-pad up", "Arrow key", ""),
    ("dpad_down", "D-pad down", "Arrow key", ""),
    ("dpad_left", "D-pad left", "Arrow key", ""),
    ("dpad_right", "D-pad right", "Arrow key", ""),
]
PROMPTS = {
    "stick_x": "Push the joystick to the RIGHT",
    "stick_y": "Push the joystick DOWN",
    "c_up": "Press C-up (or push the right stick up)",
    "c_down": "Press C-down (or push the right stick down)",
    "c_left": "Press C-left (or push the right stick left)",
    "c_right": "Press C-right (or push the right stick right)",
    "z": "Press Z (or the trigger you want to hold for typing)",
    "home": "Press Home (the button that switches modes)",
    "stick_click": "Press the joystick in, like a button",
}

# Settings sliders: (key, label, low, high, decimals, unit, needs a wheel preview)
SLIDERS = [
    ("mouse_max_speed", "Mouse speed", 300, 4000, 0, " px/s", False),
    ("mouse_curve", "Mouse precision near the center", 1.0, 3.5, 1, "", False),
    ("stick_deadzone", "Joystick dead zone", 0.05, 0.40, 2, "", False),
    ("aim_threshold", "Tilt needed to start the wheel", 0.30, 0.90, 2, "", False),
    ("release_threshold", "Tilt that counts as let go", 0.10, 0.50, 2, "", False),
    ("step_deadband_degrees", "Turn past a step before it moves", 0, 20, 0, "°", False),
    ("scroll_repeat_per_sec", "Scroll speed", 2, 30, 0, " /s", False),
    ("overlay_scale", "Wheel size", 0.6, 2.0, 2, "×", True),
    ("overlay_opacity", "Wheel opacity", 0.3, 1.0, 2, "", True),
]


def describe(spec, stick=False):
    """A settings.json control as words: 8 -> 'button 8', 'a4+' -> 'axis 4 +', 'h0:up' -> 'D-pad 0 up'."""
    if stick:
        return f"axis {str(spec).lower().lstrip('a')}"
    if isinstance(spec, int):
        return f"button {spec}"
    s = str(spec).lower()
    if s.startswith("a") and s[-1:] in "+-":
        return f"axis {s[1:-1]} {s[-1]}"
    if s.startswith("h") and ":" in s:
        idx, d = s[1:].split(":", 1)
        return f"D-pad {idx} {d}"
    return s


# ---------------------------------------------------------------- start at login
STARTUP_LINK = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup",
                            "Controller Keyboard.lnk")


def startup_enabled():
    return os.path.exists(STARTUP_LINK)


def set_startup(on, script):
    """Add or remove the Startup-folder shortcut. Raises OSError / CalledProcessError on failure."""
    if not on:
        if os.path.exists(STARTUP_LINK):
            os.remove(STARTUP_LINK)
        return
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    q = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731  (PowerShell single-quoted string)
    ps = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({q(STARTUP_LINK)}); "
          f"$s.TargetPath = {q(exe)}; $s.Arguments = {q(chr(34) + script + chr(34))}; "
          f"$s.WorkingDirectory = {q(os.path.dirname(script))}; "
          f"$s.Description = 'Controller as mouse and keyboard'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], check=True,
                   capture_output=True, creationflags=0x08000000)  # CREATE_NO_WINDOW


# ---------------------------------------------------------------- icon
def make_icon(state, size=64):
    """state: 'desktop' (blue), 'controller' (gray) or 'none' (outline: no controller)."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = {"desktop": ACCENT, "controller": (110, 117, 133)}.get(state)
    if fill:
        d.rounded_rectangle((8, 8, s - 8, s - 8), radius=s * 0.22, fill=fill + (255,))
        dot = (255, 255, 255, 255)
    else:
        d.rounded_rectangle((14, 14, s - 14, s - 14), radius=s * 0.22, outline=(150, 155, 168, 255), width=16)
        dot = (150, 155, 168, 255)
    # a little arc of characters with the highlighted one at the top
    cx, cy, r = s / 2, s * 0.86, s * 0.52
    for i, ang in enumerate((-50, -25, 0, 25, 50)):
        a = math.radians(ang)
        x, y = cx + r * math.sin(a), cy - r * math.cos(a)
        rad = s * (0.11 if ang == 0 else 0.06)
        d.ellipse((x - rad, y - rad, x + rad, y + rad), fill=dot)
    return img.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- the GUI
class Gui:
    def __init__(self, app):
        self.app = app
        self.ck = sys.modules[type(app).__module__]      # the main module (constants, parsers)
        self.root = app.overlay.root
        self.quitting = False
        self.window = None
        self.icon = None
        app.listeners.append(self.refresh_tray)
        if pystray is not None:
            try:
                self.start_tray()
            except Exception as e:  # no tray: Settings still opens by starting the app again
                self.ck.log(f"tray icon not started: {e!r}")
                self.icon = None
        else:
            self.ck.log("pystray not installed: no tray icon")

    # ----- tray
    def start_tray(self):
        app, put = self.app, self.app.commands.put
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self.status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Desktop mode", lambda icon, item: put(self.toggle_mode),
                             checked=lambda item: app.mode == "desktop"),
            pystray.MenuItem("Settings…", lambda icon, item: put(self.open_settings), default=True),
            pystray.MenuItem("Start at login", lambda icon, item: put(self.toggle_startup),
                             checked=lambda item: startup_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda icon, item: put(self.quit)),
        )
        self.icon = pystray.Icon("ControllerKeyboard", make_icon(self.icon_state()), self.tooltip(), menu)
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    def icon_state(self):
        if self.app.joy is None:
            return "none"
        return self.app.mode

    def status_text(self):
        name = self.app.controller_name()
        return f"Connected: {name}" if name else "No controller connected"

    def tooltip(self):
        mode = "Desktop mode" if self.app.mode == "desktop" else "Controller mode"
        return f"Controller Keyboard – {mode}" if self.app.joy is not None else \
            "Controller Keyboard – no controller"

    def refresh_tray(self):
        if self.icon is None:
            return
        try:
            self.icon.icon = make_icon(self.icon_state())
            self.icon.title = self.tooltip()
            self.icon.update_menu()
        except Exception as e:
            self.ck.log(f"tray update failed: {e!r}")

    def toggle_mode(self):
        self.app.set_mode("desktop" if self.app.mode == "controller" else "controller")

    def toggle_startup(self):
        try:
            set_startup(not startup_enabled(), self.script_path())
        except Exception as e:
            messagebox.showerror("Controller Keyboard", f"Couldn't change the startup shortcut:\n{e}")
        self.refresh_tray()
        if self.window:
            self.window.startup_var.set(startup_enabled())

    def script_path(self):
        return os.path.join(self.ck.HERE, "controller_keyboard.pyw")

    def quit(self):
        self.quitting = True
        if self.window:
            self.window.close()
        self.app.release_all()
        self.stop()
        self.root.after(50, self.root.destroy)

    def stop(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None

    # ----- settings window
    def open_settings(self, tab=None):
        if self.window is None:
            self.window = SettingsWindow(self)
        else:
            self.window.show()
        if tab == "controller":
            self.window.nb.select(self.window.controller)


class SettingsWindow:
    def __init__(self, gui):
        self.gui, self.app, self.ck = gui, gui.app, gui.ck
        self.top = tk.Toplevel(gui.root)
        self.top.title("Controller Keyboard")
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.icon_img = ImageTk.PhotoImage(make_icon("desktop", 32), master=self.top)
        self.top.iconphoto(False, self.icon_img)
        self.save_job = None
        self.wizard = None

        style = ttk.Style(self.top)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Prompt.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Muted.TLabel", foreground="#5f6672")
        self.px = self.top.winfo_fpixels("1i") / 96.0          # pixel sizes below are at 100% scaling
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=int(line * 1.45))
        self.top.minsize(int(600 * self.px), int(540 * self.px))

        outer = ttk.Frame(self.top, padding=12)
        outer.pack(fill="both", expand=True)
        self.nb = nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)
        nb.enable_traversal()                  # Ctrl+Tab switches tabs
        self.general = ttk.Frame(nb, padding=14)
        self.controller = ttk.Frame(nb, padding=14)
        nb.add(self.general, text="General")
        nb.add(self.controller, text="Controller")
        self.build_general()
        self.build_controller()
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Label(bottom, text=f"Settings file: {self.ck.SETTINGS_PATH}", style="Muted.TLabel").pack(side="left")
        ttk.Button(bottom, text="Close", command=self.close).pack(side="right")

        self.live_job = None
        self.update_live()
        self.show()

    def show(self):
        self.top.deiconify()
        self.top.lift()
        self.top.attributes("-topmost", True)
        self.top.after(200, lambda: self.top.attributes("-topmost", False))
        self.top.focus_force()

    def close(self):
        if self.wizard:
            self.wizard.cancel()
        if self.save_job:
            self.top.after_cancel(self.save_job)
            self.app.save_settings()
        if self.live_job:
            self.top.after_cancel(self.live_job)
        self.top.destroy()
        self.gui.window = None

    # ----- General tab
    def build_general(self):
        f = self.general
        f.columnconfigure(1, weight=1)
        self.slider_vars, self.value_labels = {}, {}
        for row, (key, label, lo, hi, dec, unit, preview) in enumerate(SLIDERS):
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 12))
            var = tk.DoubleVar(value=float(self.app.settings.get(key, self.ck.DEFAULT_SETTINGS[key])))
            scale = ttk.Scale(f, from_=lo, to=hi, variable=var,
                              command=lambda v, k=key, d=dec, p=preview: self.slider_moved(k, d, p))
            scale.grid(row=row, column=1, sticky="ew", pady=5)
            val = ttk.Label(f, width=9, anchor="e")
            val.grid(row=row, column=2, sticky="e", padx=(10, 0))
            self.slider_vars[key], self.value_labels[key] = var, val
            self.show_value(key)
        row = len(SLIDERS)
        ttk.Separator(f).grid(row=row, column=0, columnspan=3, sticky="ew", pady=12)
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        ttk.Checkbutton(f, text="Start Controller Keyboard when I sign in", variable=self.startup_var,
                        command=self.startup_clicked).grid(row=row + 1, column=0, columnspan=2, sticky="w")
        ttk.Button(f, text="Restore defaults", command=self.restore_defaults).grid(row=row + 1, column=2, sticky="e")
        ttk.Label(f, text="Changes apply right away. Moving the wheel sliders shows the wheel.",
                  style="Muted.TLabel").grid(row=row + 2, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def spec_of(self, key):
        for k, label, lo, hi, dec, unit, preview in SLIDERS:
            if k == key:
                return dec, unit
        return 2, ""

    def show_value(self, key):
        dec, unit = self.spec_of(key)
        v = self.slider_vars[key].get()
        self.value_labels[key].configure(text=(f"{v:.{dec}f}" if dec else f"{round(v)}") + unit)

    def slider_moved(self, key, dec, preview):
        v = round(self.slider_vars[key].get(), dec) if dec else int(round(self.slider_vars[key].get()))
        s = self.app.settings
        clamped = v
        if key == "release_threshold":            # keep "let go" below "start the wheel"
            clamped = min(v, round(s["aim_threshold"] - 0.05, 2))
        if key == "aim_threshold":
            clamped = max(v, round(s["release_threshold"] + 0.05, 2))
        if clamped != v:
            v = clamped
            self.slider_vars[key].set(v)
        s[key] = v
        self.show_value(key)
        if preview:
            self.app.preview_until = time.monotonic() + 1.5
        self.schedule_save()

    def schedule_save(self):
        if self.save_job:
            self.top.after_cancel(self.save_job)
        self.save_job = self.top.after(400, self.save_now)

    def save_now(self):
        self.save_job = None
        self.app.save_settings()

    def restore_defaults(self):
        for key, *_ in SLIDERS:
            self.app.settings[key] = self.ck.DEFAULT_SETTINGS[key]
            self.slider_vars[key].set(float(self.ck.DEFAULT_SETTINGS[key]))
            self.show_value(key)
        self.app.preview_until = time.monotonic() + 1.5
        self.schedule_save()

    def startup_clicked(self):
        try:
            set_startup(self.startup_var.get(), self.gui.script_path())
        except Exception as e:
            messagebox.showerror("Controller Keyboard", f"Couldn't change the startup shortcut:\n{e}", parent=self.top)
        self.startup_var.set(startup_enabled())
        self.gui.refresh_tray()

    # ----- Controller tab
    def build_controller(self):
        f = self.controller
        self.status = ttk.Label(f, text="")
        self.status.pack(anchor="w")
        ttk.Label(f, text="Press a control to see it light up here. Change any that are wrong.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 8))

        self.table_frame = ttk.Frame(f)
        self.table_frame.pack(fill="both", expand=True)
        cols = ("control", "does", "input", "live")
        self.tree = ttk.Treeview(self.table_frame, columns=cols, show="headings", height=12, selectmode="browse")
        for c, text, w, anchor in (("control", "Control", 150, "w"), ("does", "Does", 240, "w"),
                                   ("input", "Input", 100, "w"), ("live", "", 36, "center")):
            self.tree.heading(c, text=text, anchor=anchor)
            self.tree.column(c, width=int(w * self.px), minwidth=int(w * self.px * 0.7), anchor=anchor,
                             stretch=(c == "does"))
        sb = ttk.Scrollbar(self.table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("on", background="#d6e4ff")
        self.tree.bind("<Double-1>", lambda e: self.change_selected())
        self.fill_table()

        self.buttons = ttk.Frame(f)
        self.buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(self.buttons, text="Set up all…", command=lambda: self.start_wizard(None)).pack(side="left")
        ttk.Button(self.buttons, text="Change selected…", command=self.change_selected).pack(side="left", padx=6)
        self.detect_btn = ttk.Button(self.buttons, text="Auto-detect", command=self.auto_detect)
        self.detect_btn.pack(side="left")
        ttk.Button(self.buttons, text="Reset to 8BitDo 64", command=self.reset_layout).pack(side="right")

        # the wizard's panel replaces the table while it runs
        self.wiz_frame = ttk.Frame(f, padding=(0, 20))
        self.wiz_step = ttk.Label(self.wiz_frame, style="Muted.TLabel")
        self.wiz_step.pack(anchor="w")
        self.wiz_prompt = ttk.Label(self.wiz_frame, style="Prompt.TLabel", wraplength=480)
        self.wiz_prompt.pack(anchor="w", pady=(6, 4))
        self.wiz_detail = ttk.Label(self.wiz_frame, style="Muted.TLabel", wraplength=480)
        self.wiz_detail.pack(anchor="w")
        self.wiz_heard = ttk.Label(self.wiz_frame, text="")
        self.wiz_heard.pack(anchor="w", pady=(16, 0))
        wb = ttk.Frame(self.wiz_frame)
        wb.pack(anchor="w", pady=(20, 0))
        ttk.Button(wb, text="Skip (keep current)", command=lambda: self.wizard and self.wizard.skip()).pack(side="left")
        ttk.Button(wb, text="Cancel", command=lambda: self.wizard and self.wizard.cancel()).pack(side="left", padx=6)

    def fill_table(self):
        self.tree.delete(*self.tree.get_children())
        layout = self.app.settings["controller"]
        for key, name, does, kind in CONTROLS:
            self.tree.insert("", "end", iid=key, values=(name, does, describe(layout.get(key), kind == "stick"), ""))

    def change_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Controller Keyboard", "Pick a control in the list first.", parent=self.top)
            return
        self.start_wizard([sel[0]])

    def update_live(self):
        self.live_job = self.top.after(60, self.update_live)
        j = self.app.joy
        name = self.app.controller_name()
        self.status.configure(text=f"Connected: {name}" if name else "No controller connected. Turn it on to set it up.")
        self.detect_btn.state(["!disabled"] if j is not None else ["disabled"])
        if self.wizard or j is None:
            return
        for key, _name, _does, kind in CONTROLS:
            spec = self.app.inputs.get(key)
            mark = ""
            try:
                if kind == "stick":
                    v = self.ck.read_axis(j, spec[1])
                    mark = ("▶" if v > 0 else "◀") if abs(v) > 0.5 else ""
                elif self.ck.read_input(j, spec):
                    mark = "●"
            except Exception:
                mark = "?"
            if self.tree.set(key, "live") != mark:
                self.tree.set(key, "live", mark)
                self.tree.item(key, tags=("on",) if mark else ())

    def save_layout(self, layout):
        settings = dict(self.app.settings)
        settings["controller"] = dict(layout)
        try:
            self.app.apply_settings(settings)
        except ValueError as e:
            messagebox.showerror("Controller Keyboard", f"That layout didn't work:\n{e}", parent=self.top)
            return False
        self.fill_table()
        return True

    def reset_layout(self):
        if messagebox.askyesno("Controller Keyboard", "Use the 8BitDo 64 layout for every control?", parent=self.top):
            self.save_layout(self.ck.DEFAULT_CONTROLLER)

    def auto_detect(self):
        layout, message = detect_layout(self.app, dict(self.app.settings["controller"]))
        if layout is None:
            messagebox.showinfo("Controller Keyboard", message, parent=self.top)
            return
        if messagebox.askyesno("Controller Keyboard", message + "\n\nUse it?", parent=self.top):
            self.save_layout(layout)

    def start_wizard(self, keys):
        if self.app.joy is None:
            messagebox.showinfo("Controller Keyboard", "Connect the controller first.", parent=self.top)
            return
        self.table_frame.pack_forget()
        self.buttons.pack_forget()
        self.wiz_frame.pack(fill="both", expand=True)
        self.wizard = Wizard(self, keys)

    def wizard_done(self, layout):
        self.wizard = None
        self.wiz_frame.pack_forget()
        self.table_frame.pack(fill="both", expand=True)
        self.buttons.pack(fill="x", pady=(10, 0))
        if layout is not None:
            self.save_layout(layout)


class Wizard:
    """Asks for each control in turn and records the first button, axis or D-pad direction that moves."""

    def __init__(self, window, keys):
        self.w, self.app, self.ck = window, window.app, window.ck
        self.steps = [c for c in CONTROLS if keys is None or c[0] in keys]
        self.layout = dict(self.app.settings["controller"])
        self.i = 0
        self.wait_release = True       # start once everything is at rest
        self.app.release_all()
        self.app.reset_typing()
        self.app.capturing = True
        self.rest = None
        self.job = None
        self.prompt()
        self.poll()

    def snapshot(self):
        j = self.app.joy
        if j is None:
            return None
        return ({i for i in range(j.get_numbuttons()) if j.get_button(i)},
                [j.get_axis(i) for i in range(j.get_numaxes())],
                [j.get_hat(i) for i in range(j.get_numhats())])

    def prompt(self):
        key, name, does, kind = self.steps[self.i]
        self.w.wiz_step.configure(text=f"Step {self.i + 1} of {len(self.steps)}")
        self.w.wiz_prompt.configure(text=PROMPTS.get(key, f"Press {name}"))
        self.w.wiz_detail.configure(text=f"{name}: {does}. Currently {describe(self.layout.get(key), kind == 'stick')}.")
        self.w.wiz_heard.configure(text="Waiting…" if not self.wait_release else "Let go of everything…")

    def at_rest(self, snap):
        buttons, axes, hats = snap
        if self.rest is None:
            return not buttons and all(h == (0, 0) for h in hats)
        return (not buttons and all(h == (0, 0) for h in hats)
                and all(abs(a - r) < 0.3 for a, r in zip(axes, self.rest[1])))

    def poll(self):
        self.job = self.w.top.after(15, self.poll)
        snap = self.snapshot()
        if snap is None:
            self.w.wiz_heard.configure(text="The controller disconnected. Turn it back on to continue.")
            return
        if self.rest is None:
            if self.at_rest(snap):
                self.rest = snap               # sticks and triggers as they sit untouched
                self.wait_release = False
                self.prompt()
            return
        if self.wait_release:
            if self.at_rest(snap):
                self.wait_release = False
                self.prompt()
            return
        found = self.detect(snap)
        if found is None:
            return
        key, name, _does, kind = self.steps[self.i]
        self.layout[key] = found
        self.w.wiz_heard.configure(text=f"Got it: {describe(found, kind == 'stick')}")
        self.next()

    def detect(self, snap):
        buttons, axes, hats = snap
        key, _name, _does, kind = self.steps[self.i]
        stick_axes = {int(str(self.layout[k]).lower().lstrip("a")) for k in ("stick_x", "stick_y")}
        moved = [(i, v) for i, (v, r) in enumerate(zip(axes, self.rest[1])) if abs(v - r) > 0.6 and abs(v) > 0.5]
        if kind == "stick":
            return moved[0][0] if moved else None
        new_buttons = sorted(buttons - self.rest[0])
        if new_buttons:
            return new_buttons[0]
        for i, (hx, hy) in enumerate(hats):
            for d, on in (("up", hy > 0), ("down", hy < 0), ("left", hx < 0), ("right", hx > 0)):
                if on:
                    return f"h{i}:{d}"
        for i, v in moved:
            if i in stick_axes:
                continue                       # the joystick wobbling while a button is pressed
            return f"a{i}{'+' if v > 0 else '-'}"
        return None

    def next(self):
        self.i += 1
        self.wait_release = True
        if self.i >= len(self.steps):
            self.finish(self.layout)
        else:
            self.w.top.after(350, self.prompt)

    def skip(self):
        self.w.wiz_heard.configure(text="Skipped")
        self.next()

    def finish(self, layout):
        if self.job:
            self.w.top.after_cancel(self.job)
            self.job = None
        self.app.capturing = False
        self.w.wizard_done(layout)

    def cancel(self):
        self.finish(None)


# ---------------------------------------------------------------- auto-detect
SDL_TO_LAYOUT = {
    "a": "a", "b": "b", "lefttrigger": "z", "leftshoulder": "l", "rightshoulder": "r", "start": "start",
    "guide": "home", "leftstick": "stick_click",
    "dpup": "dpad_up", "dpdown": "dpad_down", "dpleft": "dpad_left", "dpright": "dpad_right",
}
HAT_BITS = {1: "up", 2: "right", 4: "down", 8: "left"}


def sdl_mapping(app):
    """The controller's layout from SDL's controller database, as {'a': 'b0', ...}, or None if unknown."""
    if app.joy_index is None:
        return None, None
    from pygame._sdl2 import controller as sdlc
    if not sdlc.get_init():
        sdlc.init()
    if not sdlc.is_controller(app.joy_index):
        return None, None
    c = sdlc.Controller(app.joy_index)         # left open: closing it can close the shared device
    mapping, name = c.get_mapping(), c.name
    if isinstance(mapping, str):
        parts = mapping.split(",")[2:]
        mapping = dict(p.split(":", 1) for p in parts if ":" in p)
    return dict(mapping), name


def convert_sdl(mapping, layout):
    """Turn SDL's mapping into our settings.json layout (what SDL doesn't list stays as it was)."""
    out = dict(layout)

    def raw(v):
        v = str(v).strip()
        sign = ""
        if v[:1] in "+-":
            sign, v = v[0], v[1:]
        inverted = v.endswith("~")
        return sign, v.rstrip("~"), inverted

    for sdl, ours in SDL_TO_LAYOUT.items():
        if sdl not in mapping:
            continue
        sign, v, inverted = raw(mapping[sdl])
        if v.startswith("b") and v[1:].isdigit():
            out[ours] = int(v[1:])
        elif v.startswith("a") and v[1:].isdigit():
            direction = sign or ("-" if inverted else "+")   # a whole-axis trigger rests at -1
            out[ours] = f"a{v[1:]}{direction}"
        elif v.startswith("h") and "." in v:
            idx, bits = v[1:].split(".", 1)
            if bits.isdigit() and int(bits) in HAT_BITS:
                out[ours] = f"h{idx}:{HAT_BITS[int(bits)]}"
    for sdl, ours in (("leftx", "stick_x"), ("lefty", "stick_y")):
        if sdl in mapping:
            _s, v, _inv = raw(mapping[sdl])
            if v.startswith("a") and v[1:].isdigit():
                out[ours] = int(v[1:])
    for sdl, neg, pos in (("rightx", "c_left", "c_right"), ("righty", "c_up", "c_down")):
        if sdl in mapping:
            _s, v, inverted = raw(mapping[sdl])
            if v.startswith("a") and v[1:].isdigit():
                n = v[1:]
                out[neg], out[pos] = (f"a{n}+", f"a{n}-") if inverted else (f"a{n}-", f"a{n}+")
    return out


def detect_layout(app, layout):
    try:
        mapping, name = sdl_mapping(app)
    except Exception as e:
        return None, f"Auto-detect didn't work: {e}"
    if mapping is None:
        return None, ("SDL doesn't know this controller, so its layout can't be detected.\n"
                      "Use “Set up all…” to press each control instead.")
    new = convert_sdl(mapping, layout)
    changed = sum(1 for k in new if new[k] != layout.get(k))
    return new, (f"Found a layout for {name}.\n{changed} control(s) would change: triggers become Z, the left "
                 f"stick moves the mouse, the right stick scrolls, and the D-pad is the arrow keys.")
