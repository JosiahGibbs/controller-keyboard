"""Controller Keyboard: a game controller as a mouse and keyboard on Windows, with a scroll-wheel
on-screen keyboard. Built for the 8BitDo 64; other controllers can be mapped in settings.json
(see find_inputs.py). Names below are the 8BitDo 64's buttons.

Controller mode (the startup mode): does nothing, so games see a plain controller.
The 8BitDo (Home) button switches between controller mode and desktop mode.

Desktop mode:
    Joystick = mouse, C buttons = scroll, D-pad = arrow keys, A = left click (hold to drag),
    tap left Z = right click, B = Escape, Start = Windows key, L / R = browser Back / Forward.

Typing (desktop mode, while left Z is held): starts on letters; pressing the joystick in cycles
    letters -> numbers -> symbols. Each wheel ends with backspace and space (before the first character).
    The characters ride the top arc of a wheel. Tilt the joystick, then rotate it: each 1/8 turn steps
    one character (clockwise = forward). The wheel starts on the space and spins back to it when let go.
    A while tilted = type the highlighted character. A with the joystick centered = space.
    B = backspace, C-down = Enter,
    Start = Shift for the next letter (double tap = Caps Lock), D-pad = arrow keys.

Settings are in settings.json next to this file (re-read when it changes).
"""

import ctypes
import json
import math
import os
import queue
import sys
import time
import tkinter as tk
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "settings.json")
LOG_PATH = os.path.join(HERE, "controller_keyboard.log")
DEBUG = "--debug" in sys.argv

os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import pygame  # noqa: E402

# ---------------------------------------------------------------- controller layout
# Which raw SDL input each control is. The defaults are the 8BitDo 64 in its 8BitDo Bluetooth mode
# on Windows (2DC8:3019). For another controller, run find_inputs.py and put its numbers in the
# "controller" section of settings.json. Each control can be:
#   7          button 7
#   "a4+"      axis 4 pushed past +0.5 ("a4-" for past -0.5); triggers that rest at -1 use "+"
#   "h0:up"    D-pad (hat) 0 pressed up (or down / left / right)
# "stick_x" / "stick_y" are axis numbers for the joystick that moves the mouse and turns the wheel.
DEFAULT_CONTROLLER = {
    "a": 0, "b": 1,
    "l": 6, "r": 7,
    "z": 8,                                        # hold to type; tap = right click
    "start": 11,
    "home": 12,                                    # switches controller / desktop mode
    "stick_x": 0, "stick_y": 1,
    # scroll; while typing C-up = Shift (double press: Caps Lock), C-left / C-right = previous / next
    # wheel, C-down = Enter
    "c_up": "a4-", "c_right": "a3+", "c_down": "a4+", "c_left": "a3-",
    "dpad_up": "h0:up", "dpad_down": "h0:down", "dpad_left": "h0:left", "dpad_right": "h0:right",
}
# The 8BitDo 64 with its toggle on Switch: it poses as Nintendo's Switch Online N64 controller
# (057E:2019).
SWITCH_N64_CONTROLLER = {
    "a": 0, "b": 1,
    "l": 9, "r": 10,
    "z": "a4+",
    "start": 6,
    "home": 5,
    "stick_x": 0, "stick_y": 1,
    "c_up": 3, "c_right": 4, "c_down": "a5+", "c_left": 2,
    "dpad_up": 11, "dpad_down": 12, "dpad_left": 13, "dpad_right": 14,
}
# Layouts chosen by the connected controller's USB vendor:product. Anything else uses the
# "controller" section of settings.json; "layouts" in settings.json overrides these per controller.
BUILTIN_LAYOUTS = {
    "057e:2019": SWITCH_N64_CONTROLLER,
}
# Friendlier names for controllers Windows only reports generically (by vendor:product).
KNOWN_CONTROLLERS = {
    "2dc8:3019": "8BitDo 64 (Bluetooth)",
    "057e:2019": "8BitDo 64 (Switch mode)",
}


def controller_id(guid):
    """'vvvv:pppp' from an SDL joystick GUID (vendor and product are little-endian at bytes 4 and 8)."""
    try:
        b = bytes.fromhex(guid)
        return f"{b[5]:02x}{b[4]:02x}:{b[9]:02x}{b[8]:02x}"
    except (ValueError, IndexError, TypeError):
        return None


def layout_key(settings, cid):
    """Where this controller's layout lives: a "layouts" entry, or None for the "controller" section."""
    if cid and (cid in settings.get("layouts", {}) or cid in BUILTIN_LAYOUTS):
        return cid
    return None


def layout_for(settings, cid):
    key = layout_key(settings, cid)
    if key is None:
        return settings["controller"]
    return settings.get("layouts", {}).get(key) or BUILTIN_LAYOUTS[key]
HAT_DIRS = {"up": (1, 1), "down": (1, -1), "left": (0, -1), "right": (0, 1)}   # (hat index, sign)

# ---------------------------------------------------------------- typing lists
# Each layer is a list that wraps around. Backspace and space sit between the last and first
# characters: one counterclockwise step from the start is space, two is backspace.
BKSP = "\b"
LISTS = {
    "letters": list("abcdefghijklmnopqrstuvwxyz") + [BKSP, " "],
    "numbers": list("0123456789.,?!'-") + [BKSP, " "],
    "symbols": list("()[]{}<>@#$%&*^~-_=+/\\|:;\"") + [BKSP, " "],
}


def home_index(chars):
    """Where the wheel starts and returns to: the space."""
    return chars.index(" ")


LAYER_LABEL = {"letters": "abc", "numbers": "123", "symbols": "#+="}
LAYER_ORDER = ["letters", "numbers", "symbols"]

DEFAULT_SETTINGS = {
    "mouse_max_speed": 1500,        # pixels per second at full tilt
    "mouse_curve": 2.0,             # higher = finer control near the center
    "stick_deadzone": 0.15,
    "aim_threshold": 0.55,          # how far the joystick must tilt to pick / step characters
    "release_threshold": 0.30,      # below this the joystick counts as let go
    "step_deadband_degrees": 8,     # extra turn past a boundary before stepping (stops flicker)
    "scroll_repeat_per_sec": 12,
    "overlay_scale": 1.0,
    "overlay_opacity": 0.9,
    # True: turning the joystick clockwise steps forward, so the wheel turns counterclockwise under the
    # highlight, and C-right slides the next wheel in from the right. False: the wheel turns the way
    # the joystick does, and C-right slides the wheel off to the right.
    "invert_wheel": True,
    "wheel_follows_mouse": False,   # show the wheel next to the mouse pointer instead of at the bottom
    "controller": DEFAULT_CONTROLLER,
    "layouts": {},                  # per-controller layouts by "vendor:product" (see BUILTIN_LAYOUTS)
}


