"""複数の VPN を 1 つのポートの裏で回す中継。コンテナ 1 つで完結する。

    利用側 ─HTTP POST─▶ :8545 ─┬─ host の出口 ─────────────HTTPS─▶ 転送先
                               ├─ tun0 (VPN: 東京)  ─────HTTPS─▶ 転送先
                               └─ tun1 (VPN: 渋谷) ...

- コンテナ内で OpenVPN を EXITS-1 本起動する (tunnels.py)。VPN の接続は張りっぱなし
- リクエストごとに、次に送ってよい時刻が最も早い出口を選ぶ。出口ごとに流量 (RATE) を守る
- 各出口への送信は、そのトンネルの tun デバイスに結びつけたソケットで行う。
  トンネルごとに経路表を分けているので、結びつけた通信だけがそのトンネルを通る
- 各出口の HTTPS 接続は使い回す (VPN 越しの TLS 確立を毎回やると流量の天井になる)
- 429 / 5xx / 本文が JSON でない / 接続失敗 なら、その出口をしばらく休ませ、別の出口でやり直す

設定は環境変数 (compose の environment) か引数で渡す。

    EXITS=20  RATE=6  LISTEN=0.0.0.0:8545  UPSTREAM=https://...  REGIONS=japan_-_tokyo,...  INCLUDE_HOST=1
    OPENVPN_USERNAME / OPENVPN_PASSWORD

GET / で出口とトンネルの状態が見られる。
PROXY_LISTEN (既定 0.0.0.0:8118) では汎用の HTTP プロキシも受ける。VPN の出口をラウンドロビンで使う (proxy.py)。
"""
import argparse
import json
import logging
import os
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection

logger = logging.getLogger("gateway")

UPSTREAM = os.environ.get("UPSTREAM", "https://mainnet.gateway.tenderly.co")
IP_ECHO = os.environ.get("IP_ECHO", "https://api.ipify.org")


def session_for(device: str = None, size: int = 64) -> requests.Session:
    """device (tun0 など) に結びつけたソケットで送る Session。接続は使い回す。"""
    opts = list(HTTPConnection.default_socket_options)
    if device:
        opts.append((socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode()))

    class _Adapter(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            kw["socket_options"] = opts
            super().init_poolmanager(*a, **kw)

    s = requests.Session()
    ad = _Adapter(pool_connections=size, pool_maxsize=size, max_retries=0)
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


def egress_of(device: str = None, timeout: float = 10.0):
    """その出口から見た自分の IP。取れなければ None。"""
    try:
        return session_for(device, size=1).get(IP_ECHO, timeout=timeout).text.strip() or None
    except requests.RequestException:
        return None


class Exit:
    def __init__(self, name: str, egress: str, session: requests.Session, target: str = None, device: str = None):
        self.name, self.egress, self.session, self.target = name, egress, session, target
        self.device = device        # tun0 など。ホストは None
        self.next_at = 0.0          # 次に送ってよい時刻
        self.cool_until = 0.0       # 休ませている期限
        self.fails = 0              # 連続失敗
        self.sent = self.ok = self.bad = 0
        self.last_error = None
        self.errors = {}


class Pool:
    """出口の選択と流量。スレッドから同時に呼ばれる。"""

    def __init__(self, exits, rate: float, cooldown_max: float = 60.0, clock=time.monotonic, sleep=time.sleep):
        self.exits = list(exits)
        self.interval = 1.0 / rate
        self.cooldown_max = cooldown_max
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()

    def acquire(self, exclude=()):
        """送ってよい出口を 1 つ予約して返す。空きが出るまで待つ。出口が 0 本なら現れるまで待つ。"""
        while True:
            with self.lock:
                now = self.clock()
                if not self.exits:
                    wait = 1.0
                else:
                    cands = [e for e in self.exits if e not in exclude] or list(self.exits)
                    e = min(cands, key=lambda x: max(x.next_at, x.cool_until))
                    ready = max(e.next_at, e.cool_until)
                    if ready <= now:
                        e.next_at = now + self.interval
                        e.sent += 1
                        return e
                    wait = min(ready - now, 0.5)
            self.sleep(wait)

    def success(self, e):
        with self.lock:
            e.ok += 1
            e.fails = 0

    def failure(self, e, reason: str = "?"):
        with self.lock:
            e.last_error = reason
            key = reason.split(" ", 1)[0]
            e.errors[key] = e.errors.get(key, 0) + 1
            e.bad += 1
            e.fails += 1
            e.cool_until = self.clock() + min(self.cooldown_max, 2.0 ** e.fails)

    def refresh(self, found):
        """出口の一覧を入れ替える。出口 IP が同じものは状態 (流量・休み) を引き継ぐ。"""
        with self.lock:
            cur = {e.egress: e for e in self.exits}
            nxt, seen = [], set()
            for e in found:
                if e.egress in seen:
                    continue
                seen.add(e.egress)
                keep = cur.get(e.egress)
                if keep is not None:
                    keep.name, keep.session, keep.target, keep.device = e.name, e.session, e.target, e.device
                    nxt.append(keep)
                else:
                    nxt.append(e)
            added = [e.egress for e in nxt if e.egress not in cur]
            gone = [g for g in cur if g not in seen]
            self.exits = nxt
            return added, gone

    def status(self):
        now = self.clock()
        with self.lock:
            return [{"name": e.name, "egress": e.egress, "sent": e.sent, "ok": e.ok, "bad": e.bad,
                     "cooling_s": round(max(0.0, e.cool_until - now), 1),
                     "errors": dict(e.errors), "last_error": e.last_error} for e in self.exits]


def _retryable(status: int, content: bytes) -> bool:
    if status == 429 or status >= 500:
        return True
    try:
        body = json.loads(content)
    except ValueError:
        return True
    err = body.get("error") if isinstance(body, dict) else None
    return bool(err) and "rate limit" in str(err.get("message", "")).lower()


def make_front(pool: Pool, upstream: str = UPSTREAM, retries: int = 8, timeout: float = 90.0, extra_status=None):
    class Front(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _reply(self, status, body: bytes, ctype="application/json"):
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            st = {"exits": pool.status()}
            if extra_status:
                st["tunnels"] = extra_status()
            self._reply(200, json.dumps(st, ensure_ascii=False).encode())

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            tried = []
            last = (502, b'{"gateway_error":"no attempt"}')
            for _ in range(retries + 1):
                n = len(pool.exits)
                e = pool.acquire(exclude=tried[-(n - 1):] if n > 1 else ())
                try:
                    r = e.session.post(e.target or upstream, data=body, timeout=timeout,
                                       headers={"content-type": "application/json"})
                    status, content = r.status_code, r.content
                except requests.RequestException as exc:
                    status, content = 502, json.dumps({"gateway_error": str(exc)[:200]}).encode()
                if not _retryable(status, content):
                    pool.success(e)
                    self._reply(status, content)
                    return
                pool.failure(e, f"{status} {content[:120].decode('utf-8', 'replace')}")
                tried.append(e)
                last = (status, content)
            self._reply(*last)

    return Front


def serve(handler, listen: str):
    host, port = listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), handler)
    srv.daemon_threads = True
    return srv


