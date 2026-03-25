"""PsychoPy adapter for the PST Chronos response box.

Wraps the framework-agnostic ``chronos.Chronos`` driver into a
drop-in replacement for ``psychopy.hardware.keyboard.Keyboard``,
merging Chronos button and AUX input events with regular keyboard events.

Timing:
    - ``rt``             : seconds from last ``clock.reset()`` to press
                           (trial-relative, same as PsychoPy KeyPress.rt).
    - ``tDown``          : seconds from experiment start to press
                           (experiment-absolute, same as PsychoPy KeyPress.tDown).
    - ``hw_timestamp_us``: raw Chronos µs counter, for offline drift correction.
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import psychopy.clock
from psychopy import logging
from psychopy.constants import NOT_STARTED
from psychopy.hardware import keyboard

# Capture the real Keyboard class at import time, before any monkey-patching.
# HybridKeyboard.start() uses this so it never accidentally instantiates itself.
_OriginalKeyboard = keyboard.Keyboard

from .chronos import ChronosLEDs


# ---------------------------------------------------------------------------
# KeyPress-compatible shim for Chronos button events
# ---------------------------------------------------------------------------

@dataclass
class _ChronosKeyShim:
    """Mimics PsychoPy's ``KeyPress`` for Chronos events.

    Supports equality checks against strings (``if key == '1': ...``)
    and carries the raw hardware timestamp for offline drift correction.
    """
    name: str
    rt: float                                # PC-clock trial RT
    tDown: float = 0.0                       # time since experiment start
    t: float = 0.0                           # same as tDown (BaseResponse compat)
    duration: Optional[float] = None         # filled on release
    code: str = ""                           # key code (BaseResponse compat)
    hw_timestamp_us: int = 0                 # raw Chronos µs timestamp

    # PsychoPy's getKeys filter uses .value
    @property
    def value(self):
        return self.name

    def __eq__(self, other):
        if isinstance(other, str):
            return self.name == other
        return NotImplemented


# ---------------------------------------------------------------------------
# HybridKeyboard: drop-in replacement for keyboard.Keyboard
# ---------------------------------------------------------------------------

class HybridKeyboard:
    """Keyboard that also listens to the Chronos response box.

    Reads from both PsychoPy's built-in keyboard and the Chronos USB
    device simultaneously.  Implements the same API as
    ``psychopy.hardware.keyboard.Keyboard``.

    Parameters
    ----------
    deviceName : str or None
        Identifier for PsychoPy's DeviceManager. Passed to the inner
        ``keyboard.Keyboard()``.
    device : int
        Physical keyboard index (-1 for all keyboards). Passed to the
        inner ``keyboard.Keyboard()``.
    bufferSize : int
        Maximum number of key events to buffer. Passed to the inner
        ``keyboard.Keyboard()``.
    waitForStart : bool
        If False (default), the keyboard and Chronos are connected and
        start listening immediately. If True, you must call ``start()``
        before any events will be captured.
    clock : psychopy.clock.Clock or None
        Custom clock for timestamping. If None, a new Clock is created.
        Passed to the inner ``keyboard.Keyboard()``.
    backend : str or None
        Keyboard backend ('ptb', 'iohub', 'event'). Passed to the
        inner ``keyboard.Keyboard()``.
    """

    def __init__(
        self,
        deviceName=None,
        device=-1,
        bufferSize=10000,
        waitForStart=False,
        clock=None,
        backend=None,
    ):
        # Store params for forwarding to inner keyboard in start()
        self._kb_params = dict(
            deviceName=deviceName,
            device=device,
            bufferSize=bufferSize,
            waitForStart=False,     # inner KB always auto-starts when we call start()
            clock=clock,
            backend=backend,
        )

        # Inner objects (created in start())
        self._kb: Optional[keyboard.Keyboard] = None
        self._chronos: Optional[ChronosLEDs] = None
        self._started = False

        # Press/release pairing for Chronos events (mirrors PsychoPy's _keysStillDown)
        self._chronos_still_down: deque[_ChronosKeyShim] = deque()
        self._chronos_events: deque[_ChronosKeyShim] = deque()

        # Accumulates every KeyPress/ChronosKeyShim returned by getKeys() during
        # the current trial.  Reset by clearEvents() so Builder's "discard previous"
        # at Begin Routine wipes it.  Use key_resp.response_log in End Routine
        # to access the full objects after Builder has reduced key_resp.keys to a string.
        self.response_log: list = []

        # Builder storage attributes (written to directly by auto-generated code)
        self.status = NOT_STARTED
        self.keys = []
        self.corr = 0
        self.rt = []
        self.time = []

        # Match PsychoPy: auto-start unless told to wait
        if not waitForStart:
            self.start()

    # --- lifecycle ----------------------------------------------------------

    def start(self):
        """Connect hardware and begin listening for events."""
        if self._started:
            return

        self._kb = _OriginalKeyboard(**self._kb_params)

        # Use psychopy.clock.getTime, the platform's absolute high-resolution
        # counter (QPC / PTB / mach_absolute_time).  This is the same domain as
        # clock.getLastResetTime(), so rt and tDown calculations have no unit
        # mismatch and are immune to clock.reset() calls between press and poll.
        self._chronos = ChronosLEDs(clock=psychopy.clock.getTime)

        if self._chronos.connected:
            self._chronos.start()

        self._started = True

    def stop(self):
        """Stop the Chronos polling thread and the inner keyboard."""
        if self._chronos is not None:
            self._chronos.stop()
        if self._kb is not None:
            self._kb.stop()

    # --- clock property (delegated to inner keyboard) -----------------------

    @property
    def clock(self):
        if self._kb is None:
            return None
        return self._kb.clock

    @clock.setter
    def clock(self, value):
        if self._kb is not None:
            self._kb.clock = value

    # --- Chronos event processing -------------------------------------------

    def _process_chronos_events(self):
        """Drain the Chronos driver queue and pair presses with releases."""
        for evt in self._chronos.get_events():
            if evt.is_press:
                # evt.timestamp is absolute (core.getTime frame).
                # rt  = time from last trial-clock reset to press.
                # tDown = time from experiment start to press (matches KeyPress.tDown).
                rt = evt.timestamp - self._kb.clock.getLastResetTime()
                global_tDown = evt.timestamp - logging.defaultClock.getLastResetTime()

                shim = _ChronosKeyShim(
                    name=evt.button,
                    rt=rt,
                    tDown=global_tDown,
                    t=global_tDown,
                    duration=None,
                    code=evt.button,
                    hw_timestamp_us=evt.hw_timestamp_us,
                )
                self._chronos_events.append(shim)
                self._chronos_still_down.append(shim)
            else:
                # Release: find the matching press and set its duration.
                # Formula mirrors PsychoPy's PTB parseMessage exactly:
                #   duration = release_abs - press_abs
                #            = evt.timestamp - (tDown + defaultClock.getLastResetTime())
                for pressed in self._chronos_still_down:
                    if pressed.name == evt.button:
                        pressed.duration = (
                            evt.timestamp
                            - pressed.tDown
                            - logging.defaultClock.getLastResetTime()
                        )
                        self._chronos_still_down.remove(pressed)
                        break

    # --- public API (matches keyboard.Keyboard exactly) ---------------------

    def getKeys(self, keyList=None, ignoreKeys=None, waitRelease=True, clear=True):
        """Return key presses from both the keyboard and Chronos.

        Parameters match ``psychopy.hardware.keyboard.Keyboard.getKeys()``.
        """
        # Native keyboard keys (fully delegated)
        keys = list(self._kb.getKeys(
            keyList=keyList,
            ignoreKeys=ignoreKeys,
            waitRelease=waitRelease,
            clear=clear,
        ))

        # Chronos keys
        self._process_chronos_events()
        to_return = []
        for shim in self._chronos_events:
            # Match PsychoPy semantics exactly:
            #   waitRelease=True  → only return events that have been released
            #   waitRelease=False → return all press events, released or not
            was_released = shim.duration is not None
            if waitRelease and not was_released:
                continue

            if keyList is not None and shim.name not in keyList:
                continue
            if ignoreKeys is not None and shim.name in ignoreKeys:
                continue

            to_return.append(shim)

        if clear:
            for shim in to_return:
                self._chronos_events.remove(shim)

        keys.extend(to_return)
        keys.sort(key=lambda k: k.rt if k.rt is not None else 0)
        self.response_log.extend(keys)
        return keys

    def getState(self, keys):
        """Return the current pressed/unpressed state of one or more keys.

        Parameters match ``psychopy.hardware.keyboard.Keyboard.getState()``.
        Chronos buttons and AUX inputs are not included; only the native
        keyboard is queried.
        """
        return self._kb.getState(keys)

    def get_aux_events(self):
        """Return and clear all AUX input transitions since last call.

        Returns a list of ``AuxEvent`` objects, each with ``channel``
        ('F' or 'G'), ``is_rising``, ``hw_timestamp_us``, and ``timestamp``.
        Simultaneous F+G transitions produce two events sharing the same
        ``hw_timestamp_us``.
        """
        if self._chronos is None:
            return []
        return self._chronos.get_aux_events()

    def clearEvents(self, eventType=None):
        """Clear events from both the keyboard and Chronos."""
        self._kb.clearEvents(eventType=eventType)
        if self._chronos is not None:
            self._chronos.clear()
        self._chronos_events.clear()
        self._chronos_still_down.clear()
        self.response_log.clear()

    def waitKeys(self, maxWait=float('inf'), keyList=None, waitRelease=True,
                 clear=True):
        """Block until a key is pressed (keyboard or Chronos).

        Parameters match ``psychopy.hardware.keyboard.Keyboard.waitKeys()``.
        Returns None if maxWait is exceeded (matches PsychoPy behaviour).
        """
        # Fresh timer so maxWait is always relative to this call, regardless
        # of when the trial clock was last reset.
        timer = psychopy.clock.Clock()

        if clear:
            self.clearEvents()

        while timer.getTime() < maxWait:
            keys = self.getKeys(
                keyList=keyList,
                ignoreKeys=None,
                waitRelease=waitRelease,
                clear=clear,
            )
            if keys:
                return keys
            psychopy.clock._dispatchWindowEvents()  # prevent "app not responding"
            time.sleep(0.00001)

        return None
