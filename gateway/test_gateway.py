"""中継の振り分け・流量・再試行と、トンネル管理を固定する。OpenVPN と通信は偽物で置き換える。"""
import json
import os
import socket
import subprocess
import threading

import pytest
import requests

import gateway
import tunnels


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def ex(name, egress, target=None):
    return gateway.Exit(name, egress, gateway.session_for(None), target=target)


# --- Pool ------------------------------------------------------------------

def test_pool_spaces_requests_per_exit():
    clk = FakeClock()
    a, b = ex("a", "1.1.1.1"), ex("b", "2.2.2.2")
    pool = gateway.Pool([a, b], rate=2.0, clock=clk, sleep=clk.sleep)       # 0.5 秒間隔
    got = [(pool.acquire().name, clk.t) for _ in range(4)]
    assert [n for n, _ in got] == ["a", "b", "a", "b"]
    assert got[2][1] == pytest.approx(0.5) and got[3][1] == pytest.approx(0.5)


def test_failure_cools_exit_down():
    clk = FakeClock()
    a, b = ex("a", "1"), ex("b", "2")
    pool = gateway.Pool([a, b], rate=100.0, clock=clk, sleep=clk.sleep)
    pool.failure(a, "429 rate limit")
    assert all(pool.acquire() is b for _ in range(3))
    assert a.errors == {"429": 1}


def test_refresh_keeps_state_and_drops_duplicate_egress():
    clk = FakeClock()
    a, b = ex("a", "1"), ex("b", "2")
    pool = gateway.Pool([a, b], rate=1.0, clock=clk, sleep=clk.sleep)
    pool.failure(a)
    added, gone = pool.refresh([ex("a2", "1"), ex("c", "3"), ex("c2", "3")])
    assert added == ["3"] and gone == ["2"]
    assert pool.exits[0] is a and a.name == "a2" and a.cool_until > 0
    assert [e.egress for e in pool.exits] == ["1", "3"]


def test_pool_waits_until_an_exit_appears():
    clk = FakeClock()
    pool = gateway.Pool([], rate=1.0, clock=clk, sleep=clk.sleep)
    later = ex("x", "9")

    def sleep_and_join(s):
        clk.sleep(s)
        pool.refresh([later])
    pool.sleep = sleep_and_join
    assert pool.acquire() is later


def test_retryable_classification():
    assert gateway._retryable(429, b"{}")
    assert gateway._retryable(503, b"upstream connect error")
    assert gateway._retryable(200, b"not json")
    assert gateway._retryable(200, json.dumps({"error": {"message": "rate limit exceeded"}}).encode())
    assert not gateway._retryable(200, json.dumps({"result": []}).encode())
    assert not gateway._retryable(200, json.dumps({"error": {"message": "query returned more than 50000"}}).encode())


