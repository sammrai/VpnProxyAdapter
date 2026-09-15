#!/bin/sh
# 配布されている .ovpn を、1 コンテナで何本も張る使い方に合わせて直す。引数のファイルをその場で書き換える。
# - 既定経路や経路を足す指定を消す。--route-nopull はサーバーが配る経路しか止めないので、設定側の指定はここで消す。
#   経路はトンネルごとの経路表に tun_up.sh が入れる
# - --up / --down は gateway が渡すので、設定側のものは消す
# - OpenVPN 2.6 で消えた指定と Windows 専用の指定を消す / 置き換える
# - /etc/openvpn/<プロバイダ>/ca.crt のような絶対パスを、設定のあるフォルダからの相対パスにする (openvpn は --cd で起動する)
set -e
[ $# -gt 0 ] || exit 0
sed -i -E \
  -e 's/^[[:space:]]*ns-cert-type[[:space:]]+server/remote-cert-tls server/' \
  -e 's#/etc/openvpn/[^/[:space:]]+/##g' \
  -e '/^[[:space:]]*(redirect-gateway|route|route-ipv6|route-method|route-delay|block-outside-dns|register-dns|keysize|key-method|ncp-disable|tls-remote|up|down|script-security)([[:space:]]|$)/d' \
  "$@"