def parse_input(spec):
    """Turn a settings.json control (7, "a4+", "h0:up") into ("button", 7) / ("axis", 4, +1) / ("hat", 0, 1, 1).
    A bare number for stick_x / stick_y means an axis."""
    if isinstance(spec, bool):
        raise ValueError(f"bad controller input: {spec!r}")
    if isinstance(spec, int):
        return ("button", spec)
    s = str(spec).strip().lower()
    if s.startswith("a") and s[-1] in "+-" and s[1:-1].isdigit():
        return ("axis", int(s[1:-1]), 1 if s[-1] == "+" else -1)
    if s.startswith("h") and ":" in s:
        idx, d = s[1:].split(":", 1)
        if idx.isdigit() and d in HAT_DIRS:
            part, sign = HAT_DIRS[d]
            return ("hat", int(idx), part, sign)
    if s.isdigit():
        return ("button", int(s))
    raise ValueError(f"bad controller input: {spec!r}")


def parse_controller(controller):
    inputs = {}
    for name, spec in controller.items():
        if name in ("stick_x", "stick_y"):
            inputs[name] = ("axis_value", int(str(spec).lower().lstrip("a")))
        else:
            inputs[name] = parse_input(spec)
    return inputs


def read_input(j, spec):
    kind = spec[0]
    if kind == "button":
        return spec[1] < j.get_numbuttons() and bool(j.get_button(spec[1]))
    if kind == "axis":
        return spec[1] < j.get_numaxes() and j.get_axis(spec[1]) * spec[2] > 0.5
    if kind == "hat":
        return spec[1] < j.get_numhats() and j.get_hat(spec[1])[spec[2]] * spec[3] > 0
    return False


def read_axis(j, index):
    return j.get_axis(index) if 0 <= index < j.get_numaxes() else 0.0


def log(msg):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 512_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass
    if DEBUG:
        print(msg, flush=True)


# ---------------------------------------------------------------- Windows input
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x1, 0x2, 0x4
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x8, 0x10
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x800, 0x1000
VK_BACK, VK_RETURN, VK_MENU, VK_ESCAPE = 0x08, 0x0D, 0x12, 0x1B
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_LWIN = 0x25, 0x26, 0x27, 0x28, 0x5B
EXTENDED_VKS = {VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_LWIN}

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _key(vk, up=False):
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, dwFlags=flags)))


def _mouse(flags, dx=0, dy=0, data=0):
    return INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF,
                                                               dwFlags=flags)))


def key_up(vk):
    _send(_key(vk, up=True))


def key_tap(vk, *modifiers):
    seq = [_key(m) for m in modifiers] + [_key(vk), _key(vk, True)] + [_key(m, True) for m in reversed(modifiers)]
    _send(*seq)


def type_char(ch):
    code = ord(ch)
    _send(INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wScan=code, dwFlags=KEYEVENTF_UNICODE))),
          INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wScan=code,
                                                                 dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))))


def mouse_move(dx, dy):
    _send(_mouse(MOUSEEVENTF_MOVE, dx, dy))


def mouse_flags(flags):
    _send(_mouse(flags))


def scroll(vertical_notches=0, horizontal_notches=0):
    if vertical_notches:
        _send(_mouse(MOUSEEVENTF_WHEEL, data=120 * vertical_notches))
    if horizontal_notches:
        _send(_mouse(MOUSEEVENTF_HWHEEL, data=120 * horizontal_notches))


# ---------------------------------------------------------------- helpers
def direction_of(x, y, current, deadband_deg=8.0):
    """8-way direction (0 = up, clockwise) with hysteresis so it doesn't flicker on a boundary."""
    angle = math.degrees(math.atan2(x, -y)) % 360.0
    if current is not None:
        diff = abs((angle - current * 45.0 + 180.0) % 360.0 - 180.0)
        if diff <= 22.5 + deadband_deg:
            return current
    return int(round(angle / 45.0)) % 8


class Repeater:
    """Key-style auto repeat: fires on press, then after a delay at a steady rate while held."""

    def __init__(self, delay=0.40, interval=0.035):
        self.delay, self.interval = delay, interval
        self.next_time = {}

    def update(self, name, held, now, action):
        if held:
            t = self.next_time.get(name)
            if t is None:
                action()
                self.next_time[name] = now + self.delay
            elif now >= t:
                action()
                self.next_time[name] = now + self.interval
        else:
            self.next_time.pop(name, None)

    def clear(self):
        self.next_time.clear()