def test_session_binds_socket_to_device():
    s = gateway.session_for("tun3")
    opts = s.get_adapter("https://example.com").poolmanager.connection_pool_kw["socket_options"]
    assert (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tun3") in opts
    host = gateway.session_for(None).get_adapter("https://example.com").poolmanager.connection_pool_kw["socket_options"]
    assert all(o[1] != socket.SO_BINDTODEVICE for o in host)


# --- front -----------------------------------------------------------------

def _fake_upstream(replies, headers=None):
    from http.server import BaseHTTPRequestHandler
    hits = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            hits.append(1)
            st, bd = replies[min(len(hits) - 1, len(replies) - 1)]
            b = json.dumps(bd).encode()
            self.send_response(st)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = gateway.serve(H, "127.0.0.1:0")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", hits


def test_front_fails_over_to_another_exit():
    s1, u1, _ = _fake_upstream([(429, {"error": {"message": "rate limit exceeded"}})])
    s2, u2, _ = _fake_upstream([(200, {"jsonrpc": "2.0", "id": 1, "result": ["ok"]})])
    try:
        pool = gateway.Pool([ex("a", "9.9.9.1", u1), ex("b", "9.9.9.2", u2)], rate=1000.0)
        front = gateway.serve(gateway.make_front(pool, upstream="http://unused", retries=3,
                                                 extra_status=lambda: [{"dev": "tun0"}]), "127.0.0.1:0")
        threading.Thread(target=front.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{front.server_address[1]}"
        for _ in range(3):
            assert requests.post(url, json={"id": 1}, timeout=5).json()["result"] == ["ok"]
        st = requests.get(url, timeout=5).json()
        by = {x["egress"]: x for x in st["exits"]}
        assert by["9.9.9.1"]["errors"].get("429", 0) >= 1 and by["9.9.9.2"]["ok"] == 3
        assert st["tunnels"] == [{"dev": "tun0"}]
        front.shutdown()
    finally:
        s1.shutdown()
        s2.shutdown()


# --- tunnels ---------------------------------------------------------------

class FakeProc:
    def __init__(self):
        self.dead = False

    def poll(self):
        return 1 if self.dead else None

    def terminate(self):
        self.dead = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.dead = True


def _conf(tmp_path, regions):
    d = tmp_path / "conf"
    d.mkdir()
    for r in regions:
        f = d / f"{r}.ovpn"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"client\nremote host-{r} 1195\n")
    return str(d)


def test_openvpn_argv_keeps_default_route():
    argv = tunnels.openvpn_argv("japan_-_tokyo", "tun4", 104, conf_dir="/c", auth="/a")
    assert "--route-nopull" in argv
    assert argv[argv.index("--dev") + 1] == "tun4"
    assert argv[argv.index("--setenv") + 1:argv.index("--setenv") + 3] == ["TABLE", "104"]
    assert argv[argv.index("--config") + 1] == "/c/japan_-_tokyo.ovpn"
    assert argv.index("--cd") < argv.index("--config") and argv[argv.index("--cd") + 1] == "/c"


def test_candidate_regions_puts_preferred_first(tmp_path):
    conf = _conf(tmp_path, ["a", "b", "c"])
    assert tunnels.candidate_regions(["c", "zz", "a"], conf) == ["c", "a", "b"]


def test_candidate_regions_walks_subdirs_and_matches_part_of_name(tmp_path):
    conf = _conf(tmp_path, ["my_x_japan_-_tokyo_udp", "my_x_japan_-_tokyo_-_2_udp", "my_x_usa_udp", "udp/US Buffalo"])
    assert tunnels.candidate_regions(["my_x_japan_-_tokyo_udp", "buffalo"], conf) == [
        "my_x_japan_-_tokyo_udp", "udp/US Buffalo", "my_x_japan_-_tokyo_-_2_udp", "my_x_usa_udp"]
    assert tunnels.candidate_regions(["JAPAN"], conf)[:2] == ["my_x_japan_-_tokyo_-_2_udp", "my_x_japan_-_tokyo_udp"]


def test_providers_lists_only_dirs_with_ovpn(tmp_path):
    (tmp_path / "a" / "sub").mkdir(parents=True)
    (tmp_path / "a" / "sub" / "x.ovpn").write_text("")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "readme.txt").write_text("")
    assert tunnels.providers(str(tmp_path)) == ["a"]
    assert tunnels.conf_dir_for("ExpressVPN", "/c") == "/c/expressvpn"


def test_sanitize_ovpn_drops_routes_and_removed_options(tmp_path):
    f = tmp_path / "x.ovpn"
    f.write_bytes(b"client\r\nns-cert-type server\r\nredirect-gateway def1\r\nroute 10.0.0.0 255.0.0.0\n"
                  b"route-nopull\nroute-method exe\nkeysize 256\nkey-method 2\nup /etc/up.sh\nup-delay\ncipher AES-256-CBC\n"
                  b"ca /etc/openvpn/tiger/ca.crt\ntls-auth /etc/openvpn/ironsocket/tls-auth.txt 1\n")
    subprocess.run(["sh", os.path.join(os.path.dirname(__file__), "sanitize_ovpn.sh"), str(f)], check=True)
    assert f.read_bytes() == (b"client\r\nremote-cert-tls server\r\nroute-nopull\nup-delay\ncipher AES-256-CBC\n"
                              b"ca ca.crt\ntls-auth tls-auth.txt 1\n")


