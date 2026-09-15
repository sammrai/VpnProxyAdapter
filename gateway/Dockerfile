FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openvpn iproute2 git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ExpressVPN の地域設定。証明書と鍵は各ファイルに埋め込まれている。
# OpenVPN 2.6 で削除された ns-cert-type と、Windows 専用の route-method だけ直す
ARG CONFIG_REPO=https://github.com/notFloran/vpn-configs-contrib.git
RUN git clone --depth 1 "$CONFIG_REPO" /tmp/cfg \
 && mkdir -p /configs \
 && cp -r /tmp/cfg/openvpn/expressvpn /configs/expressvpn \
 && rm -rf /tmp/cfg \
 && sed -i -e 's/^ns-cert-type server/remote-cert-tls server/' -e '/^route-method /d' /configs/expressvpn/*.ovpn

RUN pip install --no-cache-dir requests==2.32.3 pytest==8.3.3

WORKDIR /app
COPY gateway.py tunnels.py proxy.py tun_up.sh test_gateway.py test_proxy.py ./
RUN chmod +x tun_up.sh
ENTRYPOINT ["python", "gateway.py"]
