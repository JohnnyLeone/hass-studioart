"""Regression tests for the group-2/3 command scanner.

An early version of `scan` skipped only *known* sets. Group 3 turned out to
contain undocumented triplets, and because an empty-payload set writes 0, a
real scan of a live A100 silently zeroed two settings (get 0x06 was 6 -> 0,
get 0x10 was 1 -> 0). The scanner now infers sets from the triplet layout
(get=N, reply=N+1, set=N+2) and refuses to probe them.
"""

from __future__ import annotations

import json
import socket
import struct
import threading

# Triplets as observed on a real A100 during the scan that caused the incident.
DEVICE_TRIPLETS = {0x06: 6, 0x09: 0, 0x0C: 0, 0x10: 1}


def _fake_speaker(cli, state, writes):
    """A minimal port-50007 speaker with triplets and cross-client chatter."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)

    background = [(2, 0x38, json.dumps({"Name": "x"}).encode())]

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                try:
                    while True:
                        hdr = cli._recvn(conn, 2)
                        if len(hdr) < 2:
                            break
                        ln = struct.unpack(">H", hdr)[0]
                        body = cli._recvn(conn, ln)
                        group = struct.unpack(">H", body[0:2])[0]
                        cmd, payload = body[2], body[3:]
                        out = []
                        if group == 3 and cmd in state:
                            val = {"value": state[cmd], "ID": ""}
                            out.append((3, cmd + 1, json.dumps(val).encode()))
                        elif group == 3 and (cmd - 2) in state:
                            base = cmd - 2
                            state[base] = payload[0] if payload else 0
                            writes.append((cmd, state[base]))
                            val = {"value": state[base], "ID": ""}
                            out.append((3, base + 1, json.dumps(val).encode()))
                        out.extend(background)
                        for og, oc, op in out:
                            b = struct.pack(">H", og) + bytes([oc]) + op
                            conn.sendall(struct.pack(">H", len(b)) + b)
                except Exception:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


def test_scan_never_writes_to_undocumented_triplets(cli, capsys):
    state = dict(DEVICE_TRIPLETS)
    writes: list = []
    cli.PORT = _fake_speaker(cli, state, writes)

    cli.scan("127.0.0.1", 3, 0x00, 0x20, delay=0, timeout=0.05)
    out = capsys.readouterr().out

    assert writes == [], f"scan wrote to the device: {writes}"
    assert state == DEVICE_TRIPLETS, "scan changed device state"
    # the sets that the real incident hit must be explicitly skipped
    for set_cmd, get_cmd in ((0x08, 0x06), (0x12, 0x10)):
        assert f"{set_cmd:#04x}  (skipped: inferred SET of the {get_cmd:#04x}" in out


def test_scan_filters_other_clients_traffic(cli, capsys):
    """The speaker fans replies to every connection; that isn't our data."""
    state = dict(DEVICE_TRIPLETS)
    cli.PORT = _fake_speaker(cli, state, [])
    cli.scan("127.0.0.1", 3, 0x00, 0x10, delay=0, timeout=0.05)
    out = capsys.readouterr().out
    assert "background frames from other clients" in out
    # group-2 chatter must never be attributed to a group-3 probe
    assert "group=2" not in out
