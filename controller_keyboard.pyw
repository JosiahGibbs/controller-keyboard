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
    "stick_click": 13,                             # while typing: letters -> numbers -> symbols
    "stick_x": 0, "stick_y": 1,
    "c_up": "a4-", "c_right": "a3+", "c_down": "a4+", "c_left": "a3-",    # scroll; C-down = Enter
    "dpad_up": "h0:up", "dpad_down": "h0:down", "dpad_left": "h0:left", "dpad_right": "h0:right",
}
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
    "controller": DEFAULT_CONTROLLER,
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

        self.overlay = Overlay(self)
        self.last_settings_check = 0.0
        log("started")

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
            inputs = parse_controller(settings["controller"])    # raises on a bad entry: keep the old ones
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
        self.typing_layer = "letters"   # cycled with the joystick button while typing
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
                    log(f"controller connected: {self.joy.get_name()} {self.joy.get_guid()}")
                    self.prev = {}
                except pygame.error as e:
                    log(f"controller not opened: {e}")
            elif ev.type == pygame.JOYDEVICEREMOVED and self.joy is not None:
                if ev.instance_id == self.joy.get_instance_id():
                    log("controller disconnected")
                    self.joy = None
                    self.release_all()
                    self.reset_typing()

    def read(self):
        j, inp = self.joy, self.inputs
        pressed = lambda name: read_input(j, inp[name])  # noqa: E731
        axis = lambda name: read_axis(j, inp[name][1])  # noqa: E731
        return {
            "a": pressed("a"), "b": pressed("b"), "l": pressed("l"), "r": pressed("r"),
            "zl": pressed("z"), "start": pressed("start"), "home": pressed("home"),
            "stick": pressed("stick_click"),
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
        self.handle_events()
        if self.joy is not None:
            s = self.read()
            p = self.prev or {k: (False if isinstance(v, bool) else 0.0) for k, v in s.items()}
            pressed = lambda k: s[k] and not p[k]  # noqa: E731
            released = lambda k: p[k] and not s[k]  # noqa: E731

            if pressed("home"):
                self.release_all()
                self.reset_typing()
                self.mode = "desktop" if self.mode == "controller" else "controller"
                self.toast("Desktop mode" if self.mode == "desktop" else "Controller mode")
                log(f"mode: {self.mode}")
            elif self.mode == "desktop":
                self.desktop(s, p, pressed, released, now)
            self.prev = s
        self.overlay.render(now)

    def desktop(self, s, p, pressed, released, now):
        # Left Z held = typing. Each new hold starts on letters; the joystick button cycles
        # letters -> numbers -> symbols.
        if pressed("zl"):
            self.zl_down_at, self.zl_used = now, False
            self.typing_layer = "letters"
        if s["zl"] and pressed("stick"):
            self.typing_layer = LAYER_ORDER[(LAYER_ORDER.index(self.typing_layer) + 1) % len(LAYER_ORDER)]
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
                self.sel = (self.sel + step) % len(chars)        # clockwise = forward
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
        if pressed("start"):
            if self.caps:                                # Caps on: a single or double press turns it off
                self.caps, self.shift_once = False, False
                self.caps_off_at = now
                self.last_start_tap = 0.0
            elif now - self.caps_off_at < 0.40:          # second press of a double press: stay off
                self.caps_off_at = 0.0
            elif now - self.last_start_tap < 0.40:       # double tap: Caps Lock
                self.caps, self.shift_once = True, False
                self.last_start_tap = 0.0
            else:                                        # single tap: Shift for the next letter
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
        return {
            "layer": self.layer if typing_visible else None,
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
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]

FONT_PATH = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeuib.ttf")
COLORS = {
    "panel": (28, 31, 38), "text": (232, 234, 240), "near": (180, 185, 198), "far": (111, 117, 133),
    "dim": (138, 144, 160), "accent": (76, 141, 255), "accent_text": (255, 255, 255), "toast": (42, 46, 56),
}
ARC_SOLID_DEG = 22.0     # the arc is fully visible within this many degrees of the top...
ARC_FADE_DEG = 48.0      # ...and fades out by here (about the top quarter of the circle)
STEP_DEG = 9.5           # spacing between characters on the arc


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
        self.root.title("Controller Keyboard")
        self.root.overrideredirect(True)

        dpi_scale = self.root.winfo_fpixels("1i") / 96.0
        self.k = k = dpi_scale * float(app.settings.get("overlay_scale", 1.0))
        self.R = 230 * k                       # radius of the arc the characters ride on
        self.band = 46 * k                     # thickness of the arc
        self.top = 34 * k                      # from the window top to the arc's center line at the top
        self.w = int(2 * (self.R + self.band) * math.sin(math.radians(ARC_FADE_DEG)) + 40 * k)
        self.h = int(self.R * (1 - math.cos(math.radians(ARC_FADE_DEG))) + self.top + self.band + 40 * k)
        work = wintypes.RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(work), 0)  # SPI_GETWORKAREA
        self.x = work.left + (work.right - work.left - self.w) // 2
        self.y = work.bottom - self.h - int(24 * k)
        self.root.geometry(f"{self.w}x{self.h}+{self.x}+{self.y}")

        self.root.update_idletasks()
        self.hwnd = user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE,
                                 ex | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE)

        # a 32-bit bitmap the frames are copied into, then handed to the window
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = self.w, -self.h   # top-down
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        self.bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        gdi32.SelectObject(self.mem_dc, self.hbmp)
        self.fonts = {}
        self.bases = {}
        # load every font size and draw the arc now, so the first animation isn't slow
        for size in range(int(8 * k), int(32 * k) + 2):
            try:
                self.fonts[size] = ImageFont.truetype(FONT_PATH, size)
            except OSError:
                break
        self.base(True)
        self.base(False)
        self.blank = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))

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
        self.last_tick = time.monotonic()

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

    def arc_center(self):
        return self.w / 2, self.top + self.R

    def base(self, highlight):
        """The faded arc (and the highlight at the top), drawn once at 3x and scaled down for smooth edges."""
        img = self.bases.get(highlight)
        if img is not None:
            return img
        s = 3
        W, H = self.w * s, self.h * s
        cx, cy = self.w / 2 * s, (self.top + self.R) * s
        R_out, R_in = (self.R + self.band / 2) * s, (self.R - self.band / 2) * s
        big = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        # the band, as thin wedges whose opacity follows arc_alpha, with the inside cut out
        deg = -ARC_FADE_DEG
        while deg < ARC_FADE_DEG:
            a = arc_alpha(deg + 0.25)
            if a > 0:
                # PIL angles: 0 = 3 o'clock, clockwise; the top is -90
                d.pieslice((cx - R_out, cy - R_out, cx + R_out, cy + R_out), -90 + deg, -90 + deg + 0.6,
                           fill=COLORS["panel"] + (int(235 * a),))
            deg += 0.5
        d.ellipse((cx - R_in, cy - R_in, cx + R_in, cy + R_in), fill=(0, 0, 0, 0))
        if highlight:
            r = 25 * self.k * s
            tx, ty = cx, cy - self.R * s
            d.ellipse((tx - r, ty - r, tx + r, ty + r), fill=COLORS["accent"] + (255,))
        img = big.resize((self.w, self.h), Image.LANCZOS)
        self.bases[highlight] = img
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
        st = self.app.overlay_state(now)
        dt, self.last_tick = now - self.last_tick, now
        prev = self.last_state
        animating = False
        if st["layer"]:
            n = len(LISTS[st["layer"]])
            target = st["center"]
            if prev is None or prev["layer"] != st["layer"]:
                self.pos, self.anim = float(target), None          # new wheel: no animation
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
            ty, r = self.h - 40 * k, 20 * k
            d.rounded_rectangle((cx - tw / 2 - r, ty - r, cx + tw / 2 + r, ty + r), radius=r,
                                fill=COLORS["toast"] + (240,))
            d.text((cx, ty), st["toast"], font=f, fill=COLORS["text"] + (255,), anchor="mm")
            return img

        chars = LISTS[layer]
        n = len(chars)
        upper, active = st["upper"], st["active"]
        img = self.base(active).copy()
        layer_img = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer_img)
        pos = self.pos % n
        base = math.floor(pos)
        for i in range(base - 6, base + 7):
            off = i - pos                                      # characters after the selection sit right
            deg = off * STEP_DEG
            a = arc_alpha(deg)
            if a <= 0.02:
                continue
            dist = abs(off)
            ang = math.radians(deg)
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

        # under the top of the arc: layer, Shift / Caps, the last typed character, mode messages
        below = self.top + self.band / 2 + 22 * k
        d.text((cx, below), LAYER_LABEL[layer], font=self.font(13), fill=COLORS["dim"] + (230,), anchor="mm")
        shift = "CAPS" if st["caps"] else ("\u21e7" if upper else "")
        if st["flash"] == "␣":
            self.space_mark(d, cx, below + 28 * k, 22, COLORS["accent"] + (255,))
        elif st["flash"]:
            d.text((cx, below + 28 * k), st["flash"], font=self.font(22), fill=COLORS["accent"] + (255,), anchor="mm")
        elif shift:
            d.text((cx, below + 26 * k), shift, font=self.font(14), fill=COLORS["accent"] + (255,), anchor="mm")
        if st["toast"]:
            d.text((cx, self.h - 16 * k), st["toast"], font=self.font(12), fill=COLORS["text"] + (230,), anchor="mm")
        return Image.alpha_composite(img, layer_img)


# ---------------------------------------------------------------- entry
def main():
    mutex = kernel32.CreateMutexW(None, False, "Local\\ControllerKeyboard8BitDo")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    app = App()

    def loop():
        try:
            app.tick()
        except Exception as e:  # keep running; log what went wrong
            log(f"error: {e!r}")
            app.release_all()
        app.overlay.root.after(8, loop)

    app.overlay.root.after(8, loop)
    try:
        app.overlay.root.mainloop()
    finally:
        app.release_all()
        log("stopped")
        del mutex


if __name__ == "__main__":
    main()
