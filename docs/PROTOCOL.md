# PST Chronos Low-Level USB Protocol

---

## Device identifiers

| Field | Value |
|---|---|
| USB Vendor ID | `0x2266` (Psychology Software Tools, Inc.) |
| USB Product ID | `0x0007` (Chronos response box PST-100430) |

---

## Endpoints

| EP | Direction | Type | Purpose |
|---|---|---|---|
| `0x01` | OUT | Interrupt | Host → device: LED commands, init sequence |
| `0x81` | IN | Interrupt | Device → host: ACKs + calibration responses |
| `0x82` | IN | Interrupt | Device → host: button/AUX events (unsolicited) |

`0x01`/`0x81` form a **command/response pair**: every OUT command gets one
ACK on `0x81`.  You **must** drain `0x81` after each write or the device
stalls and refuses the next command (write times out).

`0x82` is a completely independent passive subsystem.  It requires no
initialization and fires spontaneously on button press/release and AUX input
transitions.  
---

## Button event packet format (EP `0x82`)

Every state change produces a 20-byte packet.  Two event slots are packed
per packet (the second slot may be unused / zeroed).

```
Byte  0–1  : header (always 0x00 0x00)
Bytes 2–5  : hardware timestamp (µs, big-endian uint32)
Byte  6    : AUX line level (which AUX inputs are currently HIGH)
Byte  7    : button STATE (physical buttons currently held)
Byte  8    : AUX changed (which AUX inputs changed this packet)
Byte  9    : button EVENT (physical buttons that changed this packet)
Byte  10   : event type (0x02 = button/AUX event)
Bytes 11–19: second event slot (same layout, offset by 10)
             (byte 19 doubles as global sequence counter when slot 2 unused)
```

**Physical button bitmask** (bytes 7 and 9, active-high):

| Bit | Mask | Button |
|---|---|---|
| 0 | `0x01` | Button 1 (leftmost) |
| 1 | `0x02` | Button 2 |
| 2 | `0x04` | Button 3 (centre) |
| 3 | `0x08` | Button 4 |
| 4 | `0x10` | Button 5 (rightmost) |

**AUX input bitmask** (bytes 6 and 8):

| Bit | Mask | Input | AUX connector pin | Wire colour |
|---|---|---|---|---|
| 6 | `0x40` | F (IN15) | Pin 7 | Brown |
| 7 | `0x80` | G (IN16) | Pin 6 | Yellow |
| 6+7 | `0xC0` | F+G simultaneous | Pins 6+7 | Both |

**Press vs release (physical buttons):**
- Press: bit set in **both** LEVEL (byte 7) and CHANGED (byte 9).
- Release: bit **0** in LEVEL (byte 7), but **1** in CHANGED (byte 9).

**Rise vs fall (AUX inputs):**
- Rising edge: bit set in **both** level (byte 6) and changed (byte 8).
- Falling edge: bit set in changed (byte 8) only; level bit cleared.

**Hardware timestamp:**
- Resolution: 1 µs.
- Wraps at `2^32 µs` ≈ 71.6 minutes.
- Drift: ~+68.5 ppm (measured; device-specific).
- Reading: `int.from_bytes(data[2:6], 'big')`.

---

## LED command packet format (EP `0x01`, cmd `0x00`)

```
Byte 0   : 0x00  (LED control command)
Byte 1   : sequence number (0x00–0xFF, wraps; device does not enforce order)
Byte 2   : 0x00  (reserved)
Byte 3   : N     (number of (op, reg, val) triplets)
Bytes 4…: N × 3 bytes:
             op  (1 byte) : operation code
             reg (1 byte) : register address
             val (1 byte) : value 0–255
```

Maximum packet size is 64 bytes (USB full-speed interrupt limit), so
`4 + 3N ≤ 64` → max **20 triplets** per packet.

### Operation codes

| Op | Name | Meaning |
|---|---|---|
| `0x83` | WRITE | Pre-load register `reg` with `val` |
| `0x81` | ENABLE | Enable LED(s) whose bit is set in `val` (reg must be `0xC6`) |
| `0x80` | DISABLE | Disable LED(s) whose bit is set in `val` (reg must be `0xC6`) |

### LED color registers

Each LED has three 8-bit registers for R, G, B brightness.

