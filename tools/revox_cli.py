#!/usr/bin/env python3
"""Standalone CLI for the Revox STUDIOART A100/S100 control protocol.

No Home Assistant required. Use it to verify control and to identify commands
that don't yet have a confirmed mapping.

Examples
--------
    python3 revox_cli.py 192.168.42.163 status
    python3 revox_cli.py 192.168.42.163 volume 40
    python3 revox_cli.py 192.168.42.163 source aux
    python3 revox_cli.py 192.168.42.163 play | pause | standby | power
    python3 revox_cli.py 192.168.42.163 loudness 1        # binary set 0x36
    python3 revox_cli.py 192.168.42.163 aux-trigger 0     # set 0x9E inverted
    python3 revox_cli.py 192.168.42.163 aux-sens 1        # binary set 0x43
    python3 revox_cli.py 192.168.42.163 lrswap 0          # binary set 0x62
    python3 revox_cli.py 192.168.42.163 autopoweron 1     # binary set 0x5B
    python3 revox_cli.py 192.168.42.163 poweronsrc 0      # binary set 0x9B
    python3 revox_cli.py 192.168.42.163 bassboost 1
    python3 revox_cli.py 192.168.42.163 channel left      # SETLEFT via port 7777
    python3 revox_cli.py 192.168.42.163 cmd "volume 55"   # raw `cmd ...`
    python3 revox_cli.py 192.168.42.163 raw SETSTEREO     # bare ASCII on 50007
    python3 revox_cli.py 192.168.42.163 get 2 0x34        # binary get
    python3 revox_cli.py 192.168.42.163 bin 2 0x5b 1      # binary set
    python3 revox_cli.py 192.168.42.163 readq READ_fwdownload_xml   # 0xD0 query
    python3 revox_cli.py 192.168.42.163 watch             # live push events (7777)
"""
import json
import socket
import struct
import sys

PORT = 50007
EVENT_PORT = 7777

# (group, get, reply) for the `status` verb
READS = {
    "device": (2, 0x37, 0x38),
    "playback": (2, 0x3C, 0x3D),
    "multiroom": (3, 0x03, 0x04),
    "loudness": (2, 0x34, 0x35),
    "aux_high_sens": (2, 0x41, 0x42),
    "kleernet": (3, 0x56, 0x57),
}

TOGGLES = {  # verb -> (group, set_cmd)
    "loudness": (2, 0x36),
    "aux-sens": (2, 0x43),
    "autopoweron": (2, 0x5B),
    "lrswap": (2, 0x62),
    "poweronsrc": (2, 0x9B),
    # "disable auto aux": 1 turns the Aux-In trigger OFF. Prefer the
    # `aux-trigger` verb which handles the inversion for you.
    "disautoaux": (2, 0x9E),
}


def build_frame(group: int, cmd: int, payload: bytes = b"") -> bytes:
    body = struct.pack(">H", group) + bytes([cmd]) + payload
    return struct.pack(">H", len(body)) + body


def read_frame(sock: socket.socket):
    hdr = _recvn(sock, 2)
    if len(hdr) < 2:
        return None
    length = struct.unpack(">H", hdr)[0]
    body = _recvn(sock, length)
    if len(body) < 3:
        return None
    group = struct.unpack(">H", body[0:2])[0]
    cmd = body[2]
    return group, cmd, body[3:]