# ---------------------------------------------------------------- the app
class App:
    def __init__(self):
        self.settings = dict(DEFAULT_SETTINGS)
        self.inputs = parse_controller(DEFAULT_CONTROLLER)
        self.settings_mtime = None
        self.load_settings()

        pygame.display.init()
        pygame.joystick.init()
        self.joy = None

        self.mode = "controller"
        self.prev = {}
        self.toast_text, self.toast_until = "", 0.0
        self.mouse_acc = [0.0, 0.0]
        self.left_down = False
        self.held_vks = set()
        self.repeater = Repeater()
        self.scroll_repeater = Repeater(delay=0.30, interval=1.0 / max(1, self.settings["scroll_repeat_per_sec"]))
        self.mouse_wait_center = False
        self.reset_typing()

        self.joy_index = None          # SDL device index of the open controller (for auto-detect)
        self.last_read = None          # the latest controller state (the settings window shows it live)
        self.capturing = False         # setup wizard recording inputs: don't act on the controller
        self.preview_until = 0.0       # show the wheel briefly while its size / opacity is changed
        self.commands = queue.Queue()  # from the tray icon's thread: run on this one
        self.listeners = []            # called with no arguments when the mode or connection changes

        self.overlay = Overlay(self)
        self.last_settings_check = 0.0
        log("started")

    # ----- for the GUI
    def notify(self):
        for fn in list(self.listeners):
            try:
                fn()
            except Exception as e:  # a broken listener mustn't stop the app
                log(f"listener error: {e!r}")

    def set_mode(self, mode):
        if mode == self.mode:
            return
        self.release_all()
        self.reset_typing()
        self.mode = mode
        self.toast("Desktop mode" if mode == "desktop" else "Controller mode")
        log(f"mode: {mode}")
        self.notify()

    def apply_settings(self, settings):
        """Use new settings (from the settings window) and save them. Raises ValueError on a bad layout."""
        inputs = parse_controller(layout_for(settings, self.controller_id()))
        self.settings, self.inputs = settings, inputs
        self.save_settings()

    def controller_id(self):
        joy = getattr(self, "joy", None)            # load_settings runs before the joystick is set up
        return controller_id(joy.get_guid()) if joy is not None else None

    def controller_name(self):
        if self.joy is None:
            return None
        return KNOWN_CONTROLLERS.get(self.controller_id(), self.joy.get_name())

    def current_layout(self):
        """The layout in use for the connected controller."""
        return dict(layout_for(self.settings, self.controller_id()))

    def default_layout(self):
        return dict(BUILTIN_LAYOUTS.get(self.controller_id(), DEFAULT_CONTROLLER))

    def settings_with_layout(self, layout):
        """A copy of the settings with the connected controller's layout replaced."""
        settings = dict(self.settings)
        key = layout_key(settings, self.controller_id())
        if key is None:
            settings["controller"] = dict(layout)
        else:
            settings["layouts"] = {**settings.get("layouts", {}), key: dict(layout)}
        return settings

    def use_layout(self):
        """Pick the layout for the controller now connected (or the default one)."""
        try:
            self.inputs = parse_controller(layout_for(self.settings, self.controller_id()))
        except ValueError as e:
            log(f"layout not used: {e}")

    # ----- settings
    def load_settings(self):
        try:
            mtime = os.path.getmtime(SETTINGS_PATH)
        except OSError:
            self.save_settings()
            return
        if mtime == self.settings_mtime:
            return
        self.settings_mtime = mtime
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            settings = {**DEFAULT_SETTINGS, **{k: v for k, v in data.items() if k in DEFAULT_SETTINGS}}
            controller = data.get("controller") if isinstance(data.get("controller"), dict) else {}
            settings["controller"] = {**DEFAULT_CONTROLLER,
                                      **{k: v for k, v in controller.items() if k in DEFAULT_CONTROLLER}}
            layouts = data.get("layouts") if isinstance(data.get("layouts"), dict) else {}
            settings["layouts"] = {}
            for cid, layout in layouts.items():
                if isinstance(layout, dict):
                    base = BUILTIN_LAYOUTS.get(cid.lower(), DEFAULT_CONTROLLER)
                    settings["layouts"][cid.lower()] = {**base, **{k: v for k, v in layout.items() if k in base}}
            for layout in [settings["controller"], *settings["layouts"].values()]:
                parse_controller(layout)                         # raises on a bad entry: keep the old ones
            inputs = parse_controller(layout_for(settings, self.controller_id()))
            self.settings, self.inputs = settings, inputs
        except (OSError, ValueError) as e:
            log(f"settings.json not read: {e}")

    def save_settings(self):
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4)
            self.settings_mtime = os.path.getmtime(SETTINGS_PATH)
        except OSError as e:
            log(f"settings.json not saved: {e}")

    def toast(self, text, seconds=1.6):
        self.toast_text, self.toast_until = text, time.monotonic() + seconds

    # ----- state resets
    def reset_typing(self):
        self.layer = None
        self.layer_since = 0.0
        self.sel = None            # index of the highlighted character, or None (joystick centered)
        self.dir = None            # joystick direction (0-7) the selection last stepped at
        self.a_back = False        # A held on the backspace slot (repeats like B)
        self.show_press = None     # (character, until): B / A pressed with the stick centered
        self.typing_layer = "letters"   # stepped with C-left / C-right while typing
        self.layer_dir = 0              # which way the last step went (-1 left, +1 right): the wheel slides that way
        self.shift_once = False
        self.caps = False
        self.last_start_tap = 0.0
        self.caps_off_at = 0.0
        self.zl_down_at = None
        self.zl_used = False
        self.flash_char, self.flash_until = "", 0.0

    def release_all(self):
        if self.left_down:
            mouse_flags(MOUSEEVENTF_LEFTUP)
            self.left_down = False
        for vk in list(self.held_vks):
            key_up(vk)
        self.held_vks.clear()
        self.repeater.clear()
        self.scroll_repeater.clear()

    # ----- controller connection
    def handle_events(self):
        for ev in pygame.event.get():
            if ev.type == pygame.JOYDEVICEADDED and self.joy is None:
                try:
                    self.joy = pygame.joystick.Joystick(ev.device_index)
                    self.joy_index = ev.device_index
                    self.use_layout()
                    log(f"controller connected: {self.joy.get_name()} {self.joy.get_guid()}"
                        + (f" (layout {layout_key(self.settings, self.controller_id())})"
                           if layout_key(self.settings, self.controller_id()) else ""))
                    self.prev = {}
                    self.notify()
                except pygame.error as e:
                    log(f"controller not opened: {e}")
            elif ev.type == pygame.JOYDEVICEREMOVED and self.joy is not None:
                if ev.instance_id == self.joy.get_instance_id():
                    log("controller disconnected")
                    self.joy, self.joy_index, self.last_read = None, None, None
                    self.use_layout()
                    self.release_all()
                    self.reset_typing()
                    self.notify()

    def read(self):
        j, inp = self.joy, self.inputs
        pressed = lambda name: read_input(j, inp[name])  # noqa: E731
        axis = lambda name: read_axis(j, inp[name][1])  # noqa: E731
        return {
            "a": pressed("a"), "b": pressed("b"), "l": pressed("l"), "r": pressed("r"),
            "zl": pressed("z"), "start": pressed("start"), "home": pressed("home"),
            "x": axis("stick_x"), "y": axis("stick_y"),
            "c0": pressed("c_up"), "c1": pressed("c_right"), "c2": pressed("c_down"), "c3": pressed("c_left"),
            "up": pressed("dpad_up"), "down": pressed("dpad_down"),
            "left": pressed("dpad_left"), "right": pressed("dpad_right"),
        }

    # ----- main tick
    def tick(self):
        now = time.monotonic()
        if now - self.last_settings_check > 1.0:
            self.last_settings_check = now
            self.load_settings()
        while True:                                   # commands from the tray icon
            try:
                cmd = self.commands.get_nowait()
            except queue.Empty:
                break
            cmd()
        self.handle_events()
        if self.joy is not None:
            s = self.read()
            self.last_read = s
            p = self.prev or {k: (False if isinstance(v, bool) else 0.0) for k, v in s.items()}
            pressed = lambda k: s[k] and not p[k]  # noqa: E731
            released = lambda k: p[k] and not s[k]  # noqa: E731

            if self.capturing:
                pass                                  # the setup wizard is reading the controller
            elif pressed("home"):
                self.set_mode("desktop" if self.mode == "controller" else "controller")
            elif self.mode == "desktop":
                self.desktop(s, p, pressed, released, now)
            self.prev = s
        self.overlay.render(now)

    def desktop(self, s, p, pressed, released, now):
        # Left Z held = typing. Each new hold starts on letters; C-left / C-right step through
        # letters <-> numbers <-> symbols (wrapping round).
        if pressed("zl"):
            self.zl_down_at, self.zl_used = now, False
            self.typing_layer, self.layer_dir = "letters", 0
        if s["zl"]:
            step = (1 if pressed("c1") else 0) - (1 if pressed("c3") else 0)
            if step:
                i = (LAYER_ORDER.index(self.typing_layer) + step) % len(LAYER_ORDER)
                self.typing_layer, self.layer_dir = LAYER_ORDER[i], step
                self.zl_used = True
        layer = self.typing_layer if s["zl"] else None

        # Left Z: a quick tap with nothing else pressed is a right click.
        if released("zl"):
            if self.zl_down_at is not None and not self.zl_used and now - self.zl_down_at < 0.35:
                mouse_flags(MOUSEEVENTF_RIGHTDOWN)
                mouse_flags(MOUSEEVENTF_RIGHTUP)
            self.zl_down_at = None

        if layer != self.layer:
            if self.layer is None:          # entering typing: drop any held mouse button or keys
                self.release_all()
                self.layer_since = now
            self.layer = layer
            self.sel, self.dir = None, None
            if layer is None:
                self.repeater.clear()
                self.shift_once = False
                if math.hypot(s["x"], s["y"]) > self.settings["stick_deadzone"]:
                    self.mouse_wait_center = True

        if layer is None:
            self.mouse_mode(s, p, pressed, released, now)
        else:
            self.typing(s, p, pressed, released, now)

    # ----- desktop mode: mouse and keys
    def mouse_mode(self, s, p, pressed, released, now):
        st = self.settings
        x, y = s["x"], s["y"]
        mag = math.hypot(x, y)
        dz = st["stick_deadzone"]
        if self.mouse_wait_center:
            if mag < dz:
                self.mouse_wait_center = False
        elif mag > dz:
            norm = min(1.0, (mag - dz) / (1.0 - dz))
            speed = st["mouse_max_speed"] * norm ** st["mouse_curve"] / 125.0  # pixels per tick (8 ms)
            self.mouse_acc[0] += x / mag * speed
            self.mouse_acc[1] += y / mag * speed
            dx, dy = int(self.mouse_acc[0]), int(self.mouse_acc[1])
            if dx or dy:
                self.mouse_acc[0] -= dx
                self.mouse_acc[1] -= dy
                mouse_move(dx, dy)
        else:
            self.mouse_acc = [0.0, 0.0]

        if pressed("a"):
            mouse_flags(MOUSEEVENTF_LEFTDOWN)
            self.left_down = True
        if released("a") and self.left_down:
            mouse_flags(MOUSEEVENTF_LEFTUP)
            self.left_down = False
        if pressed("b"):
            key_tap(VK_ESCAPE)
        if pressed("start"):
            key_tap(VK_LWIN)
        if pressed("l"):
            key_tap(VK_LEFT, VK_MENU)
        if pressed("r"):
            key_tap(VK_RIGHT, VK_MENU)

        self.scroll_repeater.interval = 1.0 / max(1, st["scroll_repeat_per_sec"])
        self.scroll_repeater.update("c0", s["c0"], now, lambda: scroll(vertical_notches=1))
        self.scroll_repeater.update("c2", s["c2"], now, lambda: scroll(vertical_notches=-1))
        self.scroll_repeater.update("c1", s["c1"], now, lambda: scroll(horizontal_notches=1))
        self.scroll_repeater.update("c3", s["c3"], now, lambda: scroll(horizontal_notches=-1))
        self.arrows(s, now)

    def arrows(self, s, now):
        for name, vk in (("up", VK_UP), ("down", VK_DOWN), ("left", VK_LEFT), ("right", VK_RIGHT)):
            self.repeater.update(name, s[name], now, lambda vk=vk: key_tap(vk))

    # ----- typing
    def emit(self, ch, shown=None):
        if ch.isalpha() and (self.shift_once != self.caps):
            ch = ch.upper()
        if ch.isalpha():
            self.shift_once = False
        type_char(ch)
        self.flash_char, self.flash_until = shown or ch, time.monotonic() + 0.5

    def typing(self, s, p, pressed, released, now):
        st = self.settings
        chars = LISTS[self.layer]

        if any(pressed(k) for k in ("a", "b", "start", "up", "down", "left", "right", "l", "r",
                                    "c0", "c1", "c2", "c3")):
            self.zl_used = True

        # joystick: tilting starts at the first character, each 1/8 turn steps one character
        x, y = s["x"], s["y"]
        mag = math.hypot(x, y)
        if self.sel is None:
            if mag > st["aim_threshold"]:            # tilted: the wheel starts at the space
                self.dir = direction_of(x, y, None)
                self.sel = home_index(chars)
                self.zl_used = True
        elif mag < st["release_threshold"]:          # let go: back to the space
            self.sel, self.dir = None, None
        elif mag > st["aim_threshold"]:
            new_dir = direction_of(x, y, self.dir, st["step_deadband_degrees"])
            if new_dir != self.dir:
                step = (new_dir - self.dir + 4) % 8 - 4          # -4..3, shortest way round
                if not st.get("invert_wheel", True):
                    step = -step                                 # the wheel turns with the joystick
                self.sel = (self.sel + step) % len(chars)        # inverted: clockwise = forward
                self.dir = new_dir

        # A types the highlighted character (or a space with the stick centered);
        # on the backspace slot it works like B, repeating while held
        on_back = self.sel is not None and chars[self.sel] == BKSP
        if pressed("a"):
            if on_back:
                self.a_back = True
            elif self.sel is not None:
                ch = chars[self.sel]
                self.emit(ch, shown="␣" if ch == " " else None)
            else:
                self.emit(" ", shown="␣")
                self.show_press = (" ", now + 0.25)          # light up the space on the wheel
        if not s["a"] or not on_back:
            self.a_back = False

        def backspace():
            key_tap(VK_BACK)
            if self.sel is None:                             # stick centered: show it on the wheel
                self.show_press = (BKSP, now + 0.25)
        self.repeater.update("back", s["b"] or self.a_back, now, backspace)
        if s["b"] and self.sel is None and self.show_press and self.show_press[0] == BKSP:
            self.show_press = (BKSP, now + 0.25)             # keep it lit while B is held
        if pressed("c2"):
            key_tap(VK_RETURN)
            self.flash_char, self.flash_until = "⏎", now + 0.5
        self.arrows(s, now)
        if pressed("c0") or pressed("start"):            # C-up or Start: Shift / Caps Lock
            self.shift_press(now)

    def shift_press(self, now):
        """One press: Shift for the next letter. Double press: Caps Lock. While Caps Lock is on,
        a single or double press turns it off."""
        if self.caps:
            self.caps, self.shift_once = False, False
            self.caps_off_at = now
            self.last_start_tap = 0.0
        elif now - self.caps_off_at < 0.40:              # second press of a double press: stay off
            self.caps_off_at = 0.0
        elif now - self.last_start_tap < 0.40:           # double press: Caps Lock
            self.caps, self.shift_once = True, False
            self.last_start_tap = 0.0
        else:                                            # single press: Shift for the next letter
            self.shift_once = not self.shift_once
            self.last_start_tap = now

    # ----- overlay state
    def overlay_state(self, now):
        typing_visible = self.layer is not None and (now - self.layer_since > 0.15 or self.zl_used
                                                     or self.layer != "letters")
        active = self.sel is not None
        center = self.sel if active else (home_index(LISTS[self.layer]) if self.layer else 0)
        if not active and self.layer and self.show_press and now < self.show_press[1]:
            # B or A with the stick centered: briefly highlight backspace / space on the wheel
            active, center = True, LISTS[self.layer].index(self.show_press[0])
        layer = self.layer if typing_visible else None
        if layer is None and now < self.preview_until:   # wheel size / opacity being changed
            layer, active, center = "letters", False, home_index(LISTS["letters"])
        return {
            "layer": layer,
            "layer_dir": self.layer_dir,
            "active": active,
            "center": center,
            "upper": self.shift_once != self.caps,
            "caps": self.caps,
            "flash": self.flash_char if now < self.flash_until else "",
            "toast": self.toast_text if now < self.toast_until else "",
        }


