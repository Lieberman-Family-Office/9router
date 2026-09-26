/**
 * Minimal RFC6455 WebSocket helpers for text frames (Responses WS JSON events).
 * Avoids a hard dependency on the `ws` package for the gateway hot-patch path.
 */

import { createHash, randomBytes } from "node:crypto";

const GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

export function acceptKey(secWebSocketKey) {
  return createHash("sha1").update(String(secWebSocketKey) + GUID).digest("base64");
}

/**
 * @param {import("node:http").IncomingMessage} req
 * @param {import("node:net").Socket} socket
 * @param {Buffer} head
 * @returns {boolean} true if handshake completed
 */
export function completeWsHandshake(req, socket, head) {
  const key = req.headers["sec-websocket-key"];
  const upgrade = String(req.headers.upgrade || "").toLowerCase();
  if (upgrade !== "websocket" || !key) {
    socket.write("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
    socket.destroy();
    return false;
  }
  const headers = [
    "HTTP/1.1 101 Switching Protocols",
    "Upgrade: websocket",
    "Connection: Upgrade",
    `Sec-WebSocket-Accept: ${acceptKey(key)}`,
    "",
    "",
  ].join("\r\n");
  socket.write(headers);
  if (head && head.length) socket.unshift(head);
  socket.setNoDelay(true);
  return true;
}

/** @param {string} text */
export function encodeTextFrame(text, { mask = false } = {}) {
  const payload = Buffer.from(String(text), "utf8");
  const len = payload.length;
  let header;
  if (len < 126) {
    header = Buffer.alloc(2);
    header[0] = 0x81;
    header[1] = (mask ? 0x80 : 0) | len;
  } else if (len < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x81;
    header[1] = (mask ? 0x80 : 0) | 126;
    header.writeUInt16BE(len, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x81;
    header[1] = (mask ? 0x80 : 0) | 127;
    header.writeUInt32BE(0, 2);
    header.writeUInt32BE(len, 6);
  }
  if (!mask) return Buffer.concat([header, payload]);
  const maskKey = randomBytes(4);
  const masked = Buffer.alloc(len);
  for (let i = 0; i < len; i++) masked[i] = payload[i] ^ maskKey[i % 4];
  return Buffer.concat([header, maskKey, masked]);
}

export function encodeCloseFrame(code = 1000, reason = "") {
  const msg = Buffer.from(reason, "utf8");
  const payload = Buffer.alloc(2 + msg.length);
  payload.writeUInt16BE(code, 0);
  msg.copy(payload, 2);
  const header = Buffer.alloc(2);
  header[0] = 0x88;
  header[1] = payload.length;
  return Buffer.concat([header, payload]);
}

/**
 * Incremental WS frame parser (client→server frames are masked).
 */
export class WsFrameReader {
  constructor() {
    this.buf = Buffer.alloc(0);
  }

  /** @param {Buffer} chunk */
  push(chunk) {
    this.buf = Buffer.concat([this.buf, chunk]);
    const frames = [];
    while (true) {
      const parsed = this.#tryParse();
      if (!parsed) break;
      frames.push(parsed);
    }
    return frames;
  }

  #tryParse() {
    if (this.buf.length < 2) return null;
    const b0 = this.buf[0];
    const b1 = this.buf[1];
    const opcode = b0 & 0x0f;
    const masked = (b1 & 0x80) !== 0;
    let len = b1 & 0x7f;
    let offset = 2;
    if (len === 126) {
      if (this.buf.length < 4) return null;
      len = this.buf.readUInt16BE(2);
      offset = 4;
    } else if (len === 127) {
      if (this.buf.length < 10) return null;
      const high = this.buf.readUInt32BE(2);
      const low = this.buf.readUInt32BE(6);
      if (high !== 0) {
        // oversized — drop connection by returning close
        this.buf = Buffer.alloc(0);
        return { opcode: 0x8, payload: Buffer.alloc(0), oversized: true };
      }
      len = low;
      offset = 10;
    }
    const maskLen = masked ? 4 : 0;
    if (this.buf.length < offset + maskLen + len) return null;
    let payload = this.buf.subarray(offset + maskLen, offset + maskLen + len);
    if (masked) {
      const mask = this.buf.subarray(offset, offset + 4);
      const out = Buffer.alloc(len);
      for (let i = 0; i < len; i++) out[i] = payload[i] ^ mask[i % 4];
      payload = out;
    }
    this.buf = this.buf.subarray(offset + maskLen + len);
    return { opcode, payload };
  }
}
