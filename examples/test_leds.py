"""Quick LED smoke test for ChronosLEDs.

Run from the repo root:
    python examples/test_leds.py
"""

import colorsys
import math
from chronos import ChronosLEDs

c = ChronosLEDs()
if not c.connected:
    raise SystemExit("Chronos not found")

c.start()
c.init_leds()
print(f"Firmware descriptor: {c.firmware_descriptor.hex()}")
print(f"Calibration: {c.calibration}")

print("All LEDs red for 1 s...")
c.set_leds(colors=(255, 0, 0), duration=1.0)

print("All LEDs green for 1 s...")
c.set_leds(colors=(0, 255, 0), duration=1.0)

print("All LEDs blue for 1 s...")
c.set_leds(colors=(0, 0, 255), duration=1.0)

print("Only LED 0 (leftmost) white for 2 s...")
c.set_leds(colors=(255, 255, 255), duration=1.0, leds=(0,))

print("Only LED 4 (rightmost) dim white for 2 s...")
c.set_leds(colors=(10, 10, 10), duration=1.0, leds=(4,))

FALLOFF = 3.0  # Gaussian width (higher = narrower beam)
STEP = 0.001  # peak advances this many LEDs per frame (speed)
FRAME_S = 0.0  # extra sleep per frame; USB overhead (~10 ms) is the real floor


def wave_frame(peak, hue):
    r, g, b = [round(x * 255) for x in colorsys.hsv_to_rgb(hue % 1.0, 1.0, 1.0)]
    return [
        tuple(round(ch * math.exp(-FALLOFF * (i - peak) ** 2)) for ch in (r, g, b))
        for i in range(5)
    ]


print("Rainbow wave left↔right, 3 passes...")
hue = 0.0
for _ in range(5):
    for start, end, sign in [(0.0, 4.0, 1), (4.0, 0.0, -1)]:
        peak = start
        while sign * peak <= sign * end:
            c.leds_on(colors=wave_frame(peak, hue))
            peak += sign * STEP
            hue += STEP / 7  # full rainbow over ~7 LED-widths of travel
c.leds_off()

c.stop()
print("Done.")
