FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openvpn iproute2 git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY sanitize_ovpn.sh ./

# プロバイダごとの OpenVPN 設定を /configs/<プロバイダ>/ に置く。OPENVPN_PROVIDER でフォルダを選ぶ。
# 経路を奪う指定や OpenVPN 2.6 で消えた指定は sanitize_ovpn.sh で直す。
# default.ovpn などのシンボリックリンクは別の地域と同じサーバーを指すだけで、リンク先が無いものもあるので消す
ARG CONFIG_REPO=https://github.com/notFloran/vpn-configs-contrib.git
RUN git clone --depth 1 "$CONFIG_REPO" /tmp/cfg \
 && mv /tmp/cfg/openvpn /configs \
 && rm -rf /tmp/cfg \
 && find /configs -name '*.ovpn' -type l -delete \
 && find /configs -name '*.ovpn' -type f -print0 | xargs -0 sh ./sanitize_ovpn.sh

RUN pip install --no-cache-dir requests==2.32.3 pytest==8.3.3

COPY gateway.py tunnels.py proxy.py tun_up.sh test_gateway.py test_proxy.py ./
RUN chmod +x tun_up.sh
ENTRYPOINT ["python", "gateway.py"]