# ---------------------------------------------------------------- overlay
# Drawn with Pillow and shown through a per-pixel-alpha window (UpdateLayeredWindow), so the arc can
# fade out smoothly. The window is click-through and can never take focus.
from PIL import Image, ImageChops, ImageDraw, ImageFont  # noqa: E402

gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x20, 0x80, 0x80000, 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.MonitorFromPoint.restype = ctypes.c_void_p
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
MONITOR_DEFAULTTONEAREST = 2
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

FONT_PATH = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeuib.ttf")
COLORS = {
    "panel": (28, 31, 38), "text": (232, 234, 240), "near": (180, 185, 198), "far": (111, 117, 133),
    "dim": (138, 144, 160), "accent": (76, 141, 255), "accent_text": (255, 255, 255), "toast": (42, 46, 56),
}
ARC_SOLID_DEG = 22.0     # the arc is fully visible within this many degrees of the top...
ARC_FADE_DEG = 48.0      # ...and fades out by here (about the top quarter of the circle)
STEP_DEG = 9.5           # spacing between characters on the arc
SLIDE_DISTANCE = 0.85    # how far apart the two wheels are while sliding (in window widths)
SLIDE_SECONDS = 0.30
# Following the mouse: where the arc sits (degrees clockwise from the top) for the pointer near
# each part of the screen. It faces away from nearby edges.
FOLLOW_ANGLES = {
    ("top", "left"): 135, ("top", "mid"): 180, ("top", "right"): 225,
    ("mid", "left"): 90, ("mid", "mid"): 0, ("mid", "right"): 270,
    ("bottom", "left"): 45, ("bottom", "mid"): 0, ("bottom", "right"): 315,
}     # changing wheels: the old one slides off sideways, the new one slides in


