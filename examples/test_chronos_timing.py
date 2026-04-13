"""Chronos PPM drift stability test.

Accumulates (hw_time, pc_time) pairs from baseline and computes
the running PPM estimate. Saves raw data to a timestamped CSV log.
"""

from datetime import datetime
from pathlib import Path

from psychopy import core
from chronos import Chronos

UINT32_MOD = 2**32

print("=== Chronos PPM Drift Stability Test ===")
print("Mash the button. Ctrl+C to exit.\n")

clock = core.Clock()
chronos = Chronos(clock=clock.getTime)
if not chronos.connected:
    print("FATAL: Chronos not found.")
    core.quit()
chronos.start()

log_dir = Path("data")
log_dir.mkdir(exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_path = log_dir / f"chronos_drift_{timestamp}.csv"
log_file = open(log_path, "w")
log_file.write("press,pc_time,hw_timestamp_us,elapsed_pc_s,elapsed_hw_s,drift_ms,ppm\n")
print(f"Logging to: {log_path}\n")

baseline_hw_us = None
baseline_pc_s = None
press_count = 0
elapsed_pc_sec = 0.0
drift_ms = 0.0
ppm = 0.0

try:
    while True:
        events = chronos.get_button_events()
        for evt in events:
            if not evt.is_press:
                continue
            pc_time = evt.timestamp
            hw_us = evt.hw_timestamp_us

            if baseline_hw_us is None:
                baseline_hw_us = hw_us
                baseline_pc_s = pc_time
                log_file.write(f"0,{pc_time:.6f},{hw_us},0.0,0.0,0.0,0.0\n")
                print(f"Baseline set. Start pressing!\n")
                continue

            press_count += 1

            elapsed_hw_us = (hw_us - baseline_hw_us) % UINT32_MOD
            elapsed_hw_sec = elapsed_hw_us / 1_000_000.0
            elapsed_pc_sec = pc_time - baseline_pc_s

            if elapsed_pc_sec > 0.1:
                ppm = ((elapsed_hw_sec / elapsed_pc_sec) - 1.0) * 1_000_000.0
                drift_ms = (elapsed_hw_sec - elapsed_pc_sec) * 1000.0
            else:
                ppm = 0.0
                drift_ms = 0.0

            log_file.write(f"{press_count},{pc_time:.6f},{hw_us},"
                           f"{elapsed_pc_sec:.6f},{elapsed_hw_sec:.6f},"
                           f"{drift_ms:.3f},{ppm:.1f}\n")
            log_file.flush()

            print(f"Press #{press_count:>5d}  |  "
                  f"elapsed: {elapsed_pc_sec:>7.1f}s  |  "
                  f"total drift: {drift_ms:>+8.3f} ms  |  "
                  f"PPM: {ppm:>+8.1f}")

        core.wait(0.001)

except KeyboardInterrupt:
    pass
finally:
    chronos.stop()
    log_file.close()
    if press_count > 0 and elapsed_pc_sec > 1.0:
        print(f"\n{'='*55}")
        print(f"  Total presses:    {press_count}")
        print(f"  Total duration:   {elapsed_pc_sec:.1f} s")
        print(f"  Total drift:      {drift_ms:+.3f} ms")
        print(f"  Final PPM:        {ppm:+.1f}")
        print(f"  Drift rate:       {ppm / 1000:.4f} ms/s")
        print(f"  Log saved to:     {log_path}")
        print(f"{'='*55}")
    print("Done.")
