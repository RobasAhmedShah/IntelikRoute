# Huawei Automation Plan

Goal:

```text
58.65.197.74:8090 -> 192.168.18.56:8090 -> 192.168.1.140:8090
```

The Mac can automate the second hop today with `upnpc`:

```text
192.168.18.56:8090 -> 192.168.1.140:8090
```

The missing piece is the Huawei EG8247H5 first hop.

## Recommended Router State

On the Huawei:

1. Disable `Application -> UPnP`.
2. Remove every UPnP rule pointing to the camera/NVR IP, currently `192.168.18.119`.
3. Add only a manual port mapping for the test page:

```text
Description   IntelikRoute-Test
External Port 8090
Internal Port 8090
Protocol      TCP
Internal IP   192.168.18.56
Status        Enable
```

This avoids publishing camera/NVR ports such as `80`, `443`, `554`, `37777`, and `161`.

## CLI Automation

The direct Huawei web API path is implemented in `intelikroute.py`.

Disable Huawei UPnP:

```bash
HUAWEI_USER='Epuser' HUAWEI_PASS='your-router-password' \
  python3 intelikroute.py huawei-upnp --disable
```

Create/update the public test-page mapping:

```bash
HUAWEI_USER='Epuser' HUAWEI_PASS='your-router-password' \
  python3 intelikroute.py huawei-publish --port 8090 --internal-ip 192.168.18.56
```

Current known login details from the firmware:

```text
Login URL: /login.cgi
Token source: /asp/GetRandCount.asp
Username field: UserName
Password field: PassWord, base64 encoded
Token field: x.X_HW_Token
```

Port mapping endpoint details discovered from the authenticated page:

```text
Existing mapping update: complexajax.cgi
New mapping create: addcfgajax.cgi
UPnP toggle: set.cgi
```

## Topology Fix Option

Put the Mac or the main router on the Huawei subnet, or put the TP-Link into AP mode. Then
Huawei UPnP may become directly discoverable and the existing `upnpc` flow may be enough.

## Current Discovery Result

From the Mac, SSDP only discovers the TP-Link:

```text
LOCATION: http://192.168.1.1:1900/rootDesc.xml
SERVER: TP-Link/TP-LINK UPnP/1.1 MiniUPnPd/1.8
```

Direct unicast SSDP to `192.168.18.1:1900` times out.