def arc_alpha(deg):
    d = abs(deg)
    if d <= ARC_SOLID_DEG:
        return 1.0
    if d >= ARC_FADE_DEG:
        return 0.0
    t = (d - ARC_SOLID_DEG) / (ARC_FADE_DEG - ARC_SOLID_DEG)
    return (1.0 - t) ** 1.6


class Overlay:
    def __init__(self, app):
        self.app = app
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Controller Keyboard overlay")
        self.root.overrideredirect(True)

        self.dpi_scale = self.root.winfo_fpixels("1i") / 96.0
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        self.hbmp = None
        self.setup_size(float(app.settings.get("overlay_scale", 1.0)))

        self.root.update_idletasks()
        self.hwnd = user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE,
                                 ex | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE)

        fg = user32.GetForegroundWindow()
        self.push(self.blank)
        self.root.deiconify()
        self.root.update()
        self.push(self.blank)
        if fg:
            user32.SetForegroundWindow(fg)  # showing the (empty) window must not take focus
        self.root.attributes("-topmost", True)
        self.visible = False
        self.last_state = None
        self.last_topmost = 0.0
        self.pos = 0.0          # where the characters are drawn (animates toward the selected index)
        self.anim = None        # (start pos, distance, start time, duration) while sliding or spinning
        self.slide = None       # (old layer, old pos, direction, start time) while changing wheels
        self.last_tick = time.monotonic()

    def setup_size(self, scale):
        """Size the wheel for a scale (the overlay_scale setting); can be called again to resize."""
        self.scale = scale
        self.k = k = self.dpi_scale * scale
        self.R = 230 * k                       # radius of the arc the characters ride on
        self.band = 46 * k                     # thickness of the arc
        # distances from the circle's center, along the line through the middle of the arc
        self.d_label = self.R - self.band / 2 - 22 * k     # the wheel's name (abc / 123 / #+=)
        self.d_mark = self.d_label - 27 * k                # Shift / CAPS / the last typed character
        self.d_toast = self.d_label - 101 * k              # mode messages
        self.d_pointer = self.d_label - 46 * k             # the mouse pointer, when the wheel follows it

        # load every font size now, so the first animation isn't slow
        self.fonts, self.bases, self.geoms = {}, {}, {}
        for size in range(int(8 * k), int(32 * k) + 2):
            try:
                self.fonts[size] = ImageFont.truetype(FONT_PATH, size)
            except OSError:
                break
        self.theta, self.dib_size = None, None
        self.set_geometry(0)
        work = wintypes.RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(work), 0)  # SPI_GETWORKAREA
        self.home = (work.left + (work.right - work.left - self.w) // 2, work.bottom - self.h - int(24 * k))
        self.x, self.y = self.home             # where the wheel sits unless it follows the mouse
        self.root.geometry(f"{self.w}x{self.h}+{self.x}+{self.y}")
        self.base(True)
        self.base(False)
        self.last_state = None                 # redraw at the new size

    # ----- where the arc sits: theta = the middle of the arc, in degrees clockwise from the top
    def geometry(self, theta):
        """The window size for an arc centered at theta, and where the circle's center is in it."""
        g = self.geoms.get(theta)
        if g is not None:
            return g
        k, t = self.k, math.radians(theta)
        ux, uy = math.sin(t), -math.cos(t)
        pts = []
        for i in range(-24, 25):                         # the band's edges, every 2 degrees
            a = t + math.radians(i * ARC_FADE_DEG / 24)
            for r in (self.R - self.band / 2, self.R + self.band / 2):
                pts.append((r * math.sin(a), -r * math.cos(a)))

        def box(d, hw, hh):
            pts.extend([(d * ux - hw, d * uy - hh), (d * ux + hw, d * uy + hh)])
        box(self.R, 26 * k, 26 * k)                      # the highlight
        box(self.d_label, 30 * k, 12 * k)
        box(self.d_mark, 24 * k, 18 * k)
        box(self.d_toast, 110 * k, 22 * k)
        m = 6 * k
        minx, maxx = min(p[0] for p in pts) - m, max(p[0] for p in pts) + m
        miny, maxy = min(p[1] for p in pts) - m, max(p[1] for p in pts) + m
        w, h = int(math.ceil(maxx - minx)), int(math.ceil(maxy - miny))
        # sliding wheels fade out over the outer part of the window instead of being cut off at its edge
        fade = max(1, int(w * 0.18))
        ramp = Image.linear_gradient("L").rotate(90, expand=True).resize((fade, 1))   # 0 -> 255
        mask = Image.new("L", (w, 1), 255)
        mask.paste(ramp, (0, 0))
        mask.paste(ramp.transpose(Image.FLIP_LEFT_RIGHT), (w - fade, 0))
        g = {"w": w, "h": h, "cx": -minx, "cy": -miny, "edge_mask": mask.resize((w, h)),
             "blank": Image.new("RGBA", (w, h), (0, 0, 0, 0))}
        self.geoms[theta] = g
        return g

    def set_geometry(self, theta):
        if theta == self.theta:
            return
        g = self.geometry(theta)
        self.theta = theta
        self.ux, self.uy = math.sin(math.radians(theta)), -math.cos(math.radians(theta))
        self.w, self.h, self.cx, self.cy = g["w"], g["h"], g["cx"], g["cy"]
        self.edge_mask, self.blank = g["edge_mask"], g["blank"]
        if self.dib_size != (self.w, self.h):
            # a 32-bit bitmap the frames are copied into, then handed to the window
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = self.w, -self.h   # top-down
            bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
            bits = ctypes.c_void_p()
            hbmp = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            gdi32.SelectObject(self.mem_dc, hbmp)
            if self.hbmp:
                gdi32.DeleteObject(self.hbmp)
            self.hbmp, self.bits, self.dib_size = hbmp, bits, (self.w, self.h)
        self.last_state = None

    def point(self, d):
        """The spot d from the circle's center, toward the middle of the arc."""
        return self.cx + d * self.ux, self.cy + d * self.uy

    # ----- drawing helpers
    def font(self, px):
        size = max(6, int(round(px * self.k)))
        f = self.fonts.get(size)
        if f is None:
            try:
                f = ImageFont.truetype(FONT_PATH, size)
            except OSError:
                f = ImageFont.load_default()
            self.fonts[size] = f
        return f

    def space_mark(self, d, x, y, size, fill):
        """A drawn space symbol (a short bracket, like the one on keyboard legends)."""
        px = size * self.k
        half, rise, w = px * 0.42, px * 0.22, max(2, int(px * 0.11))
        base_y = y + px * 0.14
        d.line([(x - half, base_y - rise), (x - half, base_y), (x + half, base_y), (x + half, base_y - rise)],
               fill=fill, width=w, joint="curve")

    def backspace_mark(self, d, x, y, size, fill):
        """A drawn backspace symbol: a left-pointing key outline with an x in it."""
        px = size * self.k
        w, h, tip = px * 0.62, px * 0.36, px * 0.3
        lw = max(2, int(px * 0.09))
        left, right, top, bot = x - w / 2 - tip / 3, x + w / 2 + tip / 3, y - h, y + h
        d.line([(left + tip, top), (right, top), (right, bot), (left + tip, bot), (left, y), (left + tip, top)],
               fill=fill, width=lw, joint="curve")
        xc, r = (left + tip + right) / 2, h * 0.45
        d.line([(xc - r, y - r), (xc + r, y + r)], fill=fill, width=lw)
        d.line([(xc - r, y + r), (xc + r, y - r)], fill=fill, width=lw)

    def shift_mark(self, d, x, y, size, fill):
        """A drawn Shift symbol: an outlined up arrow."""
        px = size * self.k
        hw, sw, h = px * 0.42, px * 0.18, px * 0.5        # head half-width, stem half-width, half-height
        lw = max(2, int(px * 0.09))
        top, mid, bot = y - h, y, y + h
        d.line([(x, top), (x + hw, mid), (x + sw, mid), (x + sw, bot), (x - sw, bot), (x - sw, mid),
                (x - hw, mid), (x, top)], fill=fill, width=lw, joint="curve")

    def near_cursor(self):
        """Where to put the wheel next to the mouse pointer, as (x, y, theta). The pointer sits just
        inside the arc, past the wheel's label and Shift mark. Away from the screen's edges the arc is
        above the pointer; near an edge or a corner it turns to face away from it (top-left corner:
        the arc's bottom-right part, top edge: its bottom, and so on)."""
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return self.home + (0,)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST), ctypes.byref(mi)):
            return self.home + (0,)
        wa, k = mi.rcWork, self.k
        reach = self.R + self.band / 2 - self.d_pointer + 6 * k     # from the pointer out to the arc
        side = self.geometry(0)["w"] / 2                             # half the arc's width
        row = "top" if pt.y - wa.top < reach else "bottom" if wa.bottom - pt.y < reach else "mid"
        col = "left" if pt.x - wa.left < side else "right" if wa.right - pt.x < side else "mid"
        theta = FOLLOW_ANGLES[(row, col)]
        g = self.geometry(theta)
        t = math.radians(theta)
        ux, uy = math.sin(t), -math.cos(t)
        # room for the pointer's arrow, which hangs down and to the right of where it points
        d = self.d_pointer - 26 * k * max(0.0, uy) - 14 * k * max(0.0, ux)
        # the window's far side (empty, past the pointer) may hang off the screen; that's fine
        x = int(round(pt.x - d * ux - g["cx"]))
        y = int(round(pt.y - d * uy - g["cy"]))
        return x, y, theta

    def arc_center(self):
        return self.cx, self.cy

    def base(self, highlight):
        """The faded arc (and the highlight in its middle), drawn once at 3x and scaled down for smooth edges."""
        key = (self.theta, highlight)
        img = self.bases.get(key)
        if img is not None:
            return img
        if self.theta != 0 and (0, highlight) in self.bases:
            # the arc at the top, turned around the circle's center (much faster than drawing it)
            g0, t = self.geometry(0), math.radians(self.theta)
            c, sn, cx, cy = math.cos(t), math.sin(t), self.cx, self.cy
            data = (c, sn, -c * cx - sn * cy + g0["cx"], -sn, c, sn * cx - c * cy + g0["cy"])
            img = self.bases[(0, highlight)].transform((self.w, self.h), Image.AFFINE, data,
                                                       resample=Image.BICUBIC)
            self.bases[key] = img
            return img
        s = 3
        W, H = self.w * s, self.h * s
        cx, cy = self.cx * s, self.cy * s
        R_out, R_in = (self.R + self.band / 2) * s, (self.R - self.band / 2) * s
        big = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        # the band, as thin wedges whose opacity follows arc_alpha, with the inside cut out
        deg = -ARC_FADE_DEG
        while deg < ARC_FADE_DEG:
            a = arc_alpha(deg + 0.25)
            if a > 0:
                # PIL angles: 0 = 3 o'clock, clockwise; the top is -90
                start = -90 + self.theta + deg
                d.pieslice((cx - R_out, cy - R_out, cx + R_out, cy + R_out), start, start + 0.6,
                           fill=COLORS["panel"] + (int(235 * a),))
            deg += 0.5
        d.ellipse((cx - R_in, cy - R_in, cx + R_in, cy + R_in), fill=(0, 0, 0, 0))
        if highlight:
            r = 25 * self.k * s
            tx, ty = cx + self.R * self.ux * s, cy + self.R * self.uy * s
            d.ellipse((tx - r, ty - r, tx + r, ty + r), fill=COLORS["accent"] + (255,))
        img = big.resize((self.w, self.h), Image.LANCZOS)
        self.bases[key] = img
        return img

    def push(self, img):
        """Hand a frame to the window (premultiplied BGRA, as Windows wants it)."""
        r, g, b, a = img.split()
        r, g, b = ImageChops.multiply(r, a), ImageChops.multiply(g, a), ImageChops.multiply(b, a)
        data = Image.merge("RGBA", (b, g, r, a)).tobytes()
        ctypes.memmove(self.bits, data, len(data))
        blend = BLENDFUNCTION(0, 0, int(255 * float(self.app.settings.get("overlay_opacity", 0.95))), AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, self.screen_dc, ctypes.byref(wintypes.POINT(self.x, self.y)),
                                   ctypes.byref(wintypes.SIZE(self.w, self.h)), self.mem_dc,
                                   ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)

    # ----- per tick
    def render(self, now):
        scale = min(3.0, max(0.4, float(self.app.settings.get("overlay_scale", 1.0))))
        if abs(scale - self.scale) > 1e-6:
            self.setup_size(scale)
        opacity = self.app.settings.get("overlay_opacity", 0.9)
        if opacity != getattr(self, "opacity", None):
            self.opacity, self.last_state = opacity, None      # push the next frame at the new opacity
        st = self.app.overlay_state(now)
        dt, self.last_tick = now - self.last_tick, now
        prev = self.last_state
        was_typing, typing = bool(prev and prev["layer"]), bool(st["layer"])
        if typing and not was_typing:                  # the wheel appears: next to the pointer, maybe
            follow = self.app.settings.get("wheel_follows_mouse", False)
            x, y, theta = self.near_cursor() if follow else self.home + (0,)
            self.set_geometry(theta)
            self.x, self.y = x, y
        elif was_typing and not typing:                # mode messages show in the usual place
            self.set_geometry(0)
            self.x, self.y = self.home
        animating = False
        if st["layer"]:
            n = len(LISTS[st["layer"]])
            target = st["center"]
            if prev is None or prev["layer"] != st["layer"]:
                if prev and prev["layer"] and st["layer_dir"]:
                    # changed wheels with C-left / C-right. Inverted: the new one slides in from that
                    # side; normal: the old one slides off that way
                    flip = -1 if self.app.settings.get("invert_wheel", True) else 1
                    self.slide = (prev["layer"], self.pos, flip * st["layer_dir"], now)
                else:
                    self.slide = None
                self.pos, self.anim = float(target), None
            elif target != prev["center"] or st["active"] != prev["active"]:
                # a step (quick slide), or the stick let go (spin back to the space)
                delta = (target - self.pos + n / 2) % n - n / 2   # shortest way round (the list wraps)
                if abs(delta + n / 2) < 1e-6:
                    delta = n / 2                                 # exactly opposite: spin forward
                duration = 0.09 if abs(delta) <= 2.5 else 0.22
                self.anim = (self.pos, delta, now, duration)
            if self.anim:
                start, delta, t0, duration = self.anim
                u = min(1.0, (now - t0) / duration)
                self.pos = (start + delta * (1.0 - (1.0 - u) ** 3)) % n   # fast start, eases into place
                animating = True
                if u >= 1.0:
                    self.pos, self.anim = float(target), None
            if self.slide:
                if now - self.slide[3] >= SLIDE_SECONDS:
                    self.slide = None
                animating = True                                  # one more frame after it ends
        else:
            self.slide = None
        show = bool(st["layer"] or st["toast"])
        if st != prev or animating:
            self.last_state = st
            self.push(self.draw(st) if show else self.blank)
        if show and now - self.last_topmost > 2.0:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
            self.last_topmost = now

    def draw(self, st):
        k = self.k
        cx, cy = self.arc_center()
        layer = st["layer"]
        if not layer:                                          # just a mode message
            img = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            f = self.font(16)
            tw = d.textlength(st["toast"], font=f)
            tx, ty = self.point(self.d_toast)
            r = 20 * k
            d.rounded_rectangle((tx - tw / 2 - r, ty - r, tx + tw / 2 + r, ty + r), radius=r,
                                fill=COLORS["toast"] + (240,))
            d.text((tx, ty), st["toast"], font=f, fill=COLORS["text"] + (255,), anchor="mm")
            return img

        upper, active = st["upper"], st["active"]
        if self.slide:
            # the old wheel slides sideways toward the C button pressed, and the new one slides in
            # behind it from the other side, so the two move together like cards
            old_layer, old_pos, direction, t0 = self.slide
            u = min(1.0, (time.monotonic() - t0) / SLIDE_SECONDS)
            e = 1.0 - (1.0 - u) ** 3                           # fast start, eases into place
            dist = SLIDE_DISTANCE * self.w
            dx = int(round(direction * e * dist))
            img = self.blank.copy()
            self.paste_shifted(img, self.wheel(old_layer, old_pos, False, upper), dx)
            self.paste_shifted(img, self.wheel(layer, self.pos, active, upper),
                               int(round(dx - direction * dist)))
            a = ImageChops.multiply(img.getchannel("A"), self.edge_mask)   # fade out at the window's sides
            img.putalpha(a)
        else:
            img = self.wheel(layer, self.pos, active, upper)

        # under the wheel's label (these stay put while wheels slide): Shift / Caps, the last
        # typed character, mode messages
        top_img = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(top_img)
        mx, my = self.point(self.d_mark)
        if st["flash"] == "␣":
            self.space_mark(d, mx, my, 22, COLORS["accent"] + (255,))
        elif st["flash"]:
            d.text((mx, my), st["flash"], font=self.font(22), fill=COLORS["accent"] + (255,), anchor="mm")
        elif st["caps"]:
            d.text((mx, my), "CAPS", font=self.font(14), fill=COLORS["accent"] + (255,), anchor="mm")
        elif upper:
            self.shift_mark(d, mx, my, 18, COLORS["accent"] + (255,))
        if st["toast"]:
            d.text(self.point(self.d_toast), st["toast"], font=self.font(12), fill=COLORS["text"] + (230,), anchor="mm")
        return Image.alpha_composite(img, top_img)

    def wheel(self, layer, pos, active, upper):
        """One whole wheel: the arc, its characters around pos, and its label (abc / 123 / #+=)."""
        layer_img = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer_img)
        self.draw_chars(d, LISTS[layer], pos, upper, active)
        d.text(self.point(self.d_label), LAYER_LABEL[layer], font=self.font(13), fill=COLORS["dim"] + (230,), anchor="mm")
        return Image.alpha_composite(self.base(active), layer_img)

    @staticmethod
    def paste_shifted(dst, src, dx):
        """Composite src onto dst moved dx pixels sideways (the part that falls outside is dropped)."""
        w = src.width
        if abs(dx) >= w:
            return
        if dx >= 0:
            dst.alpha_composite(src.crop((0, 0, w - dx, src.height)), (dx, 0))
        else:
            dst.alpha_composite(src.crop((-dx, 0, w, src.height)), (0, 0))

    def draw_chars(self, d, chars, pos, upper, active):
        """One wheel's characters around pos."""
        cx, cy = self.arc_center()
        n = len(chars)
        pos = pos % n
        base = math.floor(pos)
        for i in range(base - 6, base + 7):
            off = i - pos                  # characters after the selection sit clockwise from it
            deg = off * STEP_DEG
            a = arc_alpha(deg)
            if a <= 0.02:
                continue
            dist = abs(off)
            ang = math.radians(self.theta + deg)       # characters stay upright wherever the arc is
            x, y = cx + self.R * math.sin(ang), cy - self.R * math.cos(ang)
            size = 30 - 10 * min(dist, 1) - 1.5 * min(max(dist - 1, 0), 3)
            if not active:
                color = COLORS["near"] if dist < 0.5 else COLORS["far"]
            elif dist < 0.5:
                color = COLORS["accent_text"]
            elif dist < 1.5:
                color = COLORS["near"]
            else:
                color = COLORS["far"]
            ch = chars[i % n]
            fill = color + (int(255 * a),)
            if ch == " ":
                self.space_mark(d, x, y, size, fill)
                continue
            if ch == BKSP:
                self.backspace_mark(d, x, y, size, fill)
                continue
            if upper and ch.isalpha():
                ch = ch.upper()
            d.text((x, y), ch, font=self.font(size), fill=fill, anchor="mm")



