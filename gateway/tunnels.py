"""コンテナ内で OpenVPN を N 本起動し、見張る。

トンネル i は tun<i> に張り、サーバーが配る既定経路 (redirect-gateway) は受け取らない (--route-nopull)。
代わりに tun_up.sh が「tun<i> から出る通信は経路表 100+i を使い、その既定経路は tun<i>」とする。
中継は送信ソケットを tun<i> に結びつけるので、その通信だけがトンネルを通る。
サーバーが違っても同じ範囲のトンネル IP が払い出されうるので、送信元 IP ではなくデバイスで振り分けている。

- 設定は /configs/<プロバイダ>/ 以下の .ovpn。地域の名前は、そこからの相対パスから .ovpn を除いたもの
- 接続先の名前が引けない地域は飛ばす (設定リポジトリには廃止済みのサーバーが残っている)
- 出口 IP が他のトンネルと重複したら、別の地域に張り替える
- 落ちたら張り直す。同じ地域で max_region_fails 回失敗したら、その地域は使わない
- **張るのは少しずつ** (起動時も張り直しも)。同時に接続中にするのは max_connecting 本まで、開始の間隔は launch_gap 秒以上。
  30 本を一斉に張ると家のルーターの遅延が 0.2 ms から 75〜105 ms まで上がった (2026-09-23)。
  2 本ずつにしたら、30 本そろうまで約 90 秒で、ルーターの遅延は最大 1.5 ms だった
"""
import collections
import logging
import os
import socket
import subprocess
import threading
import time

logger = logging.getLogger("gateway.tunnels")

CONF_ROOT = os.environ.get("CONF_ROOT", "/configs")
CONF_DIR = os.environ.get("CONF_DIR", os.path.join(CONF_ROOT, "expressvpn"))
AUTH_FILE = os.environ.get("AUTH_FILE", "/run/vpn-auth")
LOG_DIR = os.environ.get("TUNNEL_LOG_DIR", "/tmp")
UP_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tun_up.sh")
READY_MARK = "Initialization Sequence Completed"

# プロバイダごとの優先順。載っていないプロバイダは名前順に使う。
# ExpressVPN は東京から近い順。足りなければ残りの地域を名前順に使う。
# thailand は接続までは通るが外に出られないことが続いた (2026-09-15 に 2 回) ので、優先の一覧から外している
EXPRESSVPN_REGIONS = [
    "japan_-_tokyo", "japan_-_shibuya", "japan_-_yokohama", "hong_kong_-_1", "hong_kong_-_2",
    "south_korea_-_2", "taiwan_-_3", "singapore_-_cbd", "singapore_-_jurong", "singapore_-_marina_bay",
    "philippines", "malaysia", "vietnam", "macau", "guam", "indonesia", "cambodia",
    "usa_-_los_angeles_-_1", "usa_-_san_francisco", "usa_-_seattle", "usa_-_los_angeles_-_2",
    "australia_-_sydney", "usa_-_santa_monica", "usa_-_phoenix", "usa_-_dallas", "usa_-_chicago",
]
PREFERRED = {"expressvpn": [f"my_expressvpn_{r}_udp" for r in EXPRESSVPN_REGIONS]}


def conf_dir_for(provider: str, root: str = None) -> str:
    return os.path.join(root or CONF_ROOT, provider.lower())


def providers(root: str = None):
    """.ovpn があるプロバイダの一覧。"""
    r = root or CONF_ROOT
    return sorted(p for p in os.listdir(r)
                  if any(f.endswith(".ovpn") for _, _, files in os.walk(os.path.join(r, p)) for f in files))


def config_path(region: str, conf_dir: str = None) -> str:
    return os.path.join(conf_dir or CONF_DIR, region + ".ovpn")


def remote_host(path: str):
    with open(path) as f:
        for line in f:
            if line.startswith("remote "):
                return line.split()[1]
    return None


