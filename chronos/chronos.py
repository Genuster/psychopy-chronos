"""PST Chronos PST-100430 pure-Python USB driver (no framework dependencies).

Communicates with the Chronos over USB bulk endpoint 0x82 using PyUSB
and libusb.  Runs a daemon thread that emits timestamped button events
via a polling API.

Packet layout (20 bytes per state-change on endpoint 0x82):

    Byte  0      : always 0x00
    Byte  1      : always 0x00
    Bytes 2-5    : Chronos internal timestamp (µs, big-endian uint32)
    Byte  6      : AUX line level (F=0x40, G=0x80 when HIGH)
    Byte  7      : button STATE bitmask (currently held buttons)
    Byte  8      : AUX changed bitmask (inputs that fired this event)
    Byte  9      : button EVENT bitmask (buttons that changed)
    Byte 10      : event type (0x02 = button/AUX event)
    Bytes 11-19  : second event slot, same layout offset by 10
                   (AUX level=16, btn state=17, AUX changed=18, btn event=19)

Physical button bitmask (bytes 7/9, active-high):
    Bit 0  (0x01) -> Button 1  (leftmost)
    Bit 1  (0x02) -> Button 2
    Bit 2  (0x04) -> Button 3  (centre)
    Bit 3  (0x08) -> Button 4
    Bit 4  (0x10) -> Button 5  (rightmost)

AUX input bitmask (bytes 6/8):
    Bit 6  (0x40) -> F  (IN15, AUX pin 7)
    Bit 7  (0x80) -> G  (IN16, AUX pin 6)
    Both   (0xC0) -> F+G simultaneous

A press/rise is detected when a bit appears in BOTH the state/level byte and
the event/changed byte.  A release/fall has the bit in the event/changed byte
only, with the state/level bit cleared.
"""

__all__ = ["Chronos", "ChronosLEDs", "ChronosEvent", "ButtonEvent", "AuxEvent", "BUTTON_MAP", "AUX_MAP"]

import logging
import queue
import struct
import time
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import libusb
import usb.core
import usb.backend.libusb1

from .chronos_constants import (
    VENDOR_ID,
    PRODUCT_ID,
    EP_IN,
    PKT_LEN,
    READ_SIZE,
    TIMEOUT_MS,
    _OFF_TS,
    _OFF_STATE,
    _OFF_BUTTON_LEVEL,
    _OFF_BUTTON_CHANGED,
    _OFF_TS_2,
    _OFF_BUTTON_LEVEL_2,
    _OFF_BUTTON_CHANGED_2,
    BUTTON_MAP,
    AUX_MAP,
    _OFF_AUX_LEVEL,
    _OFF_AUX_CHANGED,
    _OFF_AUX_LEVEL_2,
    _OFF_AUX_CHANGED_2,
    EP_CMD_OUT,
    EP_CMD_IN,
    _CMD_WRITE_TIMEOUT_MS,
    _NUM_LEDS,
    _CMD_LED,
    _OP_WRITE,
    _OP_ENABLE,
    _OP_DISABLE,
    _REG_ENABLE,
    _LED_REG_R,
    _LED_REG_G,
    _LED_REG_B,
    _LED_BITS,
    _CMD_CALIB,
    _CALIB_RESP_OFFSET,
    _CALIB_COUNT,
    _CALIB_FLOAT_MIN,
    _CALIB_FLOAT_MAX,
    _CALIB_WAIT_S,
    _CMD_COMMIT, _COMMIT_RESP_LEN, _FW_DESC_OFFSET,
    _LED_SEQ_INIT,
    _LED_DRAIN_TIMEOUT_MS,
    _INIT_PACKETS,
)

logger = logging.getLogger(__name__)


@dataclass
class ChronosEvent:
    """Base class for all timestamped Chronos hardware events."""

    timestamp: float = 0.0  # seconds (from supplied clock)
    hw_timestamp_us: int = 0  # raw Chronos µs timestamp


@dataclass
class ButtonEvent(ChronosEvent):
    """A physical button press or release on the Chronos."""

    button: str = ""  # '1'..'5' (left to right)
    is_press: bool = True  # False = release