# ---------------------------------------------------------------- entry
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
EVENT_MODIFY_STATE = 0x0002
OPEN_SETTINGS_EVENT = "Local\\ControllerKeyboardOpenSettings"


def main():
    mutex = kernel32.CreateMutexW(None, False, "Local\\ControllerKeyboard8BitDo")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: already running, so open its settings
        ev = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, OPEN_SETTINGS_EVENT)
        if ev:
            kernel32.SetEvent(ev)
        return
    open_settings_event = kernel32.CreateEventW(None, False, False, OPEN_SETTINGS_EVENT)  # auto-reset
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    first_run = not os.path.exists(SETTINGS_PATH)
    app = App()

    import gui  # noqa: E402  (next to this file)
    ui = gui.Gui(app)
    if first_run or "--settings" in sys.argv:
        ui.open_settings("controller" if "controller" in sys.argv else None)

    def loop():
        try:
            if open_settings_event and kernel32.WaitForSingleObject(open_settings_event, 0) == 0:
                ui.open_settings()                 # the app was started again
            app.tick()
        except Exception as e:  # keep running; log what went wrong
            log(f"error: {e!r}")
            app.release_all()
        if not ui.quitting:
            app.overlay.root.after(8, loop)

    app.overlay.root.after(8, loop)
    try:
        app.overlay.root.mainloop()
    finally:
        app.release_all()
        ui.stop()
        log("stopped")
        del mutex


if __name__ == "__main__":
    main()