def candidate_regions(preferred, conf_dir: str = None):
    """設定がある地域を、preferred に合うものを先頭にして返す。

    preferred の各要素は、名前が一致する地域があればそれ、無ければ名前に含む地域すべてに合う (大文字小文字は区別しない)。
    """
    d = conf_dir or CONF_DIR
    names = sorted(os.path.relpath(os.path.join(root, f), d)[:-len(".ovpn")]
                   for root, _, files in os.walk(d) for f in files if f.endswith(".ovpn"))
    head = []
    for p in (x.lower() for x in preferred):
        head += [n for n in names if n.lower() == p] or [n for n in names if p in n.lower()]
    return list(dict.fromkeys(head + names))


def write_auth(user: str, password: str, path: str = None) -> None:
    path = path or AUTH_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{user}\n{password}\n")


def openvpn_argv(region: str, dev: str, table: int, conf_dir: str = None, auth: str = None):
    path = config_path(region, conf_dir)
    return ["openvpn", "--cd", os.path.dirname(path),   # 設定の中の ca.crt などの相対パスを解決する
            "--config", path,
            "--dev", dev, "--dev-type", "tun",
            "--route-nopull",                       # 既定経路を奪わない
            "--auth-user-pass", auth or AUTH_FILE, "--auth-nocache",
            "--script-security", "2", "--setenv", "TABLE", str(table), "--up", UP_SCRIPT,
            "--inactive", "0", "--ping", "10", "--ping-exit", "60",
            "--verb", "3"]


def _spawn(argv, log_path):
    f = open(log_path, "wb")
    return subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT)


class Tunnel:
    def __init__(self, idx: int):
        self.idx, self.dev, self.table = idx, f"tun{idx}", 100 + idx
        self.region = None
        self.proc = None
        self.log = None
        self.state = "idle"          # idle / connecting / up / failed / no-region
        self.egress = None
        self.session = None
        self.started = 0.0
        self.retry_at = 0.0
        self.fails = 0
        self.note = ""


