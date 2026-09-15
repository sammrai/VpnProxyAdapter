#!/bin/sh
# OpenVPN の --up から呼ばれる。$dev (tun<i>) と $TABLE (100+i) は OpenVPN が渡す。
# このデバイスに結びつけた通信だけを、このトンネルに流す。既定の経路は変えない。
set -e
ip route replace default dev "$dev" table "$TABLE"
ip rule del oif "$dev" table "$TABLE" 2>/dev/null || true
ip rule add oif "$dev" table "$TABLE" priority "$TABLE"
