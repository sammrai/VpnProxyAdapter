"""汎用 HTTP プロキシ。出口は tun に結びつけず (device=None)、ローカルの偽の転送先で確かめる。"""
import socket
import threading
from http.server import BaseHTTPRequestHandler

import requests

import gateway
import proxy


def ex(name, device=None, cool=0.0):
    e = gateway.Exit(name, name, None, device=device)
    e.cool_until = cool
    return e


def _start(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_round_robin_rotates_vpn_exits_and_skips_cooling():
    host, a, b, c = ex("host"), ex("a", "tun0"), ex("b", "tun1", cool=99.0), ex("c", "tun2")
    rr = proxy.RoundRobin(gateway.Pool([host, a, b, c], rate=1.0, clock=lambda: 10.0))
    assert [rr.next().name for _ in range(4)] == ["a", "c", "a", "c"]
    assert rr.next(exclude=[a, c]) is b                 # 全部休みなら休み中でも使う
    assert proxy.RoundRobin(gateway.Pool([host], rate=1.0)).next() is None
    assert proxy.RoundRobin(gateway.Pool([host], rate=1.0), vpn_only=False).next() is host


def test_connect_via_binds_to_device(monkeypatch):
    calls = []

    class S:
        def __init__(self, *a):
            pass

        def setsockopt(self, *a):
            calls.append(a)

        def settimeout(self, t):
            pass

        def connect(self, addr):
            calls.append(("connect", addr))

        def close(self):
            pass

    monkeypatch.setattr(proxy.socket, "socket", S)
    proxy.connect_via("tun7", [("1.2.3.4", 443)], 1.0)
    assert calls == [(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tun7"), ("connect", ("1.2.3.4", 443))]


def test_http_request_is_forwarded_in_origin_form():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen["path"], seen["headers"] = self.path, dict(self.headers)
            self.send_response(200)
            self.send_header("content-length", "5")
            self.end_headers()
            self.wfile.write(b"hello")

    origin = _start(gateway.serve(H, "127.0.0.1:0"))
    px = _start(proxy.serve(gateway.Pool([ex("a")], rate=1.0), "127.0.0.1:0", vpn_only=False))
    try:
        s = requests.Session()
        s.trust_env = False
        r = s.get(f"http://127.0.0.1:{origin.server_address[1]}/x?y=1",
                  proxies={"http": f"http://127.0.0.1:{px.server_address[1]}"},
                  headers={"Proxy-Authorization": "secret"}, timeout=5)
        assert r.text == "hello"
        assert seen["path"] == "/x?y=1"
        assert seen["headers"]["Connection"] == "close" and "Proxy-Authorization" not in seen["headers"]
    finally:
        origin.shutdown()
        px.shutdown()


def test_connect_tunnels_bytes_sent_right_after_headers():
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen()

    def echo():
        c, _ = ls.accept()
        with c:
            while d := c.recv(1024):
                c.sendall(d.upper())
    threading.Thread(target=echo, daemon=True).start()
    px = _start(proxy.serve(gateway.Pool([ex("a")], rate=1.0), "127.0.0.1:0", vpn_only=False))
    try:
        with socket.create_connection(px.server_address, timeout=5) as c:
            c.sendall(b"CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: x\r\n\r\nping" % ls.getsockname()[1])
            want, got = b"HTTP/1.1 200 Connection established\r\n\r\nPING", b""
            while len(got) < len(want):
                d = c.recv(1024)
                assert d
                got += d
            assert got == want
    finally:
        px.shutdown()
        ls.close()


def test_no_vpn_exit_returns_503():
    px = _start(proxy.serve(gateway.Pool([ex("host")], rate=1.0), "127.0.0.1:0"))
    try:
        with socket.create_connection(px.server_address, timeout=5) as c:
            c.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
            assert c.recv(1024).startswith(b"HTTP/1.1 503")
    finally:
        px.shutdown()