@dataclass
class AuxEvent(ChronosEvent):
    """A digital transition on a Chronos AUX input line."""

    channel: str = ""  # 'F' (IN15, pin 7) or 'G' (IN16, pin 6)
    is_rising: bool = True  # False = falling edge


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
    counter, subject to +68.5 ppm drift (see module docstring).

    Supports the context-manager protocol::

        with Chronos() as c:
            ...
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or time.perf_counter
        self._button_events: deque[ButtonEvent] = deque()
        self._aux_events: deque[AuxEvent] = deque()
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
                logger.info(
                    "Chronos connected (VID=%04x PID=%04x)", VENDOR_ID, PRODUCT_ID
                )
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

    def get_button_events(self) -> list[ButtonEvent]:
        """Return and clear all queued button events."""
        if not self.connected:
            return []
        with self._lock:
            evts = list(self._button_events)
            self._button_events.clear()
        return evts

    def get_aux_events(self) -> list[AuxEvent]:
        """Return and clear all queued AUX input events."""
        if not self.connected:
            return []
        with self._lock:
            evts = list(self._aux_events)
            self._aux_events.clear()
        return evts

    def clear(self) -> None:
        """Discard any queued button and AUX events."""
        if not self.connected:
            return
        with self._lock:
            self._button_events.clear()
            self._aux_events.clear()

    # --- internals ---

    @staticmethod
    def _parse_hw_timestamp(data: bytes, offset: int = 2) -> int:
        """Extract the 4-byte big-endian µs timestamp from a packet."""
        return int.from_bytes(data[offset : offset + 4], byteorder="big")

    def _parse_button_slot(
        self,
        data: bytes,
        ts: float,
        ts_offset: int,
        level_offset: int,
        changed_offset: int,
    ) -> None:
        """Parse one button event slot and enqueue any button events."""
        level_mask = data[level_offset]
        changed_mask = data[changed_offset]

        if changed_mask == 0:
            return

        hw_ts = self._parse_hw_timestamp(data, ts_offset)

        for bit_val, key_name in BUTTON_MAP:
            if changed_mask & bit_val:
                is_press = bool(level_mask & bit_val)
                evt = ButtonEvent(
                    button=key_name,
                    timestamp=ts,
                    hw_timestamp_us=hw_ts,
                    is_press=is_press,
                )
                with self._lock:
                    self._button_events.append(evt)

    def _parse_aux_slot(
        self,
        data: bytes,
        ts: float,
        ts_offset: int,
        level_offset: int,
        changed_offset: int,
    ) -> None:
        """Parse one AUX event slot and enqueue any AUX events."""
        level_mask = data[level_offset]
        changed_mask = data[changed_offset]

        if changed_mask == 0:
            return

        hw_ts = self._parse_hw_timestamp(data, ts_offset)

        for bit_val, channel_name in AUX_MAP:
            if changed_mask & bit_val:
                is_rising = bool(level_mask & bit_val)
                evt = AuxEvent(
                    channel=channel_name,
                    timestamp=ts,
                    hw_timestamp_us=hw_ts,
                    is_rising=is_rising,
                )
                with self._lock:
                    self._aux_events.append(evt)

    def _poll_loop(self) -> None:
        """Background thread: read USB packets and emit events."""
        while self._running:
            try:
                data = self.dev.read(EP_IN, READ_SIZE, timeout=TIMEOUT_MS)
                ts = self._clock()  # timestamp immediately after USB read

                if data and len(data) >= PKT_LEN:
                    # Physical buttons: slot 1 (bytes 7/9) and slot 2 (bytes 17/19)
                    self._parse_button_slot(
                        data, ts,
                        ts_offset=_OFF_TS,
                        level_offset=_OFF_BUTTON_LEVEL,
                        changed_offset=_OFF_BUTTON_CHANGED,
                    )
                    self._parse_button_slot(
                        data, ts,
                        ts_offset=_OFF_TS_2,
                        level_offset=_OFF_BUTTON_LEVEL_2,
                        changed_offset=_OFF_BUTTON_CHANGED_2,
                    )
                    # AUX inputs: slot 1 (bytes 6/8) and slot 2 (bytes 16/18)
                    self._parse_aux_slot(
                        data, ts,
                        ts_offset=_OFF_TS,
                        level_offset=_OFF_AUX_LEVEL,
                        changed_offset=_OFF_AUX_CHANGED,
                    )
                    self._parse_aux_slot(
                        data, ts,
                        ts_offset=_OFF_TS_2,
                        level_offset=_OFF_AUX_LEVEL_2,
                        changed_offset=_OFF_AUX_CHANGED_2,
                    )
            except usb.core.USBError:
                pass  # timeout, no data available
            except Exception as e:
                logger.error("Chronos poll error: %s", e)


