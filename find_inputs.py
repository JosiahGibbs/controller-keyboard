"""Press buttons, move sticks and triggers: this prints the settings.json entry for each one.

Run it with the controller connected:  python find_inputs.py   (Ctrl+C to stop)
Then put the entries in the "controller" section of settings.json, for example  "z": "a4+".
"""

import os
import time

os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import pygame  # noqa: E402


def main():
    pygame.display.init()
    pygame.joystick.init()
    print("Waiting for a controller... (Ctrl+C to stop)")
    joy, rest, shown = None, None, set()
    try:
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.JOYDEVICEADDED and joy is None:
                    joy = pygame.joystick.Joystick(ev.device_index)
                    print(f"\nConnected: {joy.get_name()}  (GUID {joy.get_guid()})")
                    print(f"{joy.get_numbuttons()} buttons, {joy.get_numaxes()} axes, {joy.get_numhats()} hats")
                    print("Press each control. Leave the sticks and triggers alone for a second first.\n")
                    time.sleep(0.5)
                    pygame.event.pump()
                    rest = [joy.get_axis(i) for i in range(joy.get_numaxes())]
                elif ev.type == pygame.JOYDEVICEREMOVED and joy is not None:
                    print("Controller disconnected.")
                    joy = None
            if joy is not None:
                now = set()
                for i in range(joy.get_numbuttons()):
                    if joy.get_button(i):
                        now.add((f"button {i}", str(i)))
                for i in range(joy.get_numaxes()):
                    v = joy.get_axis(i)
                    if abs(v - rest[i]) > 0.5 and abs(v) > 0.5:
                        sign = "+" if v > 0 else "-"
                        now.add((f"axis {i} {sign}  (rests at {rest[i]:+.1f})", f'"a{i}{sign}"'))
                for i in range(joy.get_numhats()):
                    hx, hy = joy.get_hat(i)
                    for name, on in (("up", hy > 0), ("down", hy < 0), ("left", hx < 0), ("right", hx > 0)):
                        if on:
                            now.add((f"hat {i} {name}", f'"h{i}:{name}"'))
                for what, entry in sorted(now - shown):
                    print(f"  {what:34s} ->  {entry}")
                shown = now
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