| LED | Index | Red reg | Green reg | Blue reg |
|---|---|---|---|---|
| Leftmost | 0 | `0xF0` | `0xF5` | `0xFA` |
| | 1 | `0xF1` | `0xF6` | `0xFB` |
| | 2 | `0xF2` | `0xF7` | `0xFC` |
| | 3 | `0xF3` | `0xF8` | `0xFD` |
| Rightmost | 4 | `0xF4` | `0xF9` | `0xFE` |

### LED enable/disable register

Register `0xC6` is a bitmask: bit *i* controls LED *i*.

| Bit | Mask | LED |
|---|---|---|
| 0 | `0x01` | LED 0 (leftmost) |
| 1 | `0x02` | LED 1 |
| 2 | `0x04` | LED 2 |
| 3 | `0x08` | LED 3 |
| 4 | `0x10` | LED 4 (rightmost) |

**Color registers persist** between commands.  When you enable LEDs, all
five will show their stored color, including ones from a previous command.
Always write zero to the registers of LEDs you do not want lit.

### LED lighting procedure

```
1. Write all 15 color registers in one packet (op=0x83):
     (0x83, REG_R[i], val), (0x83, REG_G[i], val), (0x83, REG_B[i], val)  × 5
   LEDs not being lit must be explicitly zeroed.

2. Enable target LEDs (op=0x81, reg=0xC6, one triplet per LED):
     (0x81, 0xC6, bitmask_for_led_i)  × N
   This is what triggers visible output.

3. Wait.

4. Disable LEDs (op=0x80, reg=0xC6):
     (0x80, 0xC6, bitmask_for_led_i)  × 5
```

**Optimization:** steps 1+2 can be packed into a single 64-byte packet
(15 WRITE triplets + 5 ENABLE triplets = 20 triplets × 3 bytes + 4-byte
header = 64 bytes exactly), saving one round-trip per frame.

---

## Calibration

The device stores per-LED per-channel gain factors that map a 0–255
brightness request to the actual register value:

```
register_value = round(calib_float × brightness)
```

There are **15 coefficients**: (R, G, B) × 5 LEDs.  They are returned by
the `0x1b` command (see Init sequence below) as 16 IEEE 754 little-endian
floats; the 16th is always 0.0 (padding).

Valid coefficient range: `0.05 < f < 1.0`.  Values outside this range
in the response should be discarded.

Calibration ensures all five LEDs appear the same color/brightness despite
per-unit LED variation.  Coefficients are device-specific and must be read
live from the device on each connection.

---

## Initialization sequence

The LED subsystem requires a **136-packet initialization sequence** before
it will respond to LED commands.  It is idempotent and must be sent on
every connection.

Button events do not require initialization.

### Command types in the sequence

#### `0x23`: License / auth handshake (~43 packets)

The host sends a software license assertion text in 32-byte chunks, alternating
"data" chunks (subtype `0x01`) with padding packets (subtype `0x02`/`0x03`).

The full text reads:
> *"© 2015 Psychology Software Tools, Inc.  Solutions for Research, Assessment,
> and Education.  By transmitting this message the sender asserts and confirms
> that they have been legally licensed and authorized by Psychology Software
> Tools to reproduce and transmit this text to the Chronos device."*

The device ACKs each packet.
**Without this sequence the device ignores all subsequent commands.**
It is sent twice: once at the start of init and once in the middle.

Packet format:
```
Byte 0   : 0x23
Byte 1   : sequence number
Byte 2   : 0x00
Byte 3   : subtype  (0x01 = data chunk, 0x02/0x03 = padding)
Bytes 4…: 32-byte payload (license text fragment or zeros)
```

#### `0x1a`: Gamma LUT upload (6 packets: 1 config + 3 data + repeated)

Uploads a 180-byte brightness correction lookup table to the device firmware.
The table maps an input value to an output PWM level before the calibration
factor is applied.  The observed table is a near-identity ramp starting at
value 128 (`0x80`).

The sequence is sent **twice** (packets `1a2d`–`1a30` and `1a7a`–`1a7d`).

Config packet: `1a XX 00 01 1b` (possibly selects which bank to load).
Response: 4-byte ACK `1a XX 00 00`.

Data packets: `1a XX 00 3c <60 bytes>`, each carrying 60 bytes of LUT data.
Response: 63 bytes echoing back the **previous** state of those LUT registers
before the write (mostly zeros on a freshly-connected device).  Not useful
for reading; treat as an ACK.

