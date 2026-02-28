# ⚡ Uno R4 KV Store (v1.2.0)

A high-performance, pipelined Key-Value store for the **Arduino Uno R4 WiFi**, featuring an optimized ARM Thumb assembly backend and a binary async protocol over USB-Serial.

---

## 🛠 Features
- **ARM Thumb Optimized**: Uses `LDM`/`STM` for fast 16-byte value copies.
- **COBS Framing**: Robust packet recovery via Consistent Overhead Byte Stuffing.
- **Full Pipelining**: Up to **32 concurrent** in-flight requests.
- **Batch Operations**: Bundle up to **16 ops** in a single USB poll interval.
- **Auto-Formatting**: Python client handles string padding/truncation automatically.

---

## 🛰 Protocol Specification

### Framing
- **Method**: COBS (Consistent Overhead Byte Stuffing)
- **Delimiter**: `0x00` (used exclusively as end-of-packet marker)

### Base Packet Structure (Decoded)
`ID[2LE] + CMD[1] + DATA[...]`

| Field | Size | Details |
| :--- | :--- | :--- |
| **ID** | 2 Bytes | Little-Endian request correlation ID |
| **CMD** | 1 Byte | Command character ('c', 'r', 'd', 'b', '?') |
| **DATA** | Variable | Command-specific payload |

---

## 📜 Commands Reference

| CMD | Description | Request Data | Response Data |
| :--- | :--- | :--- | :--- |
| `?` | Help | *None* | Help String |
| `c` | Create/Update | `KEY[4] + VAL[16]` | `STATUS[1]` |
| `r` | Read | `KEY[4]` | `STATUS[1] [+ VAL[16] on hit]` |
| `d` | Delete | `KEY[4]` | `STATUS[1]` |
| `b` | Batch | `COUNT[1] + {OPS...}` | `'K' + COUNT[1] + {RESULTS...}` |

### Status Codes
- `K`: Success / Found
- `F`: Table Full (256 entry limit)
- `?`: Not Found / Error

---

## 🐍 Python Client Usage

The `KVClient` handles connection management, COBS framing, and ID tracking automatically.

```python
import asyncio
from kv_client import KVClient

async def main():
    async with KVClient("COM3") as kv:
        # Padded to "user\x00" / "1234\x00..."
        await kv.create("user", "1234")
        
        # Read returns 16 bytes or None
        val = await kv.read("user")
        print(f"Read: {val}")

asyncio.run(main())
```

> [!IMPORTANT]  
> The Python client automatically **pads** keys to 4 bytes and values to 16 bytes. It will also **truncate** them if they are too long.

---

## 🚀 Quick Start
1. **Compile & Upload**: Open `kv_store/kv_store.ino` in Arduino IDE or via `arduino-cli`.
2. **Install Dependencies**: `pip install pyserial-asyncio`.
3. **Run Demo**: `python test.py COM3`.