class Manager:
    def __init__(self, n, regions, on_change, *, egress_of, session_of, conf_dir=None, log_dir=None,
                 spawn=_spawn, resolver=socket.getaddrinfo, clock=time.monotonic,
                 ready_timeout=120.0, max_region_fails=2, interval=5.0,
                 max_connecting=2, launch_gap=2.0):
        self.tunnels = [Tunnel(i) for i in range(n)]
        self.queue = collections.deque(regions)
        self.region_fails = collections.Counter()
        self.on_change = on_change
        self.egress_of, self.session_of = egress_of, session_of
        self.conf_dir, self.log_dir = conf_dir, log_dir or LOG_DIR
        self.spawn, self.resolver, self.clock = spawn, resolver, clock
        self.ready_timeout, self.max_region_fails, self.interval = ready_timeout, max_region_fails, interval
        self.max_connecting = max_connecting    # 同時に接続中にする上限。0 なら制限しない
        self.launch_gap = launch_gap            # 接続を始める間隔の下限 (秒)
        self.last_launch = -1e9
        self._stop = threading.Event()

    # --- 1 回分の見回り。テストからも呼ぶ ---
    def step(self):
        now = self.clock()
        changed = False
        connecting = sum(t.state == "connecting" for t in self.tunnels)
        for t in self.tunnels:
            alive = t.proc is not None and t.proc.poll() is None
            if t.state in ("idle", "failed", "no-region"):
                if (now >= t.retry_at and (not self.max_connecting or connecting < self.max_connecting)
                        and now - self.last_launch >= self.launch_gap):
                    self._launch(t, now)
                    if t.state == "connecting":
                        connecting += 1
                        self.last_launch = now
            elif t.state == "connecting":
                if not alive:
                    self._fail(t, now, f"openvpn が終了した (ログ {t.log})")
                elif self._ready(t):
                    eg = self.egress_of(t.dev)
                    taken = {x.egress for x in self.tunnels if x is not t and x.state == "up"}
                    if not eg:
                        if now - t.started > self.ready_timeout:
                            self._fail(t, now, "出口 IP を取れない")
                    elif eg in taken:
                        self._fail(t, now, f"出口 IP {eg} が他のトンネルと重複")
                    else:
                        t.egress, t.session, t.state, t.fails, t.note = eg, self.session_of(t.dev), "up", 0, ""
                        logger.info("%s up: %s 出口 %s", t.dev, t.region, eg)
                        changed = True
                elif now - t.started > self.ready_timeout:
                    self._fail(t, now, "接続が完了しない")
            elif t.state == "up" and not alive:
                self._fail(t, now, "openvpn が落ちた")
                changed = True
        if changed:
            self.on_change(self.up())
        return changed

    def rotate(self, dev, reason: str = "レート制限"):
        """出口 IP を変えるためにトンネルを張り直す。

        上流のレート制限は IP ごとなので、休ませるより IP を替えるほうが早く戻る。
        **地域のせいではないので `region_fails` は増やさず、その地域は待ち行列に戻す。**
        `retry_at` は今にする (`_fail` と違って待たせない)。
        """
        now = self.clock()
        for t in self.tunnels:
            if t.dev != dev or t.state != "up":
                continue
            self._kill(t)
            if t.region:
                self.queue.append(t.region)
            logger.info("%s (%s) を張り直す: %s", t.dev, t.region, reason)
            t.region = t.proc = t.egress = t.session = None
            t.state, t.note, t.retry_at, t.fails = "failed", reason, now, 0
            self.on_change(self.up())        # 張り直すあいだプールから外す
            return True
        return False

    def up(self):
        return [{"name": f"{t.dev}:{t.region}", "dev": t.dev, "egress": t.egress, "session": t.session}
                for t in self.tunnels if t.state == "up"]

    def status(self):
        return [{"dev": t.dev, "region": t.region, "state": t.state, "egress": t.egress,
                 "fails": t.fails, "note": t.note} for t in self.tunnels]

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="tunnels").start()

    def stop(self):
        self._stop.set()
        for t in self.tunnels:
            self._kill(t)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:                       # 見回りは止めない
                logger.exception("見回りで例外")
            self._stop.wait(self.interval)

    # --- 内部 ---
    def _next_region(self):
        in_use = {t.region for t in self.tunnels if t.region}
        for _ in range(len(self.queue)):
            r = self.queue.popleft()
            if r in in_use:
                self.queue.append(r)
                continue
            try:
                host = remote_host(config_path(r, self.conf_dir))
                if not host:
                    raise OSError("remote が無い")
                self.resolver(host, None)
            except OSError:
                logger.warning("地域 %s は接続先の名前が引けない。使わない", r)
                continue
            return r
        return None

    def _launch(self, t, now):
        r = self._next_region()
        if r is None:
            t.state, t.note, t.retry_at = "no-region", "使える地域が残っていない", now + 30.0
            return
        t.region, t.state, t.started, t.note = r, "connecting", now, ""
        t.log = os.path.join(self.log_dir, f"{t.dev}.log")
        t.proc = self.spawn(openvpn_argv(r, t.dev, t.table, self.conf_dir), t.log)
        logger.info("%s 接続開始: %s", t.dev, r)

    def _ready(self, t):
        try:
            with open(t.log, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 65536))
                return READY_MARK.encode() in f.read()
        except OSError:
            return False

    def _kill(self, t):
        p = t.proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except Exception:
                p.kill()

    def _fail(self, t, now, reason):
        self._kill(t)
        logger.warning("%s (%s) %s", t.dev, t.region, reason)
        if t.region:
            self.region_fails[t.region] += 1
            if self.region_fails[t.region] < self.max_region_fails:
                self.queue.append(t.region)
            else:
                logger.warning("地域 %s は %d 回失敗。以後使わない", t.region, self.region_fails[t.region])
        t.fails += 1
        t.region = t.proc = t.egress = t.session = None
        t.state, t.note, t.retry_at = "failed", reason, now + min(60.0, 5.0 * t.fails)
