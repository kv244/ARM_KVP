"""
kv_client.py — Async Python client for the Uno R4 KV Store

Protocol
--------
Packets are COBS-framed (0x00 = end-of-packet delimiter).

Request payload  (before COBS):  ID[2LE] CMD[1] KEY[4] [VAL[16] if create]
Batch request:                    ID[2LE] 'b'[1] COUNT[1] (CMD KEY [VAL])×N

Response payload (before COBS):  ID[2LE] STATUS[1] [VAL[16] if read hit]
Batch response:                   ID[2LE] 'K'[1] COUNT[1] (STATUS [VAL])×N

STATUS: b'K' = OK/found, b'F' = table full, b'?' = not found / error

Usage
-----
    import asyncio
    from kv_client import KVClient

    async def main():
        async with KVClient("/dev/ttyACM0") as kv:
            await kv.create(b"key1", b"value_________01")
            val = await kv.read(b"key1")
            print(val)

            # Pipelined: fire many requests concurrently
            results = await asyncio.gather(
                kv.read(b"key1"),
                kv.read(b"key2"),
                kv.create(b"key3", b"value_________03"),
            )

            # Batch: single USB packet for up to 16 ops
            batch_results = await kv.batch([
                ("create", b"key4", b"value_________04"),
                ("read",   b"key1"),
                ("delete", b"key2"),
            ])

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import struct
import logging
from typing import Optional

import serial_asyncio                   # pip install pyserial-asyncio

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# COBS codec
# ---------------------------------------------------------------------------

def cobs_encode(data: bytes) -> bytes:
    """Encode bytes using COBS; appends 0x00 delimiter."""
    out = bytearray()
    code_idx = 0
    out.append(0)  # placeholder for first code
    code = 1
    for b in data:
        if b == 0:
            out[code_idx] = code
            code_idx = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 255:
                out[code_idx] = code
                code_idx = len(out)
                out.append(0)
                code = 1
    out[code_idx] = code
    out.append(0x00)  # packet delimiter
    return bytes(out)



def cobs_decode(frame: bytes) -> bytes:
    """Decode a COBS frame (without trailing 0x00). Raises ValueError on error."""
    out = bytearray()
    i = 0
    while i < len(frame):
        code = frame[i]
        if code == 0:
            raise ValueError("Unexpected zero byte in COBS frame")
        i += 1
        for _ in range(code - 1):
            if i >= len(frame):
                raise ValueError("COBS frame truncated")
            out.append(frame[i])
            i += 1
        if code < 0xFF and i < len(frame):
            out.append(0x00)
    return bytes(out)


# ---------------------------------------------------------------------------
# Serial protocol reader (streams COBS-framed packets)
# ---------------------------------------------------------------------------

class _FrameReader(asyncio.Protocol):
    """asyncio.Protocol that reassembles COBS-framed packets from the byte stream."""

    def __init__(self) -> None:
        self._buf  = bytearray()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    def data_received(self, data: bytes) -> None:
        for b in data:
            if b == 0x00:
                if self._buf:
                    try:
                        pkt = cobs_decode(bytes(self._buf))
                        self._queue.put_nowait(pkt)
                    except ValueError as e:
                        log.warning("COBS decode error: %s — frame dropped", e)
                    self._buf.clear()
            else:
                self._buf.append(b)

    async def read_packet(self) -> bytes:
        return await self._queue.get()

    def connection_lost(self, exc: Optional[Exception]) -> None:
        log.debug("Serial connection lost: %s", exc)


# ---------------------------------------------------------------------------
# KVClient
# ---------------------------------------------------------------------------

BatchOp = tuple   # ("create", key, val) | ("read", key) | ("delete", key)


class KVClient:
    """
    Async KV store client.

    Supports fully-pipelined single operations and batched operations.
    All keys must be exactly 4 bytes; all values exactly 16 bytes.
    """

    MAX_BATCH    = 16
    MAX_PIPELINE = 32               # max in-flight requests (soft cap)

    def __init__(self, port: str, baud: int = 1_000_000) -> None:
        self._port       = port
        self._baud       = baud
        self._transport  = None
        self._protocol   = None
        self._pending:   dict[int, asyncio.Future] = {}
        self._seq        = 0x1111
        self._reader_task: Optional[asyncio.Task] = None
        self._pipeline_sem = asyncio.Semaphore(self.MAX_PIPELINE)

    # ---- context manager ------------------------------------------------

    async def __aenter__(self) -> "KVClient":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ---- connection ------------------------------------------------------

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, self._protocol = await serial_asyncio.create_serial_connection(
            loop, _FrameReader, self._port, baudrate=self._baud
        )
        # Synchronization delimiter: clear host's and board's frame state
        self._transport.write(b"\x00\x00")
        await asyncio.sleep(0.5)
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._transport:
            self._transport.close()

    # ---- internal --------------------------------------------------------

    def _next_id(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        # ID 0 is reserved for framing errors from the device
        if self._seq == 0:
            self._seq = 1
        return self._seq

    def _send_raw(self, payload: bytes) -> None:
        self._transport.write(cobs_encode(payload))

    async def _reader_loop(self) -> None:
        """Continuously read response packets and resolve pending futures."""
        while True:
            try:
                pkt = await self._protocol.read_packet()
            except asyncio.CancelledError:
                return

            if len(pkt) < 3:
                log.warning("Response too short (%d bytes) — dropped", len(pkt))
                continue

            req_id = struct.unpack_from("<H", pkt, 0)[0]
            fut    = self._pending.pop(req_id, None)
            if fut is None:
                log.warning("Received response for unknown ID %d — dropped", req_id)
                continue

            if not fut.cancelled():
                fut.set_result(pkt[2:])     # strip ID, pass status + optional val

    async def _request(self, payload: bytes) -> bytes:
        """Send a single framed request and await its response."""
        async with self._pipeline_sem:
            req_id = self._next_id()
            loop   = asyncio.get_running_loop()
            fut    = loop.create_future()
            self._pending[req_id] = fut

            # Prepend the 2-byte little-endian request ID
            self._send_raw(struct.pack("<H", req_id) + payload)
            try:
                return await asyncio.wait_for(fut, timeout=2.0)
            except asyncio.TimeoutError:
                self._pending.pop(req_id, None)
                raise TimeoutError(f"Request {req_id} timed out after 2s")

    # ---- public API ------------------------------------------------------

    @staticmethod
    def _format_key(key: bytes | str) -> bytes:
        if isinstance(key, str):
            key = key.encode("utf-8")
        if len(key) > 4:
            return key[:4]
        return key.ljust(4, b"\x00")

    @staticmethod
    def _format_val(val: bytes | str) -> bytes:
        if isinstance(val, str):
            val = val.encode("utf-8")
        if len(val) > 16:
            return val[:16]
        return val.ljust(16, b"\x00")

    async def create(self, key: bytes | str, val: bytes | str) -> bool:
        """
        Store or overwrite a key-value pair.
        Returns True on success, False if the table is full.
        Automatically pads or truncates keys to 4 bytes and values to 16 bytes.
        """
        key = self._format_key(key)
        val = self._format_val(val)
        resp = await self._request(b"c" + key + val)
        return resp[0:1] == b"K"

    async def read(self, key: bytes | str) -> Optional[bytes]:
        """
        Read a value by key.
        Returns the 16-byte value, or None if not found.
        Automatically pads or truncates keys to 4 bytes.
        """
        key = self._format_key(key)
        resp = await self._request(b"r" + key)
        if resp[0:1] == b"K" and len(resp) >= 17:
            return resp[1:17]
        return None

    async def delete(self, key: bytes | str) -> bool:
        """
        Delete a key.
        Returns True if found and deleted, False if not found.
        Automatically pads or truncates keys to 4 bytes.
        """
        key = self._format_key(key)
        resp = await self._request(b"d" + key)
        return resp[0:1] == b"K"

    async def batch(self, ops: list[BatchOp]) -> list:
        """
        Execute up to MAX_BATCH operations in a single USB packet.

        ops: list of tuples:
            ("create", key: bytes, val: bytes)
            ("read",   key: bytes)
            ("delete", key: bytes)

        Returns a list of results in the same order:
            create  -> True (OK) | False (full)
            read    -> bytes (16-byte val) | None (not found)
            delete  -> True (deleted) | False (not found)
        """
        if not ops:
            return []
        if len(ops) > self.MAX_BATCH:
            raise ValueError(f"Batch limited to {self.MAX_BATCH} ops")

        # Build the batch payload: 'b' + COUNT + op data
        body = bytearray()
        body += b"b"
        body += struct.pack("B", len(ops))
        for op in ops:
            cmd = op[0]
            if cmd == "create":
                _, key, val = op
                key = self._format_key(key)
                val = self._format_val(val)
                body += b"c" + key + val
            elif cmd == "read":
                _, key = op
                key = self._format_key(key)
                body += b"r" + key
            elif cmd == "delete":
                _, key = op
                key = self._format_key(key)
                body += b"d" + key
            else:
                raise ValueError(f"Unknown batch op: {cmd!r}")

        resp = await self._request(bytes(body))
        # resp: STATUS[1]('K') COUNT[1] (per-op STATUS[1] [VAL[16]])...
        if len(resp) < 2 or resp[0:1] != b"K":
            raise IOError(f"Batch failed with status {resp[0:1]!r}")

        count  = resp[1]
        offset = 2
        results = []
        for i in range(count):
            if offset >= len(resp):
                results.append(None)
                continue
            op_status = resp[offset:offset+1]
            offset   += 1
            cmd       = ops[i][0]
            if cmd == "create":
                results.append(op_status == b"K")
            elif cmd == "read":
                if op_status == b"K" and offset + 16 <= len(resp):
                    results.append(resp[offset:offset+16])
                    offset += 16
                else:
                    results.append(None)
            elif cmd == "delete":
                results.append(op_status == b"K")

        return results

    async def get_help(self) -> str:
        """
        Fetch the human-readable help string from the device.
        """
        resp = await self._request(b"?")
        if resp[0:1] == b"K":
            return resp[1:].decode("ascii", errors="replace")
        return "Failed to retrieve help text."


# ---------------------------------------------------------------------------
# Interactive Shell
# ---------------------------------------------------------------------------

import shlex

async def interactive_shell(kv: KVClient) -> None:
    print("KV Store Interactive Shell.")
    print("Type 'help' for commands, 'exit' to quit.")
    loop = asyncio.get_running_loop()
    
    while True:
        try:
            line = await loop.run_in_executor(None, input, "kv> ")
        except EOFError:
            break
        
        line = line.strip()
        if not line:
            continue
            
        try:
            args = shlex.split(line)
        except ValueError as e:
            print(f"Error parsing command: {e}")
            continue
            
        cmd = args[0].lower()
        
        if cmd in ("exit", "quit"):
            break
        elif cmd == "help":
            print("Commands:")
            print("  c <key> <value>  - Create/Update a new key-value pair")
            print("  r <key>          - Read a value by key")
            print("  d <key>          - Delete a key")
            print("  b <ops...>       - Batch ops (e.g. b c k1 v1 r k1 d k1)")
            print("  ?                - Get help text from the USB device")
            print("  exit / quit      - Exit the shell")
        elif cmd == "?":
            res = await kv.get_help()
            print(res)
        elif cmd == "c":
            if len(args) < 3:
                print("Usage: c <key> <val>")
                continue
            key, val = args[1], args[2]
            res = await kv.create(key, val)
            print("Success" if res else "Table full / Error")
        elif cmd == "r":
            if len(args) < 2:
                print("Usage: r <key>")
                continue
            key = args[1]
            res = await kv.read(key)
            if res is not None:
                # Decode explicitly formatted strings, assuming padded with null bytes
                res_str = res.rstrip(b'\x00').decode('utf-8', errors='replace')
                print(f"[{args[1]}] = {res_str}  (Raw: {res})")
            else:
                print("Not found")
        elif cmd == "d":
            if len(args) < 2:
                print("Usage: d <key>")
                continue
            key = args[1]
            res = await kv.delete(key)
            print("Deleted" if res else "Not found")
        elif cmd == "b":
            ops = []
            idx = 1
            while idx < len(args):
                op_type = args[idx].lower()
                if op_type == "c":
                    if idx + 2 >= len(args):
                        print("Error: 'c' inside batch requires <key> and <val>")
                        break
                    ops.append(("create", args[idx+1], args[idx+2]))
                    idx += 3
                elif op_type in ("r", "d"):
                    if idx + 1 >= len(args):
                        print(f"Error: '{op_type}' inside batch requires <key>")
                        break
                    op_name = "read" if op_type == "r" else "delete"
                    ops.append((op_name, args[idx+1]))
                    idx += 2
                else:
                    print(f"Error: Unknown batch op '{op_type}'. Use c, r, or d.")
                    break
            else:
                if ops:
                    try:
                        res = await kv.batch(ops)
                        # Clean up formatting for pretty printing byte strings
                        fmt_res = []
                        for r in res:
                            if isinstance(r, bytes):
                                fmt_res.append(r.rstrip(b'\\x00').decode('utf-8', errors='replace'))
                            else:
                                fmt_res.append(r)
                        print(f"Batch results: {fmt_res} (Raw: {res})")
                    except Exception as e:
                        print(f"Batch failed: {e}")
                else:
                    print("Usage: b <ops...>. Example: b c key1 val1 r key1 d key1")
        else:
            print(f"Unknown command: {cmd}")

async def _main(port: str) -> None:
    logging.basicConfig(level=logging.WARNING)
    print(f"Connecting to {port} ...")
    try:
        async with KVClient(port) as kv:
            await interactive_shell(kv)
    except Exception as e:
        print(f"Could not connect to {port}: {e}")
        print("Ensure no other application (Arduino IDE, Serial Monitor) is using the port.")
        sys.exit(1)

if __name__ == "__main__":
    import sys
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    asyncio.run(_main(port))

