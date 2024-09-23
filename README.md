# VpnProxyAdapter

## Overview

VpnProxyAdapter is a Docker-based solution that facilitates internet connections through VPN. The primary goal of this project is to centralize VPN settings, allowing other programs or services to utilize VPN connections simply by specifying proxy settings. This eliminates the need for individual programs to configure VPN settings, significantly reducing setup efforts.

## Configuration

1. Clone project

   ```
   git clone https://github.com/sammrai/VpnProxyAdapter.git
   ```

1. Setting up the .env file

   The project root contains a `.env` file used to set environment variables for the VPN connection. Below is a sample `.env` file for using ExpressVPN.

   ```
   OPENVPN_USERNAME=your_username
   OPENVPN_PASSWORD=your_password
   OPENVPN_CONFIG=my_expressvpn_hong_kong_-_2_udp
   OPENVPN_PROVIDER=EXPRESSVPN
   ```

   - `OPENVPN_USERNAME` and `OPENVPN_PASSWORD` are the username and password provided by your VPN provider.
   - `OPENVPN_PROVIDER` is the name of the VPN provider you are using. While this project uses ExpressVPN as an example, you can choose any provider from the [list of supported VPN providers](https://github.com/haugene/docker-transmission-openvpn/blob/master/docs/supported-providers.md#external-providers).
   - `OPENVPN_CONFIG` is the name of the VPN configuration. For ExpressVPN, you can select from the [Expressvpn-Proxy-Adapter ovpn_list](https://github.com/DoganM95/Expressvpn-Proxy-Adapter/blob/master/ovpn_list).

1. Starting the service

   Run the following command in the project directory to start the service.

   ```bash
   docker-compose up -d
   ```

This will launch the VPN connection and proxy service in the background, operating based on the configured `.env` file.

## Usage

### 1. Using from within a container

Use the demo container `your_container` to verify that the IP address changes via VPN.

```bash
docker exec -it your_container curl ipinfo.io
```

### 2. Using from outside a container

After commenting in the `ports` setting of the `vpnproxy` service in the `docker-compose.yml` file to expose the proxy port to the host, use the following command to verify that the IP address changes via VPN.

```bash
curl ipinfo.io -x localhost:8901
```

### Note

* This project is intended for personal use. When using it, please comply with the terms of service of your VPN provider.
* This project is inspired by the following repository: https://github.com/DoganM95/Expressvpn-Proxy-Adapter


## Additional Information

If you're interested in trying ExpressVPN, registering through this link will grant you a 30-day free trial. For more details, please visit [ExpressVPN 30-Day Free Trial](https://www.expressrefer.com/refer-a-friend/30-days-free?locale=jp&referrer_id=96807179&utm_campaign=referrals&utm_medium=copy_link&utm_source=referral_dashboard).