def test_manager_skips_unresolvable_and_replaces_duplicate_egress(tmp_path):
    conf = _conf(tmp_path, ["a", "b", "c", "d"])
    clk = FakeClock()
    procs = {}

    def spawn(argv, log):
        with open(log, "w") as f:
            f.write(tunnels.READY_MARK)
        p = FakeProc()
        procs[argv[argv.index("--dev") + 1]] = p
        return p

    def resolver(host, port):
        if host == "host-b":
            raise socket.gaierror("no such host")
        return [("x",)]

    egress = {"tun0": ["1.1.1.1"], "tun1": ["1.1.1.1", "2.2.2.2"]}
    changes = []
    m = tunnels.Manager(2, tunnels.candidate_regions(["c", "a"], conf), changes.append,
                        egress_of=lambda dev: egress[dev].pop(0), session_of=lambda dev: f"s-{dev}",
                        conf_dir=conf, log_dir=str(tmp_path), spawn=spawn, resolver=resolver, clock=clk,
                        max_connecting=0, launch_gap=0)
    m.step()                                   # tun0 -> c, tun1 -> a
    assert [t.region for t in m.tunnels] == ["c", "a"]
    m.step()                                   # tun0 up、tun1 は出口 IP が重複 -> 張り替え待ち
    assert [t.state for t in m.tunnels] == ["up", "failed"]
    clk.t += 6
    m.step()                                   # tun1 -> b は名前が引けないので飛ばし、d
    assert m.tunnels[1].region == "d"
    m.step()
    assert [t.state for t in m.tunnels] == ["up", "up"]
    assert {x["egress"] for x in changes[-1]} == {"1.1.1.1", "2.2.2.2"}

    procs["tun0"].dead = True                  # 落ちたら出口から外す
    m.step()
    assert m.tunnels[0].state == "failed"
    assert {x["egress"] for x in changes[-1]} == {"2.2.2.2"}


# --- 呼び出し数で流量を守る / 全出口が休んだら 429 / 429 で張り直す --------------

def test_batch_consumes_rate_per_call_not_per_request():
    """JSON-RPC のバッチは長さぶん流量を食う。1 件と数えると上流の制限を batch 倍で踏む。"""
    clk = FakeClock()
    a = ex("a", "1.1.1.1")
    pool = gateway.Pool([a], rate=10.0, clock=clk, sleep=clk.sleep)         # 0.1 秒 / 呼び出し
    pool.acquire(cost=20)
    assert a.next_at == pytest.approx(2.0)                                  # 20 呼び出し = 2 秒ぶん
    pool.acquire()                                                          # 空くまで待つ
    assert clk.t == pytest.approx(2.0)


def test_cost_of_counts_jsonrpc_batch():
    assert gateway._cost_of(b'[{"id":1},{"id":2},{"id":3}]') == 3
    assert gateway._cost_of(b'{"id":1}') == 1
    assert gateway._cost_of(b'not json') == 1
    assert gateway._cost_of(b'[]') == 1


def test_acquire_gives_up_when_every_exit_is_cooling():
    """全出口が休んでいるとき、待ち続けずに None を返す (呼び出し側が減速できるように)。"""
    clk = FakeClock()
    a, b = ex("a", "1.1.1.1"), ex("b", "2.2.2.2")
    pool = gateway.Pool([a, b], rate=100.0, clock=clk, sleep=clk.sleep, rotate_after=0)
    for _ in range(6):
        pool.failure(a, "429 rate limit", "rate")
        pool.failure(b, "429 rate limit", "rate")
    assert pool.acquire(deadline=clk.t + 5.0) is None
    assert pool.acquire(deadline=clk.t + 3600.0) is not None                # 十分待てば取れる


def test_rate_limit_cools_shorter_than_hard_failure():
    clk = FakeClock()
    a, b = ex("a", "1.1.1.1"), ex("b", "2.2.2.2")
    pool = gateway.Pool([a, b], rate=100.0, clock=clk, sleep=clk.sleep, rotate_after=0)
    pool.failure(a, "429 rate limit", "rate")
    pool.failure(b, "502 boom", "hard")
    assert a.cool_until == pytest.approx(1.0) and b.cool_until == pytest.approx(4.0)


def test_rotate_only_when_waiting_costs_more_than_reconnecting():
    """張り直しは出口を丸ごと失う。**休みがそのコストを超えてから**でないと替えない。"""
    clk = FakeClock()
    a = gateway.Exit("a", "1.1.1.1", gateway.session_for(None), device="tun3")
    seen = []
    pool = gateway.Pool([a], rate=100.0, clock=clk, sleep=clk.sleep,
                        on_rate_limited=lambda dev, why: seen.append(dev),
                        rotate_after=3, rotate_cost=20.0)
    cools = []
    for i in range(5):                        # 休みは 0.5*2^n = 1,2,4,8,16 秒。どれも 20 秒未満
        pool.failure(a, "429 rate limit", "rate")
        cools.append(a.cool_until - clk.t)
    assert cools == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert seen == [], "休みのほうが短いのに張り直している"
    pool.failure(a, "429 rate limit", "rate")  # 6 回目で 32 秒 > 20 秒
    assert seen == ["tun3"]
    pool.success(a)                            # 通れば数え直し
    for _ in range(2):
        pool.failure(a, "429 rate limit", "rate")
    assert seen == ["tun3"]


