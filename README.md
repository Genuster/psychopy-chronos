# psychopy-chronos

Python driver for the PST Chronos response box, with a drop-in PsychoPy keyboard replacement.

## Installation

```bash
pip install psychopy-chronos
```

## Windows driver

If you've never installed E-Prime on your machine, install the USB driver for Chronos from the `chronos_driver_win/` directory.

## Usage

### Standalone

```python
from chronos import Chronos

with Chronos() as box:
    events = box.get_events()
```

### PsychoPy Builder

Add a **Code Component** with this in the *Before Experiment* tab:

```python
from chronos import HybridKeyboard
from psychopy.hardware import keyboard
keyboard.Keyboard = HybridKeyboard
```

All Builder Keyboard Response components will then capture Chronos button presses alongside regular keyboard input. Buttons are coded `'1'` through `'5'` left to right, so add them to the allowed keys list in your Keyboard Response component.

To save hardware timestamps, add this to the *End Routine* tab:

```python
if key_resp.response_log:
    thisExp.addData('chronos_hw_us',
        [getattr(k, 'hw_timestamp_us', '') for k in key_resp.response_log])
    thisExp.addData('chronos_pc_time_s',
        [getattr(k, 'tDown', '') for k in key_resp.response_log])
```

You can find a simple Builder demo in the `examples/` directory.  

## Hardware timestamps and drift correction

Each press is timestamped by both the host PC (`chronos_pc_time_s`, seconds from experiment start) and the Chronos internal crystal (`chronos_hw_us`, microseconds). The crystal drifts relative to the PC clock. I measured +68.7ppm on my laptop (1001 presses over 685s, about 47ms of total drift). The drift is linear and can be corrected offline by regressing `chronos_pc_time_s` onto `chronos_hw_us`:

```python
import numpy as np
slope, intercept = np.polyfit(hw_us_array, pc_time_array, 1)
corrected_rt = slope * hw_rt_us + intercept
```

Drift magnitude might or might not vary across different computers. The test script used for this measurement is in `test_chronos_timing.py`. Feel free to push the buttons few hundred times to test the drift yourself.

`chronos_hw_us` is a 32-bit counter that wraps every 71.6 minutes. Use modulo 2^32 arithmetic for elapsed time calculations across long sessions.
