"""中継の振り分け・流量・再試行と、トンネル管理を固定する。OpenVPN と通信は偽物で置き換える。"""
import json
import socket
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

def _fake_upstream(replies):
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
        (d / f"my_expressvpn_{r}_udp.ovpn").write_text(f"client\nremote host-{r} 1195\n")
    return str(d)


def test_openvpn_argv_keeps_default_route():
    argv = tunnels.openvpn_argv("japan_-_tokyo", "tun4", 104, conf_dir="/c", auth="/a")
    assert "--route-nopull" in argv
    assert argv[argv.index("--dev") + 1] == "tun4"
    assert argv[argv.index("--setenv") + 1:argv.index("--setenv") + 3] == ["TABLE", "104"]
    assert argv[argv.index("--config") + 1] == "/c/my_expressvpn_japan_-_tokyo_udp.ovpn"


def test_candidate_regions_puts_preferred_first(tmp_path):
    conf = _conf(tmp_path, ["a", "b", "c"])
    assert tunnels.candidate_regions(["c", "zz", "a"], conf) == ["c", "a", "b"]


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
                        conf_dir=conf, log_dir=str(tmp_path), spawn=spawn, resolver=resolver, clock=clk)
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
