# VpnProxyAdapter

## Overview

VpnProxyAdapter is a Docker-based solution that facilitates internet connections through VPN. The primary goal of this project is to centralize VPN settings, allowing other programs or services to utilize VPN connections simply by specifying proxy settings. This eliminates the need for individual programs to configure VPN settings, significantly reducing setup efforts.

## Configuration

1. Clone project

   ```
   git clone https://github.com/sammrai/VpnProxyAdapter.git
   ```

1. Setting up the .env file

   The project root contains a `.env` file with your VPN provider's OpenVPN credentials.

   ```
   OPENVPN_USERNAME=your_username
   OPENVPN_PASSWORD=your_password
   # OPENVPN_PROVIDER=expressvpn
   ```

   - `OPENVPN_USERNAME` and `OPENVPN_PASSWORD` are the OpenVPN (manual configuration) credentials from your VPN provider.
   - `OPENVPN_PROVIDER` (default `expressvpn`) is a folder name in [notFloran/vpn-configs-contrib/openvpn](https://github.com/notFloran/vpn-configs-contrib/tree/main/openvpn), e.g. `surfshark`, `protonvpn`, `windscribe`. Not supported: providers without `.ovpn` files there (`nordvpn`, `pia`, `ipvanish`, `vyprvpn`), and providers whose CA certificates are too weak for OpenSSL 3 (`ironsocket`, `proxpn`, `vpnbook`).
   - Regions are the `.ovpn` file names. ExpressVPN uses the nearest to Tokyo first; other providers use name order. To pick them yourself, set `REGIONS` (see the Gateway section). ExpressVPN region names are listed in [README-configlist.md](./README-configlist.md).
   - Providers limit simultaneous connections. If yours allows fewer than 19, lower `GATEWAY_EXITS` (see [Switching VPN providers](#switching-vpn-providers)).

1. Starting the service

   Run the following command in the project directory to start the service.

   ```bash
   docker compose up -d --build vpnproxy
   ```

This starts several VPN tunnels in one container and exposes them behind a single HTTP proxy port.

### Switching VPN providers

1. Put the provider's OpenVPN credentials and name in `.env`. `GATEWAY_EXITS` and `REGIONS` can go there too.

   ```
   OPENVPN_USERNAME=your_surfshark_openvpn_username
   OPENVPN_PASSWORD=your_surfshark_openvpn_password
   OPENVPN_PROVIDER=surfshark
   # Host 1 + VPN 4. Keep VPN tunnels within the provider's simultaneous connection limit.
   GATEWAY_EXITS=5
   # Optional. Regions whose file name contains these come first.
   REGIONS=jp,sg
   ```

   The name is the folder name in the config repo, in lower case. The credentials are the OpenVPN (manual setup) ones from the provider's dashboard, not your account login.

1. Recreate the container.

   ```bash
   docker compose up -d vpnproxy
   ```

1. Check that the tunnels came up.

   ```bash
   docker logs vpnproxy 2>&1 | grep -E 'プロバイダ|up:|使えるプロバイダ'
   curl -s 127.0.0.1:8545 | jq -r '.tunnels[] | "\(.dev) \(.region) \(.state)"'
   ```

   A wrong provider name stops the container with the list of available names in the log.
   Tunnels stuck in `connecting` or `failed` usually mean wrong credentials or too many simultaneous connections.

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
GATEWAY_EXITS=30 GATEWAY_RATE=15 docker compose up -d vpnproxy
curl -s 127.0.0.1:8545 | jq '{exits: [.exits[] | {name, egress, ok, bad}], tunnels: [.tunnels[] | {dev, region, state}]}'

# 汎用 HTTP プロキシ (HTTP / HTTPS)。接続ごとに出口 IP が変わる
for i in 1 2 3; do curl -s https://api.ipify.org -x localhost:8901; echo; done

# 利用側は URL を 1 つ指定し、合計 (出口数 x RATE) 呼び出し/秒 まで投げてよい。
# JSON-RPC のバッチは長さぶん数える。**バッチを使うほうが速い** (実測 448 対 108 呼び出し/秒)
curl -s 127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}'
```

| 環境変数 | 既定 | 意味 |
| --- | --- | --- |
| `EXITS` (`GATEWAY_EXITS`) | 20 | 出口数。ホスト 1 + VPN EXITS-1 |
| `RATE` (`GATEWAY_RATE`) | 15 | 出口ごとの毎秒**呼び出し数の初期値**。以後は出口ごとに自動で上下する (下の AIMD)。JSON-RPC のバッチは長さぶん消費する |
| `UPSTREAM` | Tenderly 公開 RPC | 転送先 |
| `OPENVPN_PROVIDER` | expressvpn | 設定リポジトリ `openvpn/` のフォルダ名 |
| `REGIONS` | ExpressVPN は東京から近い順、ほかは名前順 | 使う地域をカンマ区切りで (先頭から優先)。設定ファイル名 (`.ovpn` を除く) か、その一部 (`japan` など) |
| `INCLUDE_HOST` (`GATEWAY_INCLUDE_HOST`) | 1 (compose では 0) | 0 でホスト自身を出口に含めない (JSON-RPC 中継のみ。プロキシはホストを使わない)。compose は 0: Tenderly の枠は IP ごとに 1 日 1GB で、家から直結する live と取り合いになる (2026-09-23 に研究の取得が使い切り、live が 429 で止まった) |
| `PROXY_LISTEN` | 0.0.0.0:8118 | 汎用 HTTP プロキシの待受。空で無効 |

レート制限との付き合い方 (2026-09-19 に実測して調整)

- **`RATE` は「呼び出し毎秒」**。上流が数えているのは JSON-RPC の呼び出し数なので、
  20 呼び出しのバッチを 1 件と数えると実効レートが 20 倍になり、全出口が一斉に 429 を踏む
- **流量は出口ごとに自分で見つける (AIMD)。** 通り続ければ 200 呼び出しごとに +1、429 で 0.7 倍。
  **出口ごとに上限が違う** (実測で 15 は全部通るが 25 だと 22% が 429) ので、一律の値にすると
  いちばん弱い出口に全体が引きずられる。`RATE` はその初期値でしかない
- **429 のバーストは 1 回と数える** (`RATE_CUT_GAP` = 2 秒)。減速が効くまでに飛んでいた分が
  まとめて返ってくるので、1 件ごとに掛けると数百 ms で下限まで落ちる (実測で 19 → 2 になった)
- **全出口が休んでいるときは 5 秒だけ待って 429 を返す** (`ACQUIRE_WAIT`)。
  以前は空くまで黙ってブロックしていたので、呼び出し側からは「無言の 60 秒」に見えて減速もできなかった
- **429 が 3 回続き、かつ休ませる時間が張り直しのコスト (`ROTATE_COST` = 20 秒) を超えたら、
  トンネルを張り直して IP を替える。** 制限は IP ごとなので長く休ませるより替えるほうが早い。
  ただし**張り直しは実測で中央 13 秒・90% 点 21 秒かかり、そのあいだ出口が丸ごと消える**ので、
  休みが短いうちに替えると損をする。必ず両者を比べてから替える
- **トンネルは少しずつ張る** (同時に接続中は 2 本まで、開始は 2 秒おき)。30 本を一斉に張ると
  家のルーターの遅延が 0.2 ms から 75〜105 ms まで上がった (2026-09-23)。2 本ずつなら最大 1.5 ms だった
- **上流の 403 は「その IP が弾かれた」**。時間で戻らないので、別の出口でやり直し、3 回続いたら張り直す
  (プロキシの弾かれと同じ扱い。レートは動かさない)

プロキシ (:8118) での IP 張り直し

上流 RPC の 429 とは別に、**プロキシ越しに使っている先から弾かれる**ことがある。これは IP 評価の
問題で時間では戻らないので、待たずに替える (レートは動かさない)。

- **平文 HTTP** は応答の状態行が読めるので、`429` / `403` を受けたら自動で数え、3 回で張り直す
- **HTTPS (CONNECT) は中身が見えないので中継からは気づけない。** 弾かれたと分かるのは利用側だけなので、
  申告用の口を用意してある:

```bash
# 出口 IP を指定して張り直す (dev: "tun3" でも可)。13〜22 秒で別 IP の同じ本数に戻る
curl -s 127.0.0.1:8545/rotate -H 'content-type: application/json' -d '{"egress":"1.2.3.4"}'
# -> {"rotated": "tun3"}
```

実測 (出口 30 本、20 呼び出しのバッチ × 並列 20、30 秒):

| | 変更前 | 固定 RATE=15 | **AIMD (現行)** |
| --- | ---: | ---: | ---: |
| 通った呼び出し | 126/秒 | 448/秒 | **510/秒** |
| 応答の最大 | **91.2 秒** | 1.9 秒 | 6.2 秒 |
| 10 秒を超えた応答 | 4 件 | 0 件 | **0 件** |
| 上流への要求のうち 429 | **41%** | 0.1% | 1.7% |

AIMD の 1.7% は**上限を探るために意図的に触っている分**。手で調整しなくても、
出口ごとに 14〜21 呼び出し/秒へ勝手に収束する (初期値 10 から 3 分ほど)。

仕組み

- コンテナ内で OpenVPN を tun0, tun1, ... に張る。サーバーが配る既定経路は受け取らず、
  トンネルごとに経路表を分ける。中継は送信ソケットを tun デバイスに結びつけるので、その通信だけがトンネルを通る
- VPN の接続は張りっぱなし。回すのは送り先の出口だけで、各出口の HTTPS 接続も使い回す
- 429 / 5xx / 接続失敗の出口はしばらく休ませ、別の出口でやり直す
- 接続先の名前が引けない地域は飛ばす。出口 IP が重複したら別の地域に張り替える。落ちたら張り直す
- 8545 は本文をそのまま転送先へ POST する中継 (JSON-RPC 向け)
- 8901 (コンテナ内 8118) は汎用 HTTP プロキシ。接続ごとに VPN の出口をラウンドロビンで選ぶ。
  keep-alive の接続は張っている間同じ出口のまま。名前解決はトンネルを通さない
- 設定はイメージに全プロバイダ分入っている。経路を足す指定 (`redirect-gateway` / `route`) と OpenVPN 2.6 で消えた指定は
  ビルド時に `sanitize_ovpn.sh` で消す。それでも繋がらない設定は、失敗した地域として飛ばして次の地域に張る
- 同時接続の上限はプロバイダごとに違う。ExpressVPN は実測で VPN 20 本張っても既存の接続は切れなかった
- `thailand` は接続までは通るが外に出られないことがあり、優先の一覧から外している (出口 IP が取れないトンネルは自動で別の地域に張り替える)

テスト: `docker compose build vpnproxy && docker run --rm --entrypoint pytest vpnproxy:local -q`