Second variant: `1a XX 00 02 06 07` (purpose unknown, possibly sets active
channels).  Appears after the second `0x1b` calibration read.  Response: 5-byte ACK.

#### `0x1b`: Calibration read (2 packets per read, sent twice)

Requests the per-LED calibration coefficients from the device.

Request format:
```
Byte 0 : 0x1b
Byte 1 : sequence number
Byte 2 : 0x00
Byte 3 : 0x06 or 0x08  (bank / channel selector, purpose unknown)
Bytes 4–: zeros
```

Response format (device → host, EP `0x81`):
```
Byte 0      : 0x1b  (echoed command ID)
Byte 1      : echoed sequence number
Byte 2      : 0x00
Bytes 3–34  : 8 × IEEE 754 float32, little-endian
```

Two requests are needed to get all 15 coefficients: the first response
carries floats 1–8, the second carries floats 9–15 plus one trailing `0.0`.

Layout across both responses:
```
[R0, G0, B0,  R1, G1, B1,  R2, G2,  |  B2, R3, G3, B3,  R4, G4, B4,  0.0]
 <----------- response 1 ----------->   <------------- response 2 ---------->
```

Only floats in the range `(0.05, 1.0)` are valid; the trailing `0.0` is
discarded by the filter.

Example coefficients (from one device):
```
LED 0: R=0.5405  G=0.2881  B=0.1646
LED 1: R=0.6557  G=0.3403  B=0.1970
LED 2: R=0.5298  G=0.2718  B=0.1594
LED 3: R=0.5112  G=0.2744  B=0.1562
LED 4: R=0.5128  G=0.2593  B=0.1481
```

The calibration read is performed twice during init.  Both reads return
identical coefficients.

#### `0x18`: Hardware configuration (4 packets, sent twice)

```
18 XX 00 09
18 XX 00 49
18 XX 00 89
18 XX 00 C9
```

Data bytes `0x09`, `0x49`, `0x89`, `0xC9` differ only in bits 6–7
(`0b00`, `0b01`, `0b10`, `0b11`), likely selecting 4 consecutive hardware
register banks.

**Device response: pure ACK (8 bytes, all payload bytes zero).**

```
18 XX 00 00 00 00 00 00
```

`0x18` is a write-only configuration command; the device does not return
any hardware data in the response.

#### `0x1c`: Commit / apply (3 packets)

Finalizes the preceding configuration block.

**Device response: 7 bytes (includes a 4-byte descriptor).**

```
1c XX 00  01 00 21 00
```

Bytes 3–6 (`01 00 21 00`) encode the **firmware version**:
```
Byte 3: major  = 0x01 = 1
Byte 4: minor  = 0x00 = 0
Byte 5: patch  = 0x21 = 33
Byte 6: unused = 0x00
```
→ **firmware v1.0.33**.

`FirmwareVersion` is a named field in the device identity profile
(adjacent to `SerialNumber` and `FriendlyName`).  `SerialNumber` and
`FriendlyName` are read via standard USB string descriptors, not from
the Chronos protocol.

This is the only place the device returns non-trivial structured data
outside the calibration read.

### Complete response type inventory

All response command bytes observed across all sessions:

| Cmd | Response | Notes |
|---|---|---|
| `0x17` | 3-byte ACK | Keepalive (only during live experiment) |
| `0x18` | 8-byte ACK (zeros) | Hardware config write, no data returned |
| `0x1a` | 4-byte ACK or 63-byte previous-register-state | Gamma LUT write |
| `0x1b` | 35 bytes (8 calibration floats) | Calibration read |
| `0x1c` | 7 bytes (3-byte ACK + 4-byte firmware version) | Commit/apply |
| `0x23` | 1, 6, or 32 bytes (license text echo) | Auth handshake |

No other command bytes appear as response types in any session.

#### `0x00`: LED initialization commands (within init)

Several `cmd=0x00` LED packets appear during init to set up the LED state
machine.  Registers involved beyond the standard color/enable registers:

