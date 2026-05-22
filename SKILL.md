---
name: intelikroute
description: Operate the IntelikRoute CLI for Huawei EG8247H5 public forwarding, TP-Link UPnP forwarding, Huawei Static DNS, and host-header reverse proxy routing.
---

# IntelikRoute Skill

Use this skill when managing this office network's local/public service exposure from `/Users/officeintelik/Documents/IntelikRoute`.

The system automates a cascaded NAT topology:

```text
Internet 58.65.197.74
  -> Huawei EG8247H5 / ONT 192.168.18.1
  -> TP-Link Archer C60 WAN 192.168.18.56
  -> Mac 192.168.1.140
```

## Safety Rules

- Keep Huawei UPnP disabled unless explicitly testing it. Camera/NVR devices previously auto-created public rules for `80`, `443`, `554`, `37777`, and `161`.
- Prefer `publish-public` for normal publishing. It disables Huawei UPnP, ensures the Huawei public mapping, and ensures the TP-Link UPnP mapping.
- Do not expose development servers without authentication unless it is a short-lived test page.
- Use `HUAWEI_USER` and `HUAWEI_PASS` environment variables instead of passing router credentials in shell history.
- For no-port domain access, use DNS plus the reverse proxy. DNS chooses only the IP; the proxy chooses the backend port.

## Environment

```bash
cd /Users/officeintelik/Documents/IntelikRoute
export HUAWEI_USER='Epuser'
export HUAWEI_PASS='<router-password>'
```

Router defaults:

```text
Huawei URL: http://192.168.18.1
Huawei public-side target: 192.168.18.56
Mac LAN IP: 192.168.1.140
Public IP: 58.65.197.74
Proxy port on Mac: 8080
```

## Mental Model

Public port publishing requires two forwarding layers:

```text
58.65.197.74:PORT
  -> Huawei port mapping
192.168.18.56:PORT
  -> TP-Link UPnP mapping
192.168.1.140:LOCAL_PORT
```

No-port domain routing requires DNS plus reverse proxy:

```text
intelik.network -> 192.168.18.56
192.168.18.56:80 -> 192.168.1.140:8080
Host: intelik.network -> proxy-routes.json -> http://127.0.0.1:8181
```

## Implemented Files

- `intelikroute.py`: main CLI for diagnostics, TP-Link UPnP, Huawei port mappings, Huawei Static DNS.
- `intelik_proxy.py`: compatibility wrapper that calls `intelikroute.py proxy`.
- `intelik_dashboard.py`: dependency-free web dashboard used by `intelikroute.py dashboard`.
- `proxy-routes.json`: domain-to-backend route table for the reverse proxy.
- `routes.example.json`: example saved UPnP routes.
- `web-test/`: `8090` IntelikRoute test page.
- `web-test-8181/`: `8181` Welcome to Intelik test page.
- `README.md`: human quick-start.
- `HUAWEI_AUTOMATION.md`: Huawei API notes.

## Best-Practice Workflows

### Run The Management Dashboard

Start the dashboard with Huawei credentials in the environment:

```bash
export HUAWEI_USER='Epuser'
export HUAWEI_PASS='<router-password>'
python3 intelikroute.py dashboard --port 5050
```

Open:

```text
http://127.0.0.1:5050/
```

The dashboard shows:

```text
Guided no-port domain publishing wizard
Managed route count
Huawei Static DNS records
Proxy service controls
Proxy routes
Forwarding route tables
Advanced cleanup actions
```

Preferred operator workflow:

```text
1. Start the dashboard.
2. Enter the domain employees should open.
3. Enter the local backend host and port.
4. Click Publish Domain.
```

The wizard applies the route in this order:

```text
Huawei Static DNS -> Huawei port 80 -> TP-Link port 80 -> proxy route -> proxy service
```

### Diagnose Network State

```bash
python3 intelikroute.py doctor
python3 intelikroute.py list
python3 intelikroute.py huawei-list
python3 intelikroute.py huawei-dns-list
```

### Publish a Public Port

Use this for a backend that should remain accessible by explicit port:

```bash
python3 intelikroute.py publish-public --port 8181 --internal-ip 192.168.18.56
```

This ensures:

```text
Huawei UPnP disabled
Huawei: 58.65.197.74:8181 -> 192.168.18.56:8181
TP-Link: 192.168.18.56:8181 -> 192.168.1.140:8181
```

### Publish a Domain Without a Port

Use the dashboard wizard first. Manual equivalent:

1. Add Huawei local DNS:

```bash
python3 intelikroute.py huawei-dns-add intelik.network 192.168.18.56
```

2. Ensure port `80` reaches the proxy:

```bash
python3 intelikroute.py publish-public --port 80 --internal-ip 192.168.18.56 --name proxy-http
python3 intelikroute.py add proxy-http --public 80 --local 8080 --lease 0 --no-save
```

3. Add the host route in `proxy-routes.json`:

```json
{
  "routes": {
    "intelik.network": "http://127.0.0.1:8181"
  }
}
```