def test_rotate_disabled_when_cost_is_high():
    """張り直しが高くつく環境では、休みが上限に張り付いても替えない。"""
    clk = FakeClock()
    a = gateway.Exit("a", "1.1.1.1", gateway.session_for(None), device="tun3")
    seen = []
    pool = gateway.Pool([a], rate=100.0, clock=clk, sleep=clk.sleep, cooldown_max=60.0,
                        on_rate_limited=lambda dev, why: seen.append(dev),
                        rotate_after=3, rotate_cost=120.0)
    for _ in range(12):
        pool.failure(a, "429 rate limit", "rate")
    assert seen == []


def test_hard_failure_does_not_rotate():
    clk = FakeClock()
    a = gateway.Exit("a", "1.1.1.1", gateway.session_for(None), device="tun3")
    seen = []
    pool = gateway.Pool([a], rate=100.0, clock=clk, sleep=clk.sleep,
                        on_rate_limited=lambda dev, why: seen.append(dev), rotate_after=2)
    for _ in range(5):
        pool.failure(a, "502 boom", "hard")
    assert seen == []


def test_classify_splits_rate_from_hard():
    assert gateway._classify(429, b"{}") == "rate"
    assert gateway._classify(503, b"{}") == "hard"
    assert gateway._classify(200, b"not json") == "hard"
    assert gateway._classify(200, b'{"error":{"message":"rate limit exceeded"}}') == "rate"
    assert gateway._classify(200, b'{"result":"0x1"}') is None


def test_manager_rotate_relaunches_without_blaming_the_region():
    """張り直しは地域のせいではないので region_fails を増やさず、その地域は待ち行列に戻す。"""
    clk = FakeClock()
    changed = []
    mgr = tunnels.Manager(1, ["tokyo", "osaka"], lambda up: changed.append(len(up)),
                          egress_of=lambda dev: "9.9.9.9", session_of=lambda dev: None,
                          conf_dir="/nonexistent", spawn=lambda argv, log: _FakeProc(), clock=clk)
    t = mgr.tunnels[0]
    t.state, t.region, t.egress, t.session = "up", "tokyo", "9.9.9.9", object()
    t.proc = _FakeProc()
    assert mgr.rotate("tun0", "429") is True
    assert t.state == "failed" and t.egress is None and t.fails == 0
    assert t.retry_at == pytest.approx(clk.t)          # すぐ張り直せる
    assert mgr.region_fails["tokyo"] == 0
    assert "tokyo" in mgr.queue
    assert changed == [0]
    assert mgr.rotate("tun0", "429") is False          # up でなければ何もしない


class _FakeProc:
    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, *a):
        return 0

    def kill(self):
        pass


# --- 出口ごとの適応レート (AIMD) --------------------------------------------

def test_rate_climbs_while_calls_keep_going_through():
    """通り続けるあいだは少しずつ上げて、その出口の上限を探る。"""
    clk = FakeClock()
    a = ex("a", "1.1.1.1")
    pool = gateway.Pool([a], rate=10.0, clock=clk, sleep=clk.sleep)
    assert (a.rate or pool.rate0) == 10.0
    pool.success(a, cost=gateway.RATE_PROBE)
    assert a.rate == 11.0
    pool.success(a, cost=gateway.RATE_PROBE)
    assert a.rate == 12.0


