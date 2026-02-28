/*
 * Uno R4 KV Store — Pipelined Binary Protocol over USB-CDC
 *
 * ==========================================================================
 * PACKET FRAMING: COBS (Consistent Overhead Byte Stuffing)
 *   - 0x00 is used exclusively as the end-of-packet delimiter
 *   - Any dropped/corrupted byte causes at most one packet loss; the stream
 *     resyncs automatically on the next 0x00 delimiter
 *
 * REQUEST  (host -> device): COBS({ ID[2] CMD[1] KEY[4] VAL[16 if 'c'] })
 * RESPONSE (device -> host): COBS({ ID[2] STATUS[1] VAL[16 if read-hit] })
 *
 * CMD bytes:  'c' = create/update   'r' = read   'd' = delete   'b' = batch
 *
 * STATUS bytes:
 *   'K' = success / found
 *   'F' = table full (create only)
 *   '?' = not found / unknown command
 *
 * BATCH command body (after ID + 'b'):
 *   COUNT[1]  followed by COUNT × single-op packets (no ID, no framing)
 *   Each op: CMD[1] KEY[4] VAL[16 if 'c']
 *   Batch response: ID[2] 'K' COUNT[1] followed by COUNT × STATUS[1] bytes
 *   (read hits in a batch also append 16 val bytes after that op's STATUS)
 *
 * Pipeline depth: host may have up to MAX_PIPELINE requests in-flight.
 * ==========================================================================
 */

#include <Arduino.h>
#include <string.h>

extern "C" {
uint8_t kv_create(const char *key, const char *val);
uint8_t kv_read(const char *key, char *out_val);
uint8_t kv_delete(const char *key);
}

// ---------------------------------------------------------------------------
// COBS encode/decode
// ---------------------------------------------------------------------------

// Encode `len` bytes of `src` into `dst`.
// dst must be at least len+2 bytes (overhead byte + data + 0x00 delimiter).
// Returns number of bytes written to dst (including trailing 0x00).
static uint16_t cobs_encode(const uint8_t *src, uint16_t len, uint8_t *dst) {
  uint8_t *code_ptr = dst;
  uint8_t code = 1;
  uint8_t *out = dst + 1;
  uint16_t out_len = 1;

  for (uint16_t i = 0; i < len; i++) {
    if (src[i] == 0x00) {
      *code_ptr = code;
      code_ptr = out++;
      code = 1;
      out_len++;
    } else {
      *out++ = src[i];
      out_len++;
      code++;
      if (code == 0xFF) {
        *code_ptr = code;
        code_ptr = out++;
        code = 1;
        out_len++;
      }
    }
  }
  *code_ptr = code;
  *out = 0x00;
  return out_len + 1;
}