# ---------------------------------------------------------------------------
# LED packet helper
# ---------------------------------------------------------------------------


def _led_packet(seq: int, triplets: list) -> bytes:
    """Build a cmd=0x00 LED control packet from (op, reg, val) triplets."""
    data = bytes([_CMD_LED, seq & 0xFF, 0x00, len(triplets)])
    for op, reg, val in triplets:
        data += bytes([op, reg, max(0, min(255, val))])
    return data


# ---------------------------------------------------------------------------
# Chronos with LED control
# ---------------------------------------------------------------------------


class ChronosLEDs(Chronos):
    """Chronos with LED control.

    Call ``init_leds()`` once after construction to initialize the LED
    subsystem before using ``leds_on``, ``leds_off``, or ``set_leds``.
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        super().__init__(clock=clock)
        self.leds_ready: bool = False
        self._led_seq: int = 0
        self._calib: list = []  # [(r, g, b), ...] × 5 after init_leds()
        self._led_lock = threading.Lock()
        self._drain_running: bool = False
        self._drain_thread: Optional[threading.Thread] = None
        self._calib_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._collecting_calib: bool = False
        self._fw_descriptor: bytes = b""  # 4-byte descriptor from 0x1c response

    # --- lifecycle override ---

    def stop(self) -> None:
        """Turn off LEDs, then stop background threads."""
        self.leds_off()
        self._drain_running = False
        self._collecting_calib = False
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=1.0)
            self._drain_thread = None
        super().stop()

    # --- private helpers ---

    def _drain_loop(self) -> None:
        """Background thread: continuously drain EP_CMD_IN acks.

        Keeps the device from stalling without blocking the write path.
        Reads with a short timeout so it reacts within ~1 ms when an ack
        is pending, and yields the CPU promptly when the queue is empty.
        During init_leds(), also captures calibration responses into
        _calib_queue so the main thread can parse them without blocking writes.
        """
        while self._drain_running:
            try:
                resp = bytes(self.dev.read(EP_CMD_IN, READ_SIZE,
                                           timeout=_LED_DRAIN_TIMEOUT_MS))
                if self._collecting_calib and resp:
                    if resp[0] == _CMD_CALIB:
                        self._calib_queue.put(resp)
                    elif (resp[0] == _CMD_COMMIT
                          and len(resp) >= _COMMIT_RESP_LEN
                          and not self._fw_descriptor):
                        self._fw_descriptor = bytes(resp[_FW_DESC_OFFSET:_COMMIT_RESP_LEN])
            except usb.core.USBError:
                pass  # timeout, no ack pending

    def _led_write(self, data: bytes) -> None:
        """Write to EP_CMD_OUT. Acks are drained by the background thread."""
        self.dev.write(EP_CMD_OUT, data, _CMD_WRITE_TIMEOUT_MS)

    # --- public LED API ---

    @property
    def firmware_descriptor(self) -> bytes:
        """Raw 4-byte firmware version returned by the device on commit.

        Captured from the ``0x1c`` commit response during ``init_leds()``.
        Layout: ``[major, minor, patch, 0x00]`` (one byte each).
        Observed value: ``b'\\x01\\x00\\x21\\x00'`` → firmware v1.0.33.
        Empty bytes before ``init_leds()`` is called.

        To unpack: ``major, minor, patch, _ = c.firmware_descriptor``
        """
        return self._fw_descriptor

    @property
    def calibration(self) -> dict:
        """Per-LED calibration coefficients read from the device.

        Returns a dict ``{0: (r, g, b), ..., 4: (r, g, b)}`` where each
        float is a scale factor in ``(0, 1]``.  Empty dict before
        ``init_leds()`` is called.
        """
        return {i: self._calib[i] for i in range(len(self._calib))}

    def init_leds(self) -> None:
        """Run the 136-packet init sequence and read calibration from device.

        Must be called once before ``leds_on`` / ``leds_off`` / ``set_leds``.
        Raises ``RuntimeError`` if calibration data cannot be read.
        """
        if not self.connected:
            return
        with self._led_lock:
            # Start the drain thread before the write loop so acks never back
            # up on the device.  The thread also captures calibration responses
            # into _calib_queue so we can parse them without blocking writes.
            self._drain_running = True
            self._collecting_calib = True
            self._drain_thread = threading.Thread(
                target=self._drain_loop, daemon=True, name="ChronosLEDs-drain"
            )
            self._drain_thread.start()

            for pkt in _INIT_PACKETS:
                self.dev.write(EP_CMD_OUT, pkt, _CMD_WRITE_TIMEOUT_MS)

            # Collect calibration floats from the queue (filled by drain thread).
            calib_floats: list = []
            deadline = time.monotonic() + _CALIB_WAIT_S
            while len(calib_floats) < _CALIB_COUNT:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    resp = self._calib_queue.get(timeout=remaining)
                    n = (len(resp) - _CALIB_RESP_OFFSET) // 4
                    calib_floats += [
                        f
                        for f in struct.unpack_from(f"<{n}f", resp, _CALIB_RESP_OFFSET)
                        if _CALIB_FLOAT_MIN < f < _CALIB_FLOAT_MAX
                    ]
                    calib_floats = calib_floats[:_CALIB_COUNT]
                except queue.Empty:
                    break

            self._collecting_calib = False

            if len(calib_floats) != _CALIB_COUNT:
                raise RuntimeError(
                    f"Chronos LED init failed: expected {_CALIB_COUNT} calibration "
                    f"floats, got {len(calib_floats)}"
                )
            self._calib = [
                (calib_floats[i * 3], calib_floats[i * 3 + 1], calib_floats[i * 3 + 2])
                for i in range(_NUM_LEDS)
            ]
            self._led_seq = _LED_SEQ_INIT
            self.leds_ready = True
            logger.info("Chronos LED init done. Calibration: %s", self._calib)

    def leds_on(
        self,
        colors=(255, 255, 255),
        leds: tuple = (0, 1, 2, 3, 4),
    ) -> None:
        """Pre-load color registers and enable LEDs (no auto-off).

        Parameters
        ----------
        colors : (r, g, b) or list of (r, g, b)
            A single tuple applies the same color to all LEDs in ``leds``.
            A list of tuples applies one color per LED in ``leds``.
            Values 0–255.
        leds : tuple of int
            LED indices to light (0 = leftmost, 4 = rightmost).
            LEDs not listed are explicitly zeroed to prevent bleed-through
            from previous commands.
        """
        if not self.leds_ready:
            return
        # Normalise: flat (r, g, b) → one entry per LED in `leds`
        if isinstance(colors[0], int):
            color_per_led = {i: colors for i in leds}
        else:
            color_per_led = {leds[j]: colors[j] for j in range(len(leds))}
        with self._led_lock:
            # Build all 15 color triplets + 5 enable triplets in one packet.
            # 20 triplets × 3 bytes + 4-byte header = 64 bytes (USB packet limit).
            # One write + one drain instead of two, halving per-frame overhead.
            triplets = []
            for i in range(_NUM_LEDS):
                if i in color_per_led:
                    r, g, b = color_per_led[i]
                    cr, cg, cb = self._calib[i]
                    triplets += [
                        (_OP_WRITE, _LED_REG_R[i], round(cr * r)),
                        (_OP_WRITE, _LED_REG_G[i], round(cg * g)),
                        (_OP_WRITE, _LED_REG_B[i], round(cb * b)),
                    ]
                else:
                    triplets += [
                        (_OP_WRITE, _LED_REG_R[i], 0),
                        (_OP_WRITE, _LED_REG_G[i], 0),
                        (_OP_WRITE, _LED_REG_B[i], 0),
                    ]
            triplets += [(_OP_ENABLE, _REG_ENABLE, _LED_BITS[i]) for i in leds]
            self._led_write(_led_packet(self._led_seq, triplets))
            self._led_seq = (self._led_seq + 1) & 0xFF

    def leds_off(self) -> None:
        """Disable all LEDs."""
        if not self.leds_ready:
            return
        with self._led_lock:
            triplets = [(_OP_DISABLE, _REG_ENABLE, bit) for bit in _LED_BITS]
            self._led_write(_led_packet(self._led_seq, triplets))
            self._led_seq = (self._led_seq + 1) & 0xFF

    def set_leds(
        self,
        colors=(255, 255, 255),
        duration: float = 1.0,
        leds: tuple = (0, 1, 2, 3, 4),
    ) -> None:
        """Light LEDs for ``duration`` seconds, then turn off (blocking).

        colors : (r, g, b) or list of (r, g, b). Same as ``leds_on``.
        """
        self.leds_on(colors=colors, leds=leds)
        time.sleep(duration)
        self.leds_off()