def test_rate_drops_on_429_and_restarts_the_probe():
    clk = FakeClock()
    a = ex("a", "1.1.1.1")
    pool = gateway.Pool([a], rate=20.0, clock=clk, sleep=clk.sleep, rotate_after=0)
    pool.success(a, cost=gateway.RATE_PROBE // 2)       # 途中まで貯めた分は
    pool.failure(a, "429 rate limit", "rate")
    assert a.rate == pytest.approx(14.0) and a.ok_calls == 0    # 429 で捨てる
    clk.t += gateway.RATE_CUT_GAP
    pool.failure(a, "429 rate limit", "rate")
    assert a.rate == pytest.approx(9.8)


def test_hard_failure_does_not_change_the_rate():
    """5xx や接続失敗は「速すぎる」の証拠ではないので、レートは動かさない。"""
    clk = FakeClock()
    a = ex("a", "1.1.1.1")
    pool = gateway.Pool([a], rate=20.0, clock=clk, sleep=clk.sleep)
    pool.failure(a, "502 boom", "hard")
    assert (a.rate or pool.rate0) == 20.0


def test_rate_stays_within_bounds():
    clk = FakeClock()
    a = ex("a", "1.1.1.1")
    pool = gateway.Pool([a], rate=10.0, clock=clk, sleep=clk.sleep, rotate_after=0)
    for _ in range(200):
        pool.success(a, cost=gateway.RATE_PROBE)
    assert a.rate == gateway.RATE_MAX
    for _ in range(200):
        clk.t += gateway.RATE_CUT_GAP                   # 間を空けないと 1 回ぶんしか下がらない
        pool.failure(a, "429 rate limit", "rate")
    assert a.rate == gateway.RATE_MIN


def test_acquire_uses_the_per_exit_rate():
    """速い出口と遅い出口が混ざっていても、それぞれの間隔で回す。"""
    clk = FakeClock()
    fast, slow = ex("fast", "1.1.1.1"), ex("slow", "2.2.2.2")
    pool = gateway.Pool([fast, slow], rate=10.0, clock=clk, sleep=clk.sleep)
    fast.rate, slow.rate = 20.0, 5.0
    pool.acquire(); pool.acquire()
    assert fast.next_at == pytest.approx(0.05)          # 1/20
    assert slow.next_at == pytest.approx(0.2)           # 1/5


# --- バーストを 1 回と数える / プロキシからの申告 / 手動の張り直し ------------

def test_a_burst_of_429_counts_as_one_slowdown():
    """減速が効くまでに飛んでいた分がまとめて返るので、1 件ごとに掛けると下限まで落ちる。"""
    clk = FakeClock()
    a = ex("a", "1.1.1.1")
    pool = gateway.Pool([a], rate=20.0, clock=clk, sleep=clk.sleep, rotate_after=0)
    for _ in range(8):                       # 同じ瞬間に 8 件の 429 が返ってきた
        pool.failure(a, "429 rate limit", "rate")
    assert a.rate == pytest.approx(14.0)     # 20 * 0.7 を 1 回だけ
    clk.t += gateway.RATE_CUT_GAP
    pool.failure(a, "429 rate limit", "rate")
    assert a.rate == pytest.approx(9.8)      # 間が空けば次の減速


def test_proxy_block_rotates_without_touching_the_rate():
    """プロキシ越しの 403/429 は IP 評価の問題。時間で戻らないので待たずに替える。
    上流 RPC のレートとは無関係なので rate は動かさない。"""
    clk = FakeClock()
    a = gateway.Exit("a", "1.1.1.1", gateway.session_for(None), device="tun2")
    seen = []
    pool = gateway.Pool([a], rate=20.0, clock=clk, sleep=clk.sleep,
                        on_rate_limited=lambda dev, why: seen.append(dev), rotate_after=3)
    assert pool.blocked(a, "429 via proxy") is False
    assert pool.blocked(a, "429 via proxy") is False
    assert (a.rate or pool.rate0) == 20.0, "プロキシの弾かれは RPC のレートと無関係"
    assert pool.blocked(a, "429 via proxy") is True
    assert seen == ["tun2"] and a.blocks == 0


def test_rotate_exit_by_egress_or_dev():
    clk = FakeClock()
    a = gateway.Exit("a", "1.1.1.1", gateway.session_for(None), device="tun2")
    b = gateway.Exit("b", "2.2.2.2", gateway.session_for(None), device="tun3")
    seen = []
    pool = gateway.Pool([a, b], rate=10.0, clock=clk, sleep=clk.sleep,
                        on_rate_limited=lambda dev, why: seen.append((dev, why)))
    assert pool.rotate_exit(egress="2.2.2.2", reason="利用側の申告") == "tun3"
    assert pool.rotate_exit(dev="tun2") == "tun2"
    assert pool.rotate_exit(egress="9.9.9.9") is None
    assert [d for d, _ in seen] == ["tun3", "tun2"]


# --- 上流の 403 ------------------------------------------------------------

def test_success_resets_the_block_count():
    """弾かれたのが散発で、あいだに通っているなら張り直さない。"""
    clk = FakeClock()
    a = gateway.Exit("a", "1.1.1.1", gateway.session_for(None), device="tun2")
    seen = []
    pool = gateway.Pool([a], rate=20.0, clock=clk, sleep=clk.sleep,
                        on_rate_limited=lambda dev, why: seen.append(dev), rotate_after=3)
    for _ in range(3):
        pool.blocked(a, "403")
        pool.blocked(a, "403")
        pool.success(a)
    assert seen == [] and a.blocks == 0


def test_classify_treats_403_as_blocked():
    assert gateway._classify(403, b"<html>403 Forbidden</html>") == "blocked"
    assert gateway._retryable(403, b"{}")


def _front(pool):
    front = gateway.serve(gateway.make_front(pool, upstream="http://unused", retries=3), "127.0.0.1:0")
    threading.Thread(target=front.serve_forever, daemon=True).start()
    return front, f"http://127.0.0.1:{front.server_address[1]}"


def test_front_moves_off_a_403_exit_and_rotates_it():
    """上流の 403 = その IP が弾かれた。別の出口でやり直し、続いたら張り直す。レートは動かさない。"""
    s1, u1, _ = _fake_upstream([(403, {"error": "forbidden"})])
    s2, u2, _ = _fake_upstream([(200, {"jsonrpc": "2.0", "id": 1, "result": "0x1"})])
    a = gateway.Exit("a", "9.9.9.1", gateway.session_for(None), target=u1, device="tun9")
    b = gateway.Exit("b", "9.9.9.2", gateway.session_for(None), target=u2)
    seen = []
    pool = gateway.Pool([a, b], rate=1000.0, on_rate_limited=lambda dev, why: seen.append(dev),
                        rotate_after=1)
    front, url = _front(pool)
    try:
        assert requests.post(url, json={"id": 1}, timeout=5).json()["result"] == "0x1"
        assert a.errors == {"blocked": 1} and seen == ["tun9"]
        assert a.rate is None, "403 は流量の問題ではない"
    finally:
        front.shutdown()
        s1.shutdown()
        s2.shutdown()


def test_status_shows_the_egress_left_reported_by_upstream():
    """Tenderly は IP ごとに 1 日の応答量に上限がある。使い切る前に止められるよう、残りを状態に出す。"""
    s1, u1, _ = _fake_upstream([(200, {"jsonrpc": "2.0", "id": 1, "result": "0x1"})],
                               headers={"x-tdly-egress-remaining": "123456"})
    pool = gateway.Pool([ex("a", "9.9.9.1", u1)], rate=1000.0)
    front, url = _front(pool)
    try:
        assert requests.get(url, timeout=5).json()["exits"][0]["egress_left"] is None
        requests.post(url, json={"id": 1}, timeout=5)
        assert requests.get(url, timeout=5).json()["exits"][0]["egress_left"] == 123456
    finally:
        front.shutdown()
        s1.shutdown()


# --- トンネルを少しずつ張る -------------------------------------------------

def test_manager_launches_a_few_tunnels_at_a_time(tmp_path):
    """30 本を一斉に張るとルーターが混む。同時に接続中は max_connecting 本、開始は launch_gap 秒おき。"""
    regions = [f"r{i}" for i in range(7)]
    conf = _conf(tmp_path, regions)
    clk = FakeClock()
    m = tunnels.Manager(5, regions, lambda up: None, egress_of=lambda dev: None, session_of=lambda dev: None,
                        conf_dir=conf, log_dir=str(tmp_path), spawn=lambda argv, log: FakeProc(),
                        resolver=lambda h, p: [("x",)], clock=clk, max_connecting=2, launch_gap=2.0)
    connecting = lambda: [t.state for t in m.tunnels].count("connecting")   # noqa: E731
    m.step()
    assert connecting() == 1                   # 1 回の見回りで 1 本
    clk.t += 1.0
    m.step()
    assert connecting() == 1                   # 間隔が空いていない
    clk.t += 1.0
    m.step()
    assert connecting() == 2
    clk.t += 2.0
    m.step()
    assert connecting() == 2                   # 上限の 2 本で止まる