static uint16_t cobs_decode(const uint8_t *src, uint16_t len, uint8_t *dst) {
  if (len == 0)
    return 0;
  uint16_t out_len = 0;
  uint16_t i = 0;
  while (i < len) {
    uint8_t code = src[i++];
    if (code == 0)
      return 0;
    for (uint8_t j = 1; j < code; j++) {
      if (i >= len)
        return 0;
      dst[out_len++] = src[i++];
    }
    if (code < 0xFF && i < len) {
      dst[out_len++] = 0;
    }
  }
  return out_len;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

static const uint8_t MAX_PIPELINE = 32;
static const uint8_t MAX_BATCH = 16;

// Worst-case raw payload sizes:
//   Single create:  2 (ID) + 1 (cmd) + 4 (key) + 16 (val)  = 23 bytes
//   Batch:          2 + 1 + 1 (count) + 16*(1+4+16)         = 4 + 336 = 340
//   bytes
// COBS overhead: +1 byte per 254 bytes + 1 delimiter → negligible
static const uint16_t RX_BUF_SIZE = 1024;
static const uint16_t TX_BUF_SIZE = 1024;

// ---------------------------------------------------------------------------
// Buffers
// ---------------------------------------------------------------------------

static uint8_t rx_raw[RX_BUF_SIZE];
static uint16_t rx_raw_len = 0;

static uint8_t rx_decoded[RX_BUF_SIZE] __attribute__((aligned(4)));
static uint8_t tx_payload[TX_BUF_SIZE] __attribute__((aligned(4)));
static uint8_t tx_encoded[TX_BUF_SIZE] __attribute__((aligned(4)));

static char val_buf[16] __attribute__((aligned(4)));

#define VERSION "1.2.0"

// ---------------------------------------------------------------------------
// Response helpers
// ---------------------------------------------------------------------------

static void send_response(uint16_t req_id, uint8_t status, const uint8_t *val,
                          uint8_t val_len) {
  uint8_t payload_len = 0;
  tx_payload[payload_len++] = (uint8_t)(req_id & 0xFF);
  tx_payload[payload_len++] = (uint8_t)(req_id >> 8);
  tx_payload[payload_len++] = status;
  if (val && val_len > 0) {
    memcpy(tx_payload + payload_len, val, val_len);
    payload_len += val_len;
  }
  uint8_t enc_len = cobs_encode(tx_payload, payload_len, tx_encoded);
  Serial.write(tx_encoded, enc_len);
}

// ---------------------------------------------------------------------------
// Packet dispatch
// ---------------------------------------------------------------------------

static void dispatch(const uint8_t *pkt, uint16_t len) {
  if (len < 3)
    return;
  uint16_t req_id = (uint16_t)pkt[0] | ((uint16_t)pkt[1] << 8);
  uint8_t cmd = pkt[2];

  if (cmd == '?') {
    const char *help = "Uno R4 KV Store v" VERSION "\n"
                       "Commands:\n"
                       " c [k4][v16]: Create/Update\n"
                       " r [k4]:      Read\n"
                       " d [k4]:      Delete\n"
                       " b [n][ops]:  Batch up to 16\n"
                       " ?:           Help text";
    send_response(req_id, 'K', (const uint8_t *)help, strlen(help));
    return;
  }

  // Unified loop: treat single ops as batch-of-1
  uint8_t count = 1;
  uint16_t op_ptr = 3;
  if (cmd == 'b') {
    if (len < 4) {
      send_response(req_id, '?', nullptr, 0);
      return;
    }
    count = pkt[3];
    op_ptr = 4;
  }
  if (count > MAX_BATCH)
    count = MAX_BATCH;

  uint16_t rlen = 0;
  tx_payload[rlen++] = (uint8_t)(req_id & 0xFF);
  tx_payload[rlen++] = (uint8_t)(req_id >> 8);
  tx_payload[rlen++] = 'K';
  if (cmd == 'b')
    tx_payload[rlen++] = count;

  for (uint8_t i = 0; i < count; i++) {
    if (op_ptr >= len)
      break;
    uint8_t c = (cmd == 'b') ? pkt[op_ptr++] : cmd;
    const char *k = (const char *)(pkt + op_ptr);
    if (op_ptr + 4 > len)
      break;
    op_ptr += 4;

    if (c == 'c') {
      if (op_ptr + 16 > len)
        break;
      const char *v = (const char *)(pkt + op_ptr);
      op_ptr += 16;
      uint8_t r = kv_create(k, v);
      if (cmd == 'b')
        tx_payload[rlen++] = r ? 'K' : 'F';
      else
        tx_payload[2] = r ? 'K' : 'F';
    } else if (c == 'r') {
      uint8_t r = kv_read(k, val_buf);
      if (cmd == 'b') {
        tx_payload[rlen++] = r ? 'K' : '?';
        if (r) {
          memcpy(tx_payload + rlen, val_buf, 16);
          rlen += 16;
        }
      } else {
        tx_payload[2] = r ? 'K' : '?';
        if (r) {
          memcpy(tx_payload + 3, val_buf, 16);
          rlen = 19;
        }
      }
    } else if (c == 'd') {
      uint8_t r = kv_delete(k);
      if (cmd == 'b')
        tx_payload[rlen++] = r ? 'K' : '?';
      else
        tx_payload[2] = r ? 'K' : '?';
    }
    if (cmd != 'b')
      break;
  }

  uint16_t enc_len = cobs_encode(tx_payload, rlen, tx_encoded);
  Serial.write(tx_encoded, enc_len);
}

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(1000000);
  delay(100);

  // Explicitly zero the hash table (Status = 0 = Empty)
  extern char hash_table[];
  memset(hash_table, 0, 256 * 32);

  // Sync
  Serial.write((uint8_t)0x00);

  rx_raw_len = 0;
}

void loop() {
  // Drain whatever is available this iteration
  while (Serial.available()) {
    uint8_t b = (uint8_t)Serial.read();

    if (b == 0x00) {
      // End-of-packet delimiter: decode and dispatch
      if (rx_raw_len > 0) {
        uint8_t dec_len = cobs_decode(rx_raw, rx_raw_len, rx_decoded);
        if (dec_len > 0) {
          dispatch(rx_decoded, dec_len);
        }
        rx_raw_len = 0;
      }
    } else {
      // Accumulate; guard against overflow (resync by dropping frame)
      if (rx_raw_len < RX_BUF_SIZE) {
        rx_raw[rx_raw_len++] = b;
      } else {
        rx_raw_len = 0; // overflow: drop and wait for next 0x00
      }
    }
  }
}
