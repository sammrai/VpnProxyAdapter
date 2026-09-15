"""汎用の HTTP プロキシ。HTTP (絶対 URL) と HTTPS (CONNECT) を受け、接続ごとに VPN の出口をラウンドロビンで選ぶ。

    利用側 ─HTTP プロキシ─▶ :8118 ─┬─ tun0 (VPN) ─▶ インターネット
                                    ├─ tun1 (VPN) ─▶ ...

- 出口への接続は、そのトンネルの tun デバイスに結びつけたソケットで張る (gateway.py の中継と同じ仕組み)
- ホストの出口は使わない。VPN を通したつもりの通信がホストの IP で出ていかないように
- 出口を選ぶ単位は接続。HTTP は転送先に Connection: close を付けて 1 接続 1 リクエストにする
- 接続先の名前はコンテナの DNS で引く (トンネル越しには引かない)
- 休ませている出口 (中継で 429 などを受けたもの) は飛ばす。接続に失敗したら次の出口でやり直す
"""
import logging
import selectors
import socket
import socketserver
import threading
from urllib.parse import urlsplit

logger = logging.getLogger("gateway.proxy")

# 転送先へ渡さないヘッダ (hop-by-hop)
HOP_HEADERS = {b"connection", b"proxy-connection", b"keep-alive", b"proxy-authorization", b"te", b"trailer", b"upgrade"}
REASONS = {400: b"Bad Request", 502: b"Bad Gateway", 503: b"Service Unavailable"}


class RoundRobin:
    """出口を順番に配る。休ませている出口は飛ばす (全部休みなら休み中でも使う)。"""

    def __init__(self, pool, vpn_only: bool = True):
        self.pool, self.vpn_only = pool, vpn_only
        self.i = 0
        self.lock = threading.Lock()

    def next(self, exclude=()):
        exits = [e for e in self.pool.exits if (e.device or not self.vpn_only) and e not in exclude]
        if not exits:
            return None
        now = self.pool.clock()
        with self.lock:
            for k in range(len(exits)):
                e = exits[(self.i + k) % len(exits)]
                if e.cool_until <= now:
                    self.i += k + 1
                    return e
            e = exits[self.i % len(exits)]
            self.i += 1
            return e


def resolve(host: str, port: int):
    # トンネルは IPv4 の経路しか持たない
    return [ai[4] for ai in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)]


def connect_via(device, addrs, timeout: float):
    """device (tun0 など) に結びつけたソケットで addrs のどれかに繋ぐ。"""
    err = OSError("接続先のアドレスが無い")
    for addr in addrs:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if device:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode())
            s.settimeout(timeout)
            s.connect(addr)
            s.settimeout(None)
            return s
        except OSError as exc:
            s.close()
            err = exc
    raise err


def pipe(a, b, idle: float):
    """両方向に流す。片側が閉じたら相手の送信側を閉じる。両方閉じるか idle 秒無通信で終わる。"""
    peer = {a: b, b: a}
    with selectors.DefaultSelector() as sel:
        for s in (a, b):
            sel.register(s, selectors.EVENT_READ)
        while sel.get_map():
            ready = sel.select(idle)
            if not ready:
                return
            for key, _ in ready:
                s = key.fileobj
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    sel.unregister(s)
                    try:
                        peer[s].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                try:
                    peer[s].sendall(data)
                except OSError:
                    return


class Handler(socketserver.StreamRequestHandler):
    rbufsize = 0            # ヘッダの後ろに続くデータ (本文や TLS の最初の送信) を読み込み過ぎないように
    rr = None
    attempts = 3
    connect_timeout = 10.0
    idle_timeout = 300.0

    def handle(self):
        try:
            method, target, version = self.rfile.readline(8192).decode("latin-1").split()
        except ValueError:
            return self._error(400, "bad request line")
        headers = []
        while len(headers) < 200:
            h = self.rfile.readline(8192)
            if not h.strip():
                break
            headers.append(h.rstrip(b"\r\n"))

        connect = method.upper() == "CONNECT"
        if connect:
            host, _, port = target.rpartition(":")
            if not host or not port.isdigit():
                return self._error(400, "CONNECT は host:port で")
            host, port = host.strip("[]"), int(port)
        else:
            u = urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                return self._error(400, "http:// の絶対 URL だけ受ける")
            try:
                host, port = u.hostname, u.port or 80
            except ValueError:
                return self._error(400, "bad port")
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        try:
            addrs = resolve(host, port)
        except (OSError, ValueError, OverflowError) as exc:
            return self._error(502, f"名前が引けない {host}: {exc}")

        up = self._connect(addrs)
        if up is None:
            return
        with up:
            if connect:
                self.connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                head = [f"{method} {path} {version}".encode("latin-1")]
                head += [h for h in headers if h.split(b":", 1)[0].strip().lower() not in HOP_HEADERS]
                head.append(b"Connection: close")
                up.sendall(b"\r\n".join(head) + b"\r\n\r\n")
            pipe(self.connection, up, self.idle_timeout)

    def _connect(self, addrs):
        tried, last = [], "使える VPN の出口が無い"
        for _ in range(self.attempts):
            e = self.rr.next(exclude=tried)
            if e is None:
                break
            try:
                return connect_via(e.device, addrs, self.connect_timeout)
            except OSError as exc:
                tried.append(e)
                last = f"{e.name}: {exc}"
                logger.info("プロキシ接続失敗 %s", last)
        self._error(502 if tried else 503, last)
        return None

    def _error(self, status, msg):
        body = msg.encode("utf-8", "replace")
        try:
            self.connection.sendall(b"HTTP/1.1 %d %s\r\ncontent-type: text/plain; charset=utf-8\r\n"
                                    b"content-length: %d\r\nconnection: close\r\n\r\n%s"
                                    % (status, REASONS[status], len(body), body))
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


def serve(pool, listen: str, vpn_only: bool = True):
    host, port = listen.rsplit(":", 1)
    handler = type("ProxyHandler", (Handler,), {"rr": RoundRobin(pool, vpn_only)})
    return Server((host, int(port)), handler)
