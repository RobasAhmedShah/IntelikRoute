# IntelikRoute

Command-line helper for managing local UPnP port forwards with `upnpc`.

For the full operator/agent runbook, see [SKILL.md](/Users/officeintelik/Documents/IntelikRoute/SKILL.md).

Run the management dashboard:

```bash
export HUAWEI_USER='Epuser'
export HUAWEI_PASS='your-router-password'
python3 intelikroute.py dashboard --port 5050
```

Open:

```text
http://127.0.0.1:5050/
```

The dashboard is now the preferred operator surface. Use the wizard to publish a
domain to a local service without typing a port:

```text
domain employees open -> local backend host:port -> Publish Domain
```

The wizard applies the correct order for this network:

```text
Huawei Static DNS
Huawei UPnP disabled
Huawei port 80 -> TP-Link WAN IP
TP-Link port 80 -> Mac proxy 8080
Proxy host route -> local backend port
```

This is designed for your current network shape:

```text
Internet 58.65.197.74
  -> Huawei EG8247H5 192.168.18.1
  -> TP-Link Archer C60 192.168.1.1
  -> Mac 192.168.1.140
```

The CLI can currently automate the UPnP router visible from the Mac, which is the Archer C60. In testing, that router reports `192.168.18.56` as its external IP. Because your actual internet IP is `58.65.197.74`, you still need a Huawei rule for each public route:

```text
58.65.197.74:PORT -> 192.168.18.56:PORT -> 192.168.1.140:LOCAL_PORT
```

## Quick Start

Check the live topology:

```bash
python3 intelikroute.py doctor
```

List current UPnP mappings:

```bash
python3 intelikroute.py list
```

Add a route:

```bash
python3 intelikroute.py add web --public 8080 --local 3000
```

Remove a saved route:

```bash
python3 intelikroute.py remove web
```

Apply all saved routes from `routes.json`:

```bash
cp routes.example.json routes.json
python3 intelikroute.py apply
```

Verify saved routes:

```bash
python3 intelikroute.py verify
```

## Publish The Test Page Publicly

The full automated flow is two hops:

```bash
export HUAWEI_USER='Epuser'
export HUAWEI_PASS='your-router-password'

# Stop cameras/NVRs from auto-publishing ports on the Huawei.
python3 intelikroute.py huawei-upnp --disable

# Ensure the Huawei forwards public 8090 to the TP-Link/C60 WAN IP.
python3 intelikroute.py huawei-publish --port 8090 --internal-ip 192.168.18.56

# Ensure the TP-Link/C60 forwards 8090 to this Mac.
python3 intelikroute.py add web-test --public 8090 --local 8090 --lease 0
```

Or run the whole routing setup as one command:

```bash
HUAWEI_USER='Epuser' HUAWEI_PASS='your-router-password' \
  python3 intelikroute.py publish-public --port 8090 --internal-ip 192.168.18.56
```

Then serve only the dedicated test folder:

```bash
python3 -m http.server 8090 --bind 0.0.0.0 --directory /Users/officeintelik/Documents/IntelikRoute/web-test
```

Public URL:

```text
http://58.65.197.74:8090/
```

## Important Huawei Note

`upnpc` discovery does not cross your NAT layers. From the Mac it finds:

```text
desc: http://192.168.1.1:1900/rootDesc.xml
UPnP external: 192.168.18.56
```

The CLI now handles the Huawei layer through the router web API, while still using `upnpc` for the TP-Link/C60 layer.

Keep Huawei UPnP disabled if you do not want cameras/NVRs to recreate public mappings for `80`, `443`, `554`, `37777`, or `161`.

## Current Clean State

After cleanup, IntelikRoute-managed Huawei routes, Huawei DNS entries, proxy routes, and local proxy/test servers are removed. The dashboard remains available at `http://127.0.0.1:5050/`.

## Safer Defaults

Only expose services that have authentication. For development apps, prefer HTTPS plus an app password, VPN, WireGuard, Cloudflare Tunnel, or Tailscale Funnel when the route will be online for more than short testing.