| Register | Usage in init | Notes |
|---|---|---|
| `0x01`–`0x05` | Written with various values | Likely PWM or brightness config |
| `0x08`, `0x09` | Written to `0x00` | Possibly mode/config reset |
| `0xC0` | Written via op `0x80` (val `0x01`) | Unknown (possible master enable) |
| `0xC1` | Written via `0x80`/`0x81` with various bitmasks | Unknown (possibly per-LED power or PWM mode) |
| `0xC6` | Standard enable register | Used normally |
| `0xFF` | Written with `0x0C`, `0x4A` | Possibly a commit trigger or clock divider |

The full `0x00` init sequence appears to be a power-on self-test that cycles
the LED drive registers into a known state.

---

## Auxiliary I/O connector

The Chronos has an 8-pin AUX I/O breakout connector.  Pinout reproduced from
the PST Chronos Operator Manual (https://pstnet.com/wp-content/uploads/2017/09/Chronos-Operator-Manual.pdf), §8.3:

| Pin | Colour | Function | Description | EP `0x82` pseudo-button |
|---|---|---|---|---|
| 1 | Light Blue | +5 V | Power output | — |
| 2 | Light Green | OUT14 (base 0) | Digital output | — |
| 3 | Purple | OUT15 | Digital output | — |
| 4 | White | Digital GND | Ground reference for digital inputs/outputs | — |
| 5 | Orange | Analog GND | Ground reference for ADC1 | — |
| 6 | Yellow | IN16 (base 1) | Digital input | G (`0x80`) |
| 7 | Brown | IN15 (base 1) | Digital input | F (`0x40`) |
| 8 | Red | ADC1 | Analog input | 9 (threshold crossing) |

**Digital inputs (IN15, IN16)** appear in EP `0x82` packets as pseudo-buttons
F and G using bitmasks `0x40` and `0x80` in bytes 6 and 8 (see packet format
above).  They behave identically to physical buttons: both rising and falling edges
are reported.

**ADC1** does not generate EP `0x82` events in response to TTL-level input.
It likely requires a dedicated USB read command (not yet documented).

**Digital outputs (OUT14, OUT15)** state is reported in bytes 6/8 of EP `0x82`
startup packets using bits `0x10` and `0x20` respectively.  Whether they can
be driven by USB command is not yet known.

---

## USB connect / disconnect behaviour

### Startup packets (EP `0x82`)

Three packets are sent within ~415 ms of every USB enumeration.  Sequence
numbers reset to `0x22`/`0x24` on each connect regardless of previous state.
Timestamps below are relative to USB enumeration (device clock resets per
session):

| # | seq | ts (µs) | Notes |
|---|---|---|---|
| 1 | `0x22` | ~1 603 | Reports OUT14/OUT15 initial state (bits `0x10`+`0x20`) going HIGH in bytes 6 and 8 |
| 2 | — | ~12 208 | 10-byte packet, type `0x01`, content invariant across all observed sessions |
| 3 | `0x24` | ~15 228 | OUT14/OUT15 falling edge; second timestamp slot contains a value related to the previous session (exact semantics unclear) |

Full semantics of these packets are not yet decoded.

### Disconnect packet (EP `0x82`)

On USB disconnect the device emits a fixed-content packet before the host
loses the connection:

```
seq=0x90  ts=0  byte6=0x00  byte8=0x40  Δts=66,650,111
```

`Δts = 66,650,111 µs` (66.65 s) appears to be a **hardcoded firmware
constant**: it is identical across every observed disconnect regardless of
session length or activity.  `seq=0x90` also resets to the same value each
time.

---

## Keepalive / poll (`0x16`)

The host sends `0x16` packets frequently during an experiment as a
watchdog / connection keepalive.  The device does not require them.

---

## Keepalive (`0x17`)

The host sends `0x17` packets during an experiment.  The device does not
require them.  Response is a 3-byte ACK `17 XX 00`.

## Unknown / partially decoded

| Item | Status |
|---|---|
| `0x1c` descriptor bytes (`01 00 21 00`) | **Decoded**: firmware v1.0.33 (major.minor.patch.unused) |
| `0xC0`, `0xC1` register semantics | **Unknown**: only seen in init LED commands |
| `0xFF` register semantics | **Unknown**: written `0x0C`/`0x4A` during init |
| `0x1a` config packet (`02 06 07`) | **Unknown**: possibly channel/bank select |
| Hardware timestamp clock source | **Assumed** µs; drift measured at ~+68.5 ppm |