def main(argv=None):
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exits", type=int, default=int(env("EXITS", "20")), help="出口数 (ホストを含む)")
    ap.add_argument("--rate", type=float, default=float(env("RATE", "6")), help="出口ごとの毎秒リクエスト")
    ap.add_argument("--listen", default=env("LISTEN", "0.0.0.0:8545"))
    ap.add_argument("--upstream", default=UPSTREAM)
    ap.add_argument("--regions", default=env("REGIONS", ""), help="使う地域をカンマ区切りで (先頭から優先)")
    ap.add_argument("--proxy-listen", default=env("PROXY_LISTEN", "0.0.0.0:8118"), help="汎用 HTTP プロキシの待受。空で無効")
    ap.add_argument("--retries", type=int, default=int(env("RETRIES", "8")))
    ap.add_argument("--no-host", action="store_true", default=env("INCLUDE_HOST", "1") == "0",
                    help="ホスト自身を出口に含めない")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    exits = []
    if not args.no_host:
        eg = egress_of(None)
        if eg:
            exits.append(Exit("host", eg, session_for(None)))
        else:
            logger.warning("ホストの出口 IP を取れない。ホストは出口に含めない")
    host_exits = list(exits)
    pool = Pool(exits, rate=args.rate)

    mgr = None
    n_vpn = args.exits - len(host_exits)
    if n_vpn > 0:
        import tunnels
        user, pw = env("OPENVPN_USERNAME"), env("OPENVPN_PASSWORD")
        if not user or not pw:
            raise SystemExit("OPENVPN_USERNAME / OPENVPN_PASSWORD が無い")
        tunnels.write_auth(user, pw)
        preferred = [r for r in args.regions.split(",") if r] or tunnels.DEFAULT_REGIONS

        def on_change(up):
            found = host_exits + [Exit(t["name"], t["egress"], t["session"], device=t["dev"]) for t in up]
            added, gone = pool.refresh(found)
            logger.info("出口 %d 本 (+%s -%s)", len(pool.exits), added, gone)

        mgr = tunnels.Manager(n_vpn, tunnels.candidate_regions(preferred), on_change,
                              egress_of=egress_of, session_of=session_for)
        mgr.start()

        def stop(*_):
            mgr.stop()
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)

    srv = serve(make_front(pool, args.upstream, args.retries, extra_status=mgr.status if mgr else None), args.listen)
    logger.info("待受 %s / 出口 %d 本 (ホスト %d + VPN %d) / 出口ごと毎秒 %g / 転送先 %s",
                args.listen, args.exits, len(host_exits), max(n_vpn, 0), args.rate, args.upstream)
    if args.proxy_listen:
        import proxy
        psrv = proxy.serve(pool, args.proxy_listen)
        threading.Thread(target=psrv.serve_forever, daemon=True, name="proxy").start()
        logger.info("HTTP プロキシ待受 %s (VPN の出口をラウンドロビン)", args.proxy_listen)
    srv.serve_forever()


if __name__ == "__main__":
    main()