def _recvn(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _decode(payload: bytes):
    if not payload:
        return None
    try:
        return json.loads(payload.decode())
    except Exception:
        return payload[0] if len(payload) == 1 else payload.hex()


def request(host: str, group: int, req_cmd: int, reply_cmd: int):
    with socket.create_connection((host, PORT), timeout=4) as s:
        s.sendall(build_frame(group, req_cmd))
        for _ in range(8):
            frame = read_frame(s)
            if not frame:
                break
            g, c, payload = frame
            if c == reply_cmd:
                return _decode(payload)
    return None


def get_status(host: str) -> dict:
    status = {name: request(host, *spec) for name, spec in READS.items()}
    # Aux-In trigger is the inverse of "DisAutoAux" (verified on device)
    kleer = status.get("kleernet") or {}
    if isinstance(kleer, dict) and "DisAutoAux" in kleer:
        status["aux_trigger"] = 0 if kleer["DisAutoAux"] else 1
    return status


def send_cmd(host: str, text: str) -> None:
    with socket.create_connection((host, PORT), timeout=4) as s:
        s.sendall(f"cmd {text}\r\n".encode())
        s.settimeout(1.0)
        try:
            print("reply:", s.recv(256).decode("utf-8", "replace").strip())
        except Exception:
            print("sent:", f"cmd {text}")


def send_raw(host: str, text: str) -> None:
    with socket.create_connection((host, PORT), timeout=4) as s:
        s.sendall(f"{text}\r\n".encode())
        s.settimeout(1.0)
        try:
            print("reply:", s.recv(256).decode("utf-8", "replace").strip())
        except Exception:
            print("sent:", text)


def send_bin(host: str, group: int, cmd: int, value: int) -> None:
    with socket.create_connection((host, PORT), timeout=4) as s:
        s.sendall(build_frame(group, cmd, bytes([value & 0xFF])))
        s.settimeout(1.5)
        try:
            frame = read_frame(s)
            if frame:
                g, c, payload = frame
                print(f"ack: group={g} cmd=0x{c:02x} value={_decode(payload)!r}")
            else:
                print("no ack")
        except Exception:
            print("sent bin:", group, hex(cmd), value)


# -- event channel (port 7777) ------------------------------------------------
def build_event_frame(op: int, payload: bytes = b"", version: int = 0x02) -> bytes:
    # client -> speaker: [00 00 VV][OP][00 00 00 00][len LE][payload]
    return (
        bytes([0x00, 0x00, version, op])
        + b"\x00\x00\x00\x00"
        + struct.pack("<H", len(payload))
        + payload
    )


def read_event_frame(sock: socket.socket):
    # speaker -> client: [00 00 VV 00][OP][ST][crc16][len16 BE][payload]
    hdr = _recvn(sock, 10)
    if len(hdr) < 10:
        return None
    op, status = hdr[4], hdr[5]
    length = struct.unpack(">H", hdr[8:10])[0]
    payload = _recvn(sock, length) if length else b""
    return op, status, payload


def event_connect(host: str) -> socket.socket:
    s = socket.create_connection((host, EVENT_PORT), timeout=4)
    s.sendall(build_event_frame(0x03))  # subscribe handshake
    return s


def event_ascii(host: str, text: str) -> None:
    """Send a bare ASCII command via the event channel (op 0x6A)."""
    with event_connect(host) as s:
        s.sendall(build_event_frame(0x6A, text.encode()))
        s.settimeout(3.0)
        try:
            while True:
                frame = read_event_frame(s)
                if not frame:
                    break
                op, status, payload = frame
                print(_fmt_event(op, status, payload))
                if op == 0x6A:  # ack received
                    break
        except socket.timeout:
            pass


def event_query(host: str, text: str) -> None:
    """READ_* query via op 0xD0 (e.g. READ_fwdownload_xml)."""
    with event_connect(host) as s:
        s.sendall(build_event_frame(0xD0, text.encode()))
        s.settimeout(4.0)
        try:
            while True:
                frame = read_event_frame(s)
                if not frame:
                    break
                op, status, payload = frame
                if op == 0xD0:
                    print(payload.decode("utf-8", "replace"))
                    return
        except socket.timeout:
            print("no reply")


def _fmt_event(op: int, status: int, payload: bytes) -> str:
    label = {
        0x03: "handshake",
        0x40: "volume",
        0x67: "channel-status",
        0x6A: "ascii-ack",
        0x70: "mirror",
        0xD0: "query-reply",
    }.get(op, f"op 0x{op:02x}")
    if op == 0x70 and len(payload) >= 5:
        length = struct.unpack(">H", payload[0:2])[0]
        body = payload[2 : 2 + length]
        group = struct.unpack(">H", body[0:2])[0]
        cmd = body[2]
        data = body[3:]
        return (
            f"[mirror] group={group} cmd=0x{cmd:02x}"
            + (f" value={_decode(data)!r}" if data else " (get)")
        )
    text = payload.decode("utf-8", "replace") if payload else ""
    return f"[{label}] status={status}" + (f" {text}" if text else "")


def watch(host: str) -> None:
    """Subscribe to the push/event channel and print every state change."""
    print("watching push events on port 7777 (Ctrl-C to stop)...")
    with event_connect(host) as s:
        s.sendall(build_event_frame(0x40, version=0x01))  # like the app does
        s.settimeout(None)
        while True:
            frame = read_event_frame(s)
            if not frame:
                print("connection closed")
                return
            print(_fmt_event(*frame))


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    host, verb, *rest = sys.argv[1:]

    if verb == "status":
        print(json.dumps(get_status(host), indent=2, ensure_ascii=False))
    elif verb == "volume":
        send_cmd(host, f"volume {int(rest[0])}")
    elif verb in ("volup", "voldown", "play", "pause", "standby", "power"):
        send_cmd(host, verb if verb != "standby" else "timerstandby")
    elif verb == "source":
        send_cmd(host, f"source {rest[0]}")
    elif verb == "aux-trigger":
        # wire command is "disable auto aux" -> inverted
        send_bin(host, 2, 0x9E, 0 if int(rest[0], 0) else 1)
    elif verb in TOGGLES:
        group, cmd = TOGGLES[verb]
        send_bin(host, group, cmd, int(rest[0], 0))
    elif verb == "bassboost":
        send_cmd(host, f"basssboost {int(rest[0])}")
    elif verb == "url":
        send_cmd(host, f"url {rest[0]}")
    elif verb == "maxvolume":
        send_cmd(host, f"maxvolume {int(rest[0])}")
    elif verb == "channel":
        event_ascii(
            host,
            {"stereo": "SETSTEREO", "left": "SETLEFT", "right": "SETRIGHT"}[rest[0].lower()],
        )
    elif verb == "cmd":
        send_cmd(host, rest[0])
    elif verb == "raw":
        send_raw(host, rest[0])
    elif verb == "get":
        group, cmd = int(rest[0], 0), int(rest[1], 0)
        print(request(host, group, cmd, cmd + 1))
    elif verb == "bin":
        send_bin(host, int(rest[0], 0), int(rest[1], 0), int(rest[2], 0))
    elif verb == "readq":
        event_query(host, rest[0])
    elif verb == "watch":
        watch(host)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