4. Start the backend and proxy:

```bash
python3 -m http.server 8181 --bind 0.0.0.0 --directory /Users/officeintelik/Documents/IntelikRoute/web-test-8181
python3 intelikroute.py proxy --port 8080
```

5. Test:

```bash
dig +short @192.168.18.1 intelik.network
curl -i http://intelik.network/
```

Expected current route:

```text
intelik.network -> 192.168.18.56
intelik.network HTTP -> 127.0.0.1:8181
```

## CLI Manual

Top-level help:

```bash
python3 intelikroute.py -h
```

Current commands:

```text
doctor
list
add
remove
apply
verify
publish-test
huawei-upnp
huawei-publish
huawei-list
huawei-remove
publish-public
huawei-dns-list
huawei-dns-add
huawei-dns-remove
proxy
dashboard
```

### `doctor`

Show topology and nested NAT diagnostics.

```bash
python3 intelikroute.py doctor
```

Syntax:

```text
usage: intelikroute doctor [-h]
```

### `list`

List active TP-Link/C60 UPnP mappings visible from the Mac.

```bash
python3 intelikroute.py list
python3 intelikroute.py list --no-warn
```

Syntax:

```text
usage: intelikroute list [-h] [--no-warn]
```

Flags:

- `--no-warn`: skip public-IP comparison.

### `add`

Add a TP-Link/C60 UPnP mapping.

```bash
python3 intelikroute.py add web --public 8181 --local 8181 --lease 0
python3 intelikroute.py add proxy-http --public 80 --local 8080 --lease 0 --no-save
```

Syntax:

```text
usage: intelikroute add [-h] --public PUBLIC --local LOCAL [--host HOST]
                        [--proto PROTO] [--lease LEASE]
                        [--description DESCRIPTION] [--no-save]
                        name
```

Flags:

- `name`: saved route name.
- `--public`: external port on the UPnP router.
- `--local`: local/backend port.
- `--host`: internal host IP or `auto`; default `auto`.
- `--proto`: `tcp` or `udp`; default `tcp`.
- `--lease`: lease seconds; `0` means permanent when supported.
- `--description`: UPnP description.
- `--no-save`: apply without writing `routes.json`.

### `remove`

Remove a TP-Link/C60 UPnP mapping.

```bash
python3 intelikroute.py remove web
python3 intelikroute.py remove --public 8181 --proto tcp
```

Syntax:

```text
usage: intelikroute remove [-h] [--public PUBLIC] [--proto PROTO] [name]
```

### `apply`

Apply all routes from `routes.json`.

```bash
python3 intelikroute.py apply
```

### `verify`

Verify saved routes from `routes.json`.

```bash
python3 intelikroute.py verify
python3 intelikroute.py verify web
```

### `publish-test`

Apply only the inner TP-Link route and print the Huawei rule that would be required. This is mainly diagnostic now that `publish-public` exists.

```bash
python3 intelikroute.py publish-test --port 8181 --lease 600 --name web-test
```

Syntax:

```text
usage: intelikroute publish-test [-h] [--port PORT] [--lease LEASE] [--name NAME]
```

### `huawei-upnp`

Enable or disable Huawei UPnP.

```bash
python3 intelikroute.py huawei-upnp --disable
python3 intelikroute.py huawei-upnp --enable
```

Syntax:

```text
usage: intelikroute huawei-upnp [-h] [--base-url BASE_URL]
                                [--username USERNAME] [--password PASSWORD]
                                (--enable | --disable)
```

Flags:

- `--base-url`: Huawei router URL; default `http://192.168.18.1`.
- `--username`: Huawei username; prefer `HUAWEI_USER`.
- `--password`: Huawei password; prefer `HUAWEI_PASS`.
- `--enable` / `--disable`: desired UPnP state.

### `huawei-list`

List Huawei public port mappings.

```bash
python3 intelikroute.py huawei-list
```

Syntax:

```text
usage: intelikroute huawei-list [-h] [--base-url BASE_URL]
                                [--username USERNAME] [--password PASSWORD]
```

### `huawei-publish`

Create or append a Huawei public port mapping row.

```bash
python3 intelikroute.py huawei-publish --port 8181 --internal-ip 192.168.18.56 --name intelikroute-8181
```

Syntax:

```text
usage: intelikroute huawei-publish [-h] [--base-url BASE_URL]
                                   [--username USERNAME] [--password PASSWORD]
                                   [--port PORT] [--internal-ip INTERNAL_IP]
                                   [--name NAME]
```

Behavior:

- If the internal host has no Huawei mapping, create one.
- If the internal host already has a Huawei mapping, append a new port row.
- If that exact port already exists, do nothing.

### `huawei-remove`

Remove one Huawei public port mapping row.

```bash
python3 intelikroute.py huawei-remove --port 8181 --internal-ip 192.168.18.56
```

Syntax:

```text
usage: intelikroute huawei-remove [-h] [--base-url BASE_URL]
                                  [--username USERNAME] [--password PASSWORD]
                                  --port PORT [--proto PROTO]
                                  [--internal-ip INTERNAL_IP]
```

Flags:

- `--port`: external port to remove.
- `--proto`: `tcp` or `udp`; default `tcp`.
- `--internal-ip`: optional filter, usually `192.168.18.56`.

### `publish-public`

Best-practice one-command public publish.

```bash
python3 intelikroute.py publish-public --port 8181 --internal-ip 192.168.18.56
```

Syntax:

```text
usage: intelikroute publish-public [-h] [--base-url BASE_URL]
                                   [--username USERNAME] [--password PASSWORD]
                                   [--port PORT] [--internal-ip INTERNAL_IP]
                                   [--name NAME]
```

This runs:

```text
huawei-upnp --disable
huawei-publish --port PORT --internal-ip INTERNAL_IP
add NAME --public PORT --local PORT
```

### `huawei-dns-list`

List Huawei Static DNS host entries.

```bash
python3 intelikroute.py huawei-dns-list
```

Syntax:

```text
usage: intelikroute huawei-dns-list [-h] [--base-url BASE_URL]
                                    [--username USERNAME]
                                    [--password PASSWORD]
```

### `huawei-dns-add`

Add a Huawei Static DNS host entry.

```bash
python3 intelikroute.py huawei-dns-add intelik.network 192.168.18.56
```

Syntax:

```text
usage: intelikroute huawei-dns-add [-h] [--base-url BASE_URL]
                                   [--username USERNAME] [--password PASSWORD]
                                   domain ip
```

Notes:

- DNS maps hostnames to IPs, not ports.
- Use the reverse proxy when hostnames should route to different backend ports.

### `huawei-dns-remove`

Remove a Huawei Static DNS host entry.

```bash
python3 intelikroute.py huawei-dns-remove intelik.network
```

Syntax:

```text
usage: intelikroute huawei-dns-remove [-h] [--base-url BASE_URL]
                                      [--username USERNAME]
                                      [--password PASSWORD]
                                      domain
```

## Reverse Proxy Manual

Start the host-header proxy:

```bash
python3 intelikroute.py proxy --port 8080
```

Syntax:

```text
usage: intelikroute proxy [-h] [--proxy-config PROXY_CONFIG] [--host HOST] [--port PORT]
```

Flags:

- `--proxy-config`: route table JSON; default `proxy-routes.json`.
- `--host`: bind address; default `0.0.0.0`.
- `--port`: proxy listen port; default `8080`.

Compatibility wrapper:

```bash
python3 intelik_proxy.py --port 8080
```

## Dashboard Manual

Start the network management dashboard:

```bash
python3 intelikroute.py dashboard --port 5050
```

Syntax:

```text
usage: intelikroute dashboard [-h] [--host HOST] [--port PORT]
```

Flags:

- `--host`: bind address; default `0.0.0.0`.
- `--port`: dashboard listen port; default `5050`.

The dashboard API uses the same CLI capability layer:

```text
/api/status
/api/wizard/domain
/api/publish
/api/huawei/remove
/api/upnp/disable
/api/dns/add
/api/dns/remove
/api/proxy/start
/api/proxy/stop
/api/proxy/add
/api/proxy/remove
```

Route table format:

```json
{
  "routes": {
    "intelik.network": "http://127.0.0.1:8181",
    "test.intelik.lan": "http://127.0.0.1:8090"
  }
}
```

## Current Implemented Routes

Huawei public port mappings currently include:

```text
No IntelikRoute Huawei public mappings are currently configured after cleanup.
```

TP-Link/C60 UPnP mappings recently observed include:

```text
8082 -> 192.168.1.140:8082
```

The remaining `8082` rule was pre-existing and is not managed by IntelikRoute.

Huawei Static DNS currently includes:

```text
No Huawei Static DNS entries are currently configured after cleanup.
```

Reverse proxy currently maps:

```text
No IntelikRoute proxy routes are currently configured after cleanup.
```

## Verification Commands

```bash
python3 -m py_compile intelikroute.py intelik_proxy.py intelik_dashboard.py
python3 intelikroute.py doctor
python3 intelikroute.py list --no-warn
python3 intelikroute.py huawei-list
python3 intelikroute.py huawei-dns-list
dig +short @192.168.18.1 intelik.network
curl -i http://intelik.network/
curl -i http://127.0.0.1:5050/
```

## Troubleshooting

- If `http://intelik.network/` fails but `dig @192.168.18.1 intelik.network` works, check that the proxy is running on `8080`.
- If direct public `:PORT` access fails, check both `huawei-list` and `list`.
- If camera/NVR ports reappear in Huawei UPnP, run `huawei-upnp --disable` again and disable UPnP/NAT auto mapping inside the camera/NVR.
- If employees on downstream routers cannot resolve Huawei Static DNS names, configure those routers' DHCP DNS to `192.168.18.1`, or run a dedicated internal DNS service.
