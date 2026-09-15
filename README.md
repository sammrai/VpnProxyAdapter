# VpnProxyAdapter

## Overview

VpnProxyAdapter is a Docker-based solution that facilitates internet connections through VPN. The primary goal of this project is to centralize VPN settings, allowing other programs or services to utilize VPN connections simply by specifying proxy settings. This eliminates the need for individual programs to configure VPN settings, significantly reducing setup efforts.

## Configuration

1. Clone project

   ```
   git clone https://github.com/sammrai/VpnProxyAdapter.git
   ```

1. Setting up the .env file

   The project root contains a `.env` file with your ExpressVPN OpenVPN credentials.

   ```
   OPENVPN_USERNAME=your_username
   OPENVPN_PASSWORD=your_password
   ```

   - `OPENVPN_USERNAME` and `OPENVPN_PASSWORD` are the OpenVPN (manual configuration) credentials from your ExpressVPN account.
   - Regions are chosen automatically (nearest to Tokyo first). To pick them yourself, set `REGIONS` (see the Gateway section). Region names are listed in [README-configlist.md](./README-configlist.md) without the `my_expressvpn_` prefix and `_udp` suffix.

1. Starting the service

   Run the following command in the project directory to start the service.

   ```bash
   docker compose up -d --build vpnproxy
   ```

This starts several VPN tunnels in one container and exposes them behind a single HTTP proxy port.

## Usage

Each new connection through the proxy leaves from the next VPN exit (round robin), so the IP address changes per request.

```bash
for i in 1 2 3; do curl -s https://api.ipify.org -x localhost:8901; echo; done
```

### Note

* This project is intended for personal use. When using it, please comply with the terms of service of your VPN provider.
* This project is inspired by the following repository: https://github.com/DoganM95/Expressvpn-Proxy-Adapter


## Additional Information

It seems that ExpressVPN is currently offering a 30-day free trial.　[ExpressVPN's official website](https://www.expressrefer.com/refer-a-friend/30-days-free?locale=jp&referrer_id=96807179&utm_campaign=referrals&utm_medium=copy_link&utm_source=referral_dashboard)

## Gateway: 複数の VPN を 1 つのポートで回す

`vpnproxy` コンテナ (ソースは `gateway/`) は、コンテナ 1 つの中で VPN を地域ごとに複数張り、1 つのポートの裏で
リクエストごとに振り分ける。IP ごとに流量制限がある API (無料の RPC など) を、出口の数だけ並列に叩くためのもの。
以前の transmission-openvpn 版は置き換えた。8901 の汎用 HTTP プロキシは残してあり、接続ごとに VPN の出口を順番に使う。

```bash
docker compose up -d --build vpnproxy                 # 出口 20 本 (ホスト 1 + VPN 19)
GATEWAY_EXITS=30 GATEWAY_RATE=6 docker compose up -d vpnproxy
curl -s 127.0.0.1:8545 | jq '{exits: [.exits[] | {name, egress, ok, bad}], tunnels: [.tunnels[] | {dev, region, state}]}'

# 汎用 HTTP プロキシ (HTTP / HTTPS)。接続ごとに出口 IP が変わる
for i in 1 2 3; do curl -s https://api.ipify.org -x localhost:8901; echo; done

# 利用側は URL を 1 つ指定し、合計 (出口数 x RATE) まで投げてよい
curl -s 127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}'
```

| 環境変数 | 既定 | 意味 |
| --- | --- | --- |
| `EXITS` (`GATEWAY_EXITS`) | 20 | 出口数。ホスト 1 + VPN EXITS-1 |
| `RATE` (`GATEWAY_RATE`) | 6 | 出口ごとの毎秒リクエスト |
| `UPSTREAM` | Tenderly 公開 RPC | 転送先 |
| `REGIONS` | 東京から近い順 | 使う地域をカンマ区切りで (先頭から優先) |
| `INCLUDE_HOST` | 1 | 0 でホスト自身を出口に含めない (JSON-RPC 中継のみ。プロキシはホストを使わない) |
| `PROXY_LISTEN` | 0.0.0.0:8118 | 汎用 HTTP プロキシの待受。空で無効 |

仕組み

- コンテナ内で OpenVPN を tun0, tun1, ... に張る。サーバーが配る既定経路は受け取らず、
  トンネルごとに経路表を分ける。中継は送信ソケットを tun デバイスに結びつけるので、その通信だけがトンネルを通る
- VPN の接続は張りっぱなし。回すのは送り先の出口だけで、各出口の HTTPS 接続も使い回す
- 429 / 5xx / 接続失敗の出口はしばらく休ませ、別の出口でやり直す
- 接続先の名前が引けない地域は飛ばす。出口 IP が重複したら別の地域に張り替える。落ちたら張り直す
- 8545 は本文をそのまま転送先へ POST する中継 (JSON-RPC 向け)
- 8901 (コンテナ内 8118) は汎用 HTTP プロキシ。接続ごとに VPN の出口をラウンドロビンで選ぶ。
  keep-alive の接続は張っている間同じ出口のまま。名前解決はトンネルを通さない
- ExpressVPN の同時接続には上限がある。実測では VPN 20 本で既存の接続は切れなかった
- `thailand` は接続までは通るが外に出られないことがあり、優先の一覧から外している (出口 IP が取れないトンネルは自動で別の地域に張り替える)

テスト: `docker compose build vpnproxy && docker run --rm --entrypoint pytest vpnproxy:local -q`
