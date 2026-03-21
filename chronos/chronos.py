"""PST Chronos PST-100430 pure-Python USB driver (no framework dependencies).

Communicates with the Chronos over USB bulk endpoint 0x82 using PyUSB
and libusb.  Runs a daemon thread that emits timestamped button events
via a polling API.

Packet layout (20 bytes per state-change on endpoint 0x82):

    Byte  0      : always 0x00
    Byte  1      : always 0x00
    Bytes 2-5    : Chronos internal timestamp (µs, big-endian uint32)
    Byte  6      : unknown / reserved
    Byte  7      : button STATE bitmask  (which buttons are currently held)
    Byte  8      : unknown / reserved
    Byte  9      : button EVENT bitmask  (which buttons changed in this event)
    Byte 10      : event type (0x02 = button event)
    Bytes 11-19  : second event in same format (bytes 11-12 header,
                   13-16 timestamp, 17 state, 18 reserved, 19 event)

Button bitmask mapping (active-high):
    Bit 0  (0x01) -> Button 1  (leftmost)
    Bit 1  (0x02) -> Button 2
    Bit 2  (0x04) -> Button 3  (centre)
    Bit 3  (0x08) -> Button 4
    Bit 4  (0x10) -> Button 5  (rightmost)

A press is detected when a bit is set in BOTH the state and event masks
(state=held, event=changed).  A release has the bit set only in the
event mask while the state bit is cleared.
"""

__all__ = ["Chronos", "ButtonEvent", "BUTTON_MAP"]

import logging
import time
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import libusb
import usb.core
import usb.backend.libusb1

logger = logging.getLogger(__name__)

# --- USB identifiers ---
VENDOR_ID  = 0x2266
PRODUCT_ID = 0x0007

# --- Endpoint / packet constants ---
EP_IN          = 0x82   # bulk IN endpoint for button events
PKT_LEN        = 20     # expected minimum packet length
READ_SIZE      = 64     # max bytes per USB read
TIMEOUT_MS     = 10     # read timeout before retry (ms)

# --- Byte offsets inside a single 10-byte event slot ---
_OFF_TS    = 2   # 4-byte big-endian µs timestamp
_OFF_STATE = 7   # button state bitmask (held buttons)
_OFF_EVENT = 9   # button event bitmask (changed buttons)

# --- Second event slot starts at byte 11 ---
_SLOT2_BASE = 11
_OFF_TS_2    = _SLOT2_BASE + 2   # bytes 13-16
_OFF_STATE_2 = _SLOT2_BASE + 6   # byte 17
_OFF_EVENT_2 = _SLOT2_BASE + 8   # byte 19

# --- Button bitmask -> label ---
BUTTON_MAP: list[tuple[int, str]] = [
    (0x01, '1'),  # Button 1
    (0x02, '2'),  # Button 2
    (0x04, '3'),  # Button 3
    (0x08, '4'),  # Button 4
    (0x10, '5'),  # Button 5
]


@dataclass
class ButtonEvent:
    """A single Chronos button event (press or release)."""
    button: str                          # '1' .. '5'
    timestamp: float                     # seconds (from supplied clock)
    hw_timestamp_us: int = 0             # raw Chronos µs timestamp
    is_press: bool = True                # False = release


class Chronos:
    """Low-level reader for the PST Chronos response box.

    Parameters
    ----------
    clock : callable, optional
        A zero-argument function that returns the current time in seconds
        (e.g. ``time.perf_counter``).  Defaults to ``time.perf_counter``.

    Notes
    -----
    The ``timestamp`` field on emitted ``ButtonEvent`` objects is recorded
    immediately after the USB read returns, so it includes USB transfer
    latency but not parsing overhead.  For sub-millisecond analysis,
    prefer ``hw_timestamp_us`` (the raw Chronos-internal microsecond
    counter, subject to +68.5 ppm drift — see module docstring).

    Supports the context-manager protocol::

        with Chronos() as c:
            ...
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or time.perf_counter
        self._events: deque[ButtonEvent] = deque()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.connected: bool = False
        self.dev: Optional[usb.core.Device] = None

        try:
            backend = usb.backend.libusb1.get_backend(
                find_library=lambda x: libusb.dll._name
            )
            self.dev = usb.core.find(
                idVendor=VENDOR_ID,
                idProduct=PRODUCT_ID,
                backend=backend,
            )
            if self.dev:
                self.dev.set_configuration()
                self.connected = True
                logger.info("Chronos connected (VID=%04x PID=%04x)", VENDOR_ID, PRODUCT_ID)
            else:
                logger.warning("Chronos device not found")
        except (usb.core.USBError, usb.core.NoBackendError) as e:
            logger.warning("Chronos init error: %s", e)

    # --- lifecycle ---

    def start(self) -> None:
        """Begin listening for button events in a background thread."""
        if not self.connected:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # --- event access ---

    def get_events(self) -> list[ButtonEvent]:
        """Return and clear all queued button events."""
        if not self.connected:
            return []
        with self._lock:
            evts = list(self._events)
            self._events.clear()
        return evts

    def clear(self) -> None:
        """Discard any queued events."""
        if not self.connected:
            return
        with self._lock:
            self._events.clear()

    # --- internals ---

    @staticmethod
    def _parse_hw_timestamp(data: bytes, offset: int = 2) -> int:
        """Extract the 4-byte big-endian µs timestamp from a packet."""
        return int.from_bytes(data[offset:offset + 4], byteorder='big')

    def _parse_event_slot(self, data: bytes, ts: float,
                          ts_offset: int, state_offset: int,
                          event_offset: int) -> None:
        """Parse one 10-byte event slot and enqueue any button events."""
        state_mask = data[state_offset]
        event_mask = data[event_offset]

        if event_mask == 0:
            return  # no button changed in this slot

        hw_ts = self._parse_hw_timestamp(data, ts_offset)

        for bit_val, key_name in BUTTON_MAP:
            if event_mask & bit_val:
                is_press = bool(state_mask & bit_val)
                evt = ButtonEvent(
                    button=key_name,
                    timestamp=ts,
                    hw_timestamp_us=hw_ts,
                    is_press=is_press,
                )
                with self._lock:
                    self._events.append(evt)

    def _poll_loop(self) -> None:
        """Background thread: read USB packets and emit events."""
        while self._running:
            try:
                data = self.dev.read(EP_IN, READ_SIZE, timeout=TIMEOUT_MS)
                ts = self._clock()  # timestamp immediately after USB read

                if data and len(data) >= PKT_LEN:
                    # Parse first event slot (bytes 0-10)
                    self._parse_event_slot(
                        data, ts,
                        ts_offset=_OFF_TS,
                        state_offset=_OFF_STATE,
                        event_offset=_OFF_EVENT,
                    )
                    # Parse second event slot (bytes 11-19)
                    self._parse_event_slot(
                        data, ts,
                        ts_offset=_OFF_TS_2,
                        state_offset=_OFF_STATE_2,
                        event_offset=_OFF_EVENT_2,
                    )
            except usb.core.USBError:
                pass  # timeout — no data available
            except Exception as e:
                logger.error("Chronos poll error: %s", e)
