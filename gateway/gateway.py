"""複数の VPN を 1 つのポートの裏で回す中継。コンテナ 1 つで完結する。

    利用側 ─HTTP POST─▶ :8545 ─┬─ host の出口 ─────────────HTTPS─▶ 転送先
                               ├─ tun0 (VPN: 東京)  ─────HTTPS─▶ 転送先
                               └─ tun1 (VPN: 渋谷) ...

- コンテナ内で OpenVPN を EXITS-1 本起動する (tunnels.py)。VPN の接続は張りっぱなし
- リクエストごとに、次に送ってよい時刻が最も早い出口を選ぶ。**流量は出口ごとに自分で見つける**
  (AIMD: 通り続ければ +1、429 で半分)。RATE はその初期値。出口ごとに上限が違うので、
  一律にするといちばん弱い出口に全体が引きずられる。
  **RATE は「毎秒リクエスト」ではなく「毎秒呼び出し」**。上流が数えているのは JSON-RPC の呼び出し数で、
  20 呼び出しのバッチを 1 件と数えると実効レートが 20 倍になって 429 を踏む
- **全出口が休んでいるときは待たずに 429 を返す** (ACQUIRE_WAIT 秒まで待つ)。黙ってブロックすると
  呼び出し側が減速できず、詰まりが見えないまま数十秒待たされる
- **429 が続き、かつ休ませる時間が張り直しのコスト (ROTATE_COST 秒) を超えたら、トンネルを張り直して
  IP を替える。** 制限は IP ごとなので、長く休ませるくらいなら替えたほうが早い。
  逆に**休みが短いうちに替えると損** — 張り直しのあいだ出口が丸ごと消えるため。必ず両者を比べる
- 各出口への送信は、そのトンネルの tun デバイスに結びつけたソケットで行う。
  トンネルごとに経路表を分けているので、結びつけた通信だけがそのトンネルを通る
- 各出口の HTTPS 接続は使い回す (VPN 越しの TLS 確立を毎回やると流量の天井になる)
- 429 / 5xx / 本文が JSON でない / 接続失敗 なら、その出口をしばらく休ませ、別の出口でやり直す
- トンネルの接続 (起動時・張り直し) は 2 本ずつ少しずつ (tunnels.py)。一斉に張ると家のルーターが混む
- **上流の 403 は「その IP が弾かれた」** (時間で戻らない)。別の出口でやり直し、続いたら張り直す

設定は環境変数 (compose の environment) か引数で渡す。

    EXITS=20  RATE=6  LISTEN=0.0.0.0:8545  UPSTREAM=https://...  REGIONS=japan_-_tokyo,...  INCLUDE_HOST=1
    OPENVPN_USERNAME / OPENVPN_PASSWORD  OPENVPN_PROVIDER=expressvpn (設定リポジトリのフォルダ名)

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
ACQUIRE_WAIT = 5.0    # 出口が空くのを待つ上限 (秒)。超えたら 429 を返して呼び出し側に減速させる
ROTATE_AFTER = 3      # 張り直しを考え始める連続 429 の回数 (これだけでは張り直さない。下の ROTATE_COST を見よ)
ROTATE_COST = 20.0    # トンネルを張り直す実コスト (秒)。実測で中央 13 / 90% 点 21 / 最大 22。
                      # **休ませる時間がこれより短いなら待つほうが安い** (張り直すと出口が丸ごと消える)
# 出口ごとに上限が違う (実測で 15 は全部通るが 25 だと 22% が 429)。一律の値では
# **いちばん弱い出口に全体を合わせる**ことになるので、出口ごとに自分で見つけさせる。
RATE_MIN, RATE_MAX = 5.0, 60.0    # 適応レートの下限・上限 (呼び出し/秒)
RATE_STEP = 1.0                   # 加算で増やす幅
RATE_PROBE = 200                  # この呼び出し数ぶん連続で通ったら 1 段上げる
RATE_BACKOFF = 0.7                # 減らすときに掛ける
# **429 のバーストは 1 回と数える。** 減速が効くまでに飛んでいた分がまとめて返ってくるので、
# 1 件ごとに掛けると数百 ms で下限まで落ちる (実測で 19 -> 2 になった)。
RATE_CUT_GAP = 2.0                # 前回の減速からこの秒数は、追加の 429 で下げない


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
        self.rate = None            # この出口の実効レート (呼び出し/秒)。None なら Pool の初期値
        self.ok_calls = 0           # 増速の判定に使う、直近で通った呼び出し数
        self.rate_cut_at = -1e9     # 最後に減速した時刻 (バーストを 1 回と数えるため)
        self.blocks = 0             # プロキシ側で「この IP が弾かれた」と分かった回数
        self.cool_until = 0.0       # 休ませている期限
        self.fails = 0              # 連続失敗
        self.rate_fails = 0         # 連続した 429 (張り直しの判断に使う)
        self.egress_left = None     # 上流が申告する、この IP の 1 日の応答量の残り (バイト)。Tenderly の x-tdly-egress-remaining
        self.sent = self.ok = self.bad = 0
        self.last_error = None
        self.errors = {}


class Pool:
    """出口の選択と流量。スレッドから同時に呼ばれる。"""

    def __init__(self, exits, rate: float, cooldown_max: float = 60.0, clock=time.monotonic, sleep=time.sleep,
                 on_rate_limited=None, rotate_after: int = ROTATE_AFTER, rotate_cost: float = ROTATE_COST):
        self.exits = list(exits)
        self.rate0 = rate           # 出口ごとのレートの初期値 (以後は AIMD で動く)
        self.interval = 1.0 / rate  # 初期値の間隔 (互換のため残す)
        self.cooldown_max = cooldown_max
        self.clock, self.sleep = clock, sleep
        self.on_rate_limited = on_rate_limited   # 429 が続いた出口の device を渡す (IP を替えてもらう)
        self.rotate_after = rotate_after         # 0 なら張り直さない (ROTATE_AFTER)
        self.rotate_cost = rotate_cost           # 休みがこれ以上に伸びて初めて張り直す
        self.lock = threading.Lock()

    def acquire(self, exclude=(), cost: int = 1, deadline: float = None):
        """送ってよい出口を 1 つ予約して返す。

        cost      その 1 リクエストが上流で消費する呼び出し数 (JSON-RPC のバッチ長)。
                  流量は「リクエスト毎秒」ではなく「呼び出し毎秒」で守る。上流が数えているのはこちら。
        deadline  この時刻までに空く出口が無ければ None を返す。**待ち続けない**ため。
                  全出口が休んでいるときに黙ってブロックすると、呼び出し側が減速できない。
        """
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
                        e.next_at = now + max(cost, 1) / (e.rate or self.rate0)
                        e.sent += 1
                        return e
                    wait = min(ready - now, 0.5)
                if deadline is not None and now + wait > deadline:
                    return None
            self.sleep(wait)

    def success(self, e, cost: int = 1):
        with self.lock:
            e.ok += 1
            e.fails = e.rate_fails = e.blocks = 0
            # 加算増加: 通り続けているあいだは少しずつ上げて、その出口の上限を探る
            e.ok_calls += max(cost, 1)
            if e.ok_calls >= RATE_PROBE:
                e.ok_calls = 0
                e.rate = min(RATE_MAX, (e.rate or self.rate0) + RATE_STEP)

    def failure(self, e, reason: str = "?", kind: str = "hard"):
        """kind="rate" は 429 (速すぎるだけ)。"hard" は 5xx や接続失敗 (出口が壊れている可能性)。"""
        rotate = None
        with self.lock:
            e.last_error = reason
            key = reason.split(" ", 1)[0]
            e.errors[key] = e.errors.get(key, 0) + 1
            e.bad += 1
            e.fails += 1
            base = 0.5 if kind == "rate" else 2.0
            cool = min(self.cooldown_max, base * (2.0 ** e.fails))
            e.cool_until = self.clock() + cool
            if kind == "rate":
                # 乗算減少。ただし直前に下げたばかりなら、それは同じバーストの残りなので下げない
                now = self.clock()
                if now - e.rate_cut_at >= RATE_CUT_GAP:
                    e.rate = max(RATE_MIN, (e.rate or self.rate0) * RATE_BACKOFF)
                    e.rate_cut_at = now
                e.ok_calls = 0
                e.rate_fails += 1
                # 張り直すとその出口は ROTATE_COST 秒ぶん丸ごと消える。
                # **休ませる時間のほうが短いなら待つほうが安い。** 休みが伸びきってから初めて替える。
                if (self.rotate_after and e.rate_fails >= self.rotate_after
                        and cool >= self.rotate_cost and e.device):
                    rotate, e.rate_fails = e.device, 0
            else:
                e.rate_fails = 0
        if rotate and self.on_rate_limited:      # ロックの外で呼ぶ (張り直しは refresh を伴う)
            self.on_rate_limited(rotate, reason)

    def blocked(self, e, reason: str = "proxy block"):
        """プロキシ越しに 429/403 を受けた、または上流 RPC が 403 を返した = **その IP が弾かれている**。

        上流 RPC のレート制限とは別物なので `rate` は動かさない。
        IP 評価は時間で戻らないので、休ませても意味が薄い。**数回で張り直す** (待ちとの比較はしない)。
        """
        rotate = None
        with self.lock:
            e.last_error = reason
            e.errors["blocked"] = e.errors.get("blocked", 0) + 1
            e.bad += 1
            e.blocks += 1
            e.cool_until = max(e.cool_until, self.clock() + 5.0)
            if self.rotate_after and e.blocks >= self.rotate_after and e.device:
                rotate, e.blocks = e.device, 0
        if rotate and self.on_rate_limited:
            self.on_rate_limited(rotate, reason)
        return rotate is not None

    def rotate_exit(self, dev: str = None, egress: str = None, reason: str = "手動"):
        """利用側からの張り直し要求。HTTPS の CONNECT は中身が見えないので、
        弾かれたことに気づけるのは利用側だけ。その申告で替えられるようにしておく。"""
        with self.lock:
            for e in self.exits:
                if (dev and e.device == dev) or (egress and e.egress == egress):
                    dev = e.device
                    break
            else:
                return None
        if dev and self.on_rate_limited:
            self.on_rate_limited(dev, reason)
            return dev
        return None

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
                     "rate": round(e.rate or self.rate0, 1), "egress_left": e.egress_left,
                     "cooling_s": round(max(0.0, e.cool_until - now), 1),
                     "errors": dict(e.errors), "last_error": e.last_error} for e in self.exits]


def _retryable(status: int, content: bytes) -> bool:
    return _classify(status, content) is not None


def _classify(status: int, content: bytes):
    """やり直すべきなら "rate" / "blocked" / "hard" を返す。やり直さないなら None。

    429 とレート制限のエラー本文は "rate" (速すぎるだけ)。403 は "blocked" (その IP が弾かれた。時間で戻らない)。
    5xx・本文が JSON でない・接続失敗は "hard"。
    """
    if status == 429:
        return "rate"
    if status == 403:
        return "blocked"
    if status >= 500:
        return "hard"
    try:
        body = json.loads(content)
    except ValueError:
        return "hard"
    err = body.get("error") if isinstance(body, dict) else None
    if err and "rate limit" in str(err.get("message", "")).lower():
        return "rate"
    return None


def _cost_of(body: bytes) -> int:
    """JSON-RPC のバッチ長 = そのリクエストが上流で消費する呼び出し数。"""
    try:
        p = json.loads(body)
    except ValueError:
        return 1
    return len(p) if isinstance(p, list) and p else 1


def make_front(pool: Pool, upstream: str = UPSTREAM, retries: int = 8, timeout: float = 90.0, extra_status=None,
               acquire_wait: float = ACQUIRE_WAIT):
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
            if self.path.rstrip("/").endswith("/rotate"):
                # HTTPS プロキシ越しの利用側は 429 を中継から見えない。申告で替えられるようにしておく
                try:
                    q = json.loads(body) if body else {}
                except ValueError:
                    q = {}
                dev = pool.rotate_exit(q.get("dev"), q.get("egress"), q.get("reason", "利用側の申告"))
                out = json.dumps({"rotated": dev}).encode()
                self._reply(200 if dev else 404, out)
                return
            cost = _cost_of(body)
            tried = []
            last = (502, b'{"gateway_error":"no attempt"}')
            for _ in range(retries + 1):
                n = len(pool.exits)
                e = pool.acquire(exclude=tried[-(n - 1):] if n > 1 else (), cost=cost,
                                 deadline=pool.clock() + acquire_wait)
                if e is None:
                    # 全出口が休んでいる。黙って待たずに 429 を返して、呼び出し側に減速させる
                    self.send_response(429)
                    self.send_header("content-type", "application/json")
                    self.send_header("retry-after", "2")
                    out = b'{"gateway_error":"all exits cooling"}'
                    self.send_header("content-length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                try:
                    r = e.session.post(e.target or upstream, data=body, timeout=timeout,
                                       headers={"content-type": "application/json"})
                    status, content = r.status_code, r.content
                    left = r.headers.get("x-tdly-egress-remaining")
                    if left and left.isdigit():
                        e.egress_left = int(left)
                except requests.RequestException as exc:
                    status, content = 502, json.dumps({"gateway_error": str(exc)[:200]}).encode()
                kind = _classify(status, content)
                if kind is None:
                    pool.success(e, cost)
                    self._reply(status, content)
                    return
                reason = f"{status} {content[:120].decode('utf-8', 'replace')}"
                if kind == "blocked":
                    pool.blocked(e, reason)
                else:
                    pool.failure(e, reason, kind)
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
    ap.add_argument("--rate", type=float, default=float(env("RATE", "6")),
                    help="出口ごとの毎秒**呼び出し数** (JSON-RPC のバッチは長さぶん消費する)")
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
    mgr_box = {}      # Manager は後で作るので箱越しに渡す (ホストの出口には device が無いので呼ばれない)
    pool = Pool(exits, rate=args.rate,
                on_rate_limited=lambda dev, why: mgr_box["m"].rotate(dev, why[:60]) if "m" in mgr_box else None)

    mgr = None
    n_vpn = args.exits - len(host_exits)
    if n_vpn > 0:
        import tunnels
        user, pw = env("OPENVPN_USERNAME"), env("OPENVPN_PASSWORD")
        if not user or not pw:
            raise SystemExit("OPENVPN_USERNAME / OPENVPN_PASSWORD が無い")
        provider = env("OPENVPN_PROVIDER", "expressvpn").lower()
        conf_dir = env("CONF_DIR") or tunnels.conf_dir_for(provider)
        preferred = [r.strip() for r in args.regions.split(",") if r.strip()] or tunnels.PREFERRED.get(provider, [])
        regions = tunnels.candidate_regions(preferred, conf_dir) if os.path.isdir(conf_dir) else []
        if not regions:
            try:
                have = ", ".join(tunnels.providers())
            except OSError:
                have = "?"
            raise SystemExit(f"{conf_dir} に .ovpn が無い (OPENVPN_PROVIDER={provider})。使えるプロバイダ: {have}")
        tunnels.write_auth(user, pw)
        logger.info("プロバイダ %s / 地域の候補 %d", provider, len(regions))

        def on_change(up):
            found = host_exits + [Exit(t["name"], t["egress"], t["session"], device=t["dev"]) for t in up]
            added, gone = pool.refresh(found)
            logger.info("出口 %d 本 (+%s -%s)", len(pool.exits), added, gone)

        # 見回りは 1 秒ごと (少しずつ張るので、張る間隔 launch_gap を守れる細かさにする)
        mgr = tunnels.Manager(n_vpn, regions, on_change, egress_of=egress_of, session_of=session_for,
                              conf_dir=conf_dir, interval=1.0)
        mgr_box["m"] = mgr
        mgr.start()

        def stop(*_):
            mgr.stop()
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)

    srv = serve(make_front(pool, args.upstream, args.retries, extra_status=mgr.status if mgr else None), args.listen)
    logger.info("待受 %s / 出口 %d 本 (ホスト %d + VPN %d) / 出口ごと毎秒 %g 呼び出し / "
                "待ち上限 %gs / 429 が %d 回続き休みが %gs を超えたら張り直し / 転送先 %s",
                args.listen, args.exits, len(host_exits), max(n_vpn, 0), args.rate,
                ACQUIRE_WAIT, ROTATE_AFTER, ROTATE_COST, args.upstream)
    if args.proxy_listen:
        import proxy
        psrv = proxy.serve(pool, args.proxy_listen)
        threading.Thread(target=psrv.serve_forever, daemon=True, name="proxy").start()
        logger.info("HTTP プロキシ待受 %s (VPN の出口をラウンドロビン)", args.proxy_listen)
    srv.serve_forever()


if __name__ == "__main__":
    main()
