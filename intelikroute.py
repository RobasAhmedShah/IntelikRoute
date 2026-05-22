#!/usr/bin/env python3
"""Small CLI for managing local UPnP port forwards with miniupnpc."""

from __future__ import annotations

import argparse
import base64
import http.client
import http.cookiejar
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path("routes.json")
DEFAULT_DESC_PREFIX = "intelikroute"
UPNPC_TIMEOUT = 25
HUAWEI_BASE_URL = "http://192.168.18.1"
DEFAULT_PROXY_CONFIG = Path("proxy-routes.json")
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


@dataclass
class Mapping:
    protocol: str
    public_port: int
    internal_host: str
    local_port: int
    description: str
    remote_host: str
    lease: int


@dataclass
class UpnpState:
    raw: str
    discovered_url: str | None
    local_ip: str | None
    external_ip: str | None
    mappings: list[Mapping]


class CliError(Exception):
    pass


class ProxyHandler(BaseHTTPRequestHandler):
    routes: dict[str, str] = {}

    def do_GET(self) -> None:
        self.proxy()

    def do_HEAD(self) -> None:
        self.proxy()

    def do_POST(self) -> None:
        self.proxy()

    def do_PUT(self) -> None:
        self.proxy()

    def do_PATCH(self) -> None:
        self.proxy()

    def do_DELETE(self) -> None:
        self.proxy()

    def proxy(self) -> None:
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        target = self.routes.get(host)
        if not target:
            self.send_error(404, f"No proxy route for host {host or '<missing>'}")
            return

        parsed = urllib.parse.urlparse(target)
        if parsed.scheme != "http":
            self.send_error(502, "Only http backends are supported by this lightweight proxy")
            return

        backend_host = parsed.hostname or "127.0.0.1"
        backend_port = parsed.port or 80
        backend_prefix = parsed.path.rstrip("/")
        backend_path = backend_prefix + self.path

        body = None
        if self.command in {"POST", "PUT", "PATCH"}:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else None

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
        }
        headers["Host"] = f"{backend_host}:{backend_port}"
        headers["X-Forwarded-Host"] = host
        headers["X-Forwarded-Proto"] = "http"

        try:
            conn = http.client.HTTPConnection(backend_host, backend_port, timeout=20)
            conn.request(self.command, backend_path, body=body, headers=headers)
            response = conn.getresponse()
            response_body = response.read()
        except OSError as exc:
            self.send_error(502, f"Backend unavailable: {exc}")
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

        self.send_response(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() not in HOP_BY_HOP_HEADERS:
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response_body)


class HuaweiClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )

    def request(
        self,
        path: str,
        *,
        data: dict[str, str] | None = None,
        timeout: int = 10,
    ) -> str:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        body = None
        headers = {
            "User-Agent": "IntelikRoute/1.0",
            "Referer": f"{self.base_url}/",
        }
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", "replace")
            if exc.code == 404 and "html/ipv6/not_find_file.asp" in url:
                return body_text
            raise

    def login(self) -> None:
        self.request("/")
        token = self.request("/asp/GetRandCount.asp").strip().lstrip("\ufeff")
        if not token:
            raise CliError("Huawei login token was empty.")
        self.request(
            "/login.cgi",
            data={
                "UserName": self.username,
                "PassWord": base64.b64encode(self.password.encode("utf-8")).decode("ascii"),
                "Language": "english",
                "x.X_HW_Token": token,
            },
        )
        page = self.request("/html/bbsp/portmapping/portmappingnew.asp")
        if "PortMapping" not in page and "Port Mapping" not in page:
            raise CliError("Huawei login did not reach the authenticated port mapping page.")

    def page(self, path: str) -> str:
        return self.request(path)

    def post(self, path: str, data: dict[str, str]) -> str:
        return self.request(path, data=data)


def huawei_credentials(args: argparse.Namespace) -> tuple[str, str]:
    username = args.username or os.environ.get("HUAWEI_USER")
    password = args.password or os.environ.get("HUAWEI_PASS")
    if not username or not password:
        raise CliError("Set HUAWEI_USER and HUAWEI_PASS, or pass --username and --password.")
    return username, password


def huawei_client(args: argparse.Namespace) -> HuaweiClient:
    username, password = huawei_credentials(args)
    client = HuaweiClient(args.base_url, username, password)
    client.login()
    return client


def extract_value(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text, re.S)
    if not match:
        raise CliError(f"Could not find {label} in Huawei page.")
    return bytes(match.group(1), "utf-8").decode("unicode_escape")


def extract_token(page: str) -> str:
    return extract_value(r'id=["\']hwonttoken["\'][^>]*value=["\']([^"\']+)', page, "Huawei token")


def extract_named_token(page: str) -> str:
    return extract_value(
        r'name=["\']onttoken["\'][^>]*value=["\']([^"\']+)|id=["\']hwonttoken["\'][^>]*value=["\']([^"\']+)',
        page,
        "Huawei token",
    )


def extract_port_mapping_domain(page: str) -> str:
    return extract_value(r'new stPortMap\("([^"]+)","1"[^)]*?"192\\x2e168\\x2e18\\x2e56"', page, "port mapping domain")


def extract_wan_interface(page: str) -> str:
    domain = extract_port_mapping_domain(page)
    return domain.rsplit(".PortMapping", 1)[0]


def extract_portlist_inst(page: str) -> str | None:
    match = re.search(r'new stPortMappingPortList\("([^"]+\.X_HW_Portlist\.\d+)"', page)
    return bytes(match.group(1), "utf-8").decode("unicode_escape") if match else None


def decode_js_escaped(value: str) -> str:
    return bytes(value, "utf-8").decode("unicode_escape")


def huawei_port_mappings(page: str) -> list[dict[str, str]]:
    mappings: dict[str, dict[str, str]] = {}
    map_re = re.compile(
        r'new stPortMap\("(?P<domain>[^"]+)","(?P<enabled>[^"]*)","(?P<remote>[^"]*)",'
        r'"(?P<remote_range>[^"]*)","(?P<rule>[^"]*)","(?P<client>[^"]*)",'
        r'"(?P<description>[^"]*)","(?P<external_ip>[^"]*)"\)'
    )
    for match in map_re.finditer(page):
        domain = decode_js_escaped(match.group("domain"))
        mappings[domain] = {
            "domain": domain,
            "enabled": decode_js_escaped(match.group("enabled")),
            "client": decode_js_escaped(match.group("client")),
            "description": decode_js_escaped(match.group("description")),
            "ports": [],
        }

    port_re = re.compile(
        r'new stPortMappingPortList\("(?P<domain>[^"]+)","(?P<protocol>[^"]+)",'
        r'"(?P<internal>[^"]*)","(?P<external>[^"]*)","(?P<src>[^"]*)"\)'
    )
    for match in port_re.finditer(page):
        portlist_domain = decode_js_escaped(match.group("domain"))
        parent = portlist_domain.rsplit(".X_HW_Portlist", 1)[0]
        if parent in mappings:
            port = {
                "portlist_domain": portlist_domain,
                "protocol": decode_js_escaped(match.group("protocol")),
                "internal_port": decode_js_escaped(match.group("internal")),
                "external_port": decode_js_escaped(match.group("external")),
            }
            mappings[parent]["ports"].append(port)
            mappings[parent].update(port)
    return list(mappings.values())


def port_range_matches(value: str | None, port: int) -> bool:
    return value in {str(port), f"{port}:{port}"}


def huawei_dns_hosts(page: str) -> list[dict[str, str]]:
    host_re = re.compile(r'new DnsHostsItemClass\("([^"]*)","([^"]*)","([^"]*)"\)')
    hosts = []
    for match in host_re.finditer(page):
        if not any(match.group(index) for index in (1, 2, 3)):
            continue
        hosts.append(
            {
                "domain": decode_js_escaped(match.group(1)),
                "ip": decode_js_escaped(match.group(2)),
                "name": decode_js_escaped(match.group(3)),
            }
        )
    return hosts


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=True,
            timeout=UPNPC_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise CliError(f"Command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError(f"Timed out running: {' '.join(cmd)}") from exc

    if check and proc.returncode != 0:
        output = (proc.stdout + proc.stderr).strip()
        raise CliError(output or f"Command failed: {' '.join(cmd)}")
    return proc


def require_upnpc() -> str:
    path = shutil.which("upnpc")
    if not path:
        raise CliError("upnpc is not installed or not on PATH.")
    return path


def parse_upnpc_list(raw: str) -> UpnpState:
    discovered_url = None
    local_ip = None
    external_ip = None
    mappings: list[Mapping] = []

    mapping_re = re.compile(
        r"^\s*\d+\s+"
        r"(?P<proto>TCP|UDP)\s+"
        r"(?P<public>\d+)->(?P<host>[0-9a-fA-F:.]+):(?P<local>\d+)\s+"
        r"'(?P<desc>[^']*)'\s+'(?P<remote>[^']*)'\s+(?P<lease>\d+)"
    )

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("desc: "):
            discovered_url = stripped.removeprefix("desc: ").strip()
        elif stripped.startswith("Local LAN ip address :"):
            local_ip = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("ExternalIPAddress ="):
            external_ip = stripped.split("=", 1)[1].strip()
        else:
            match = mapping_re.match(line)
            if match:
                mappings.append(
                    Mapping(
                        protocol=match.group("proto"),
                        public_port=int(match.group("public")),
                        internal_host=match.group("host"),
                        local_port=int(match.group("local")),
                        description=match.group("desc"),
                        remote_host=match.group("remote"),
                        lease=int(match.group("lease")),
                    )
                )

    return UpnpState(
        raw=raw,
        discovered_url=discovered_url,
        local_ip=local_ip,
        external_ip=external_ip,
        mappings=mappings,
    )


def get_upnp_state() -> UpnpState:
    require_upnpc()
    proc = run(["upnpc", "-l"])
    return parse_upnpc_list(proc.stdout + proc.stderr)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"routes": []}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CliError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("routes"), list):
        raise CliError(f"{path} must contain an object with a routes array.")
    return data


def load_proxy_routes(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise CliError(f"Proxy config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"{path} is not valid JSON: {exc}") from exc
    routes = data.get("routes", {})
    if not isinstance(routes, dict):
        raise CliError(f"{path} must contain an object named routes.")
    return {str(host).lower(): str(target) for host, target in routes.items()}


def save_config(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def normalize_protocol(value: str) -> str:
    protocol = value.upper()
    if protocol not in {"TCP", "UDP"}:
        raise CliError("Protocol must be TCP or UDP.")
    return protocol


def normalize_port(value: int | str, label: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise CliError(f"{label} must be a number.") from exc
    if port < 1 or port > 65535:
        raise CliError(f"{label} must be between 1 and 65535.")
    return port


def route_description(route: dict[str, Any]) -> str:
    return str(route.get("description") or f"{DEFAULT_DESC_PREFIX}:{route['name']}")


def add_mapping(route: dict[str, Any]) -> None:
    host = str(route.get("host") or "auto")
    internal_host = "@" if host == "auto" else host
    local_port = normalize_port(route["local_port"], "local_port")
    public_port = normalize_port(route["public_port"], "public_port")
    protocol = normalize_protocol(str(route.get("protocol", "tcp")))
    lease = int(route.get("lease", 0))
    description = route_description(route)

    cmd = [
        "upnpc",
        "-e",
        description,
        "-a",
        internal_host,
        str(local_port),
        str(public_port),
        protocol,
    ]
    if lease > 0:
        cmd.append(str(lease))
    run(cmd)


def delete_mapping(public_port: int, protocol: str) -> None:
    run(["upnpc", "-d", str(public_port), normalize_protocol(protocol)])


def route_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "name": args.name,
        "host": args.host,
        "local_port": normalize_port(args.local, "local"),
        "public_port": normalize_port(args.public, "public"),
        "protocol": normalize_protocol(args.proto),
        "lease": int(args.lease),
        "description": args.description or f"{DEFAULT_DESC_PREFIX}:{args.name}",
    }


def upsert_route(config: dict[str, Any], route: dict[str, Any]) -> None:
    routes = config["routes"]
    for index, existing in enumerate(routes):
        if existing.get("name") == route["name"]:
            routes[index] = route
            return
    routes.append(route)


def find_route(config: dict[str, Any], name: str) -> dict[str, Any]:
    for route in config["routes"]:
        if route.get("name") == name:
            return route
    raise CliError(f"No route named {name!r} in config.")


def remove_route(config: dict[str, Any], name: str) -> None:
    config["routes"] = [route for route in config["routes"] if route.get("name") != name]


def public_ip() -> str | None:
    urls = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
    ]
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=8) as response:
                value = response.read().decode("utf-8", "replace").strip()
            if value:
                return value
        except OSError:
            continue
    return None


def default_gateway() -> str | None:
    if sys.platform == "darwin":
        proc = run(["route", "-n", "get", "default"], check=False)
        match = re.search(r"gateway:\s+(\S+)", proc.stdout)
        return match.group(1) if match else None

    proc = run(["sh", "-c", "ip route show default 2>/dev/null"], check=False)
    match = re.search(r"default via\s+(\S+)", proc.stdout)
    return match.group(1) if match else None


def local_ip_hint() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def is_private_ip(value: str | None) -> bool:
    if not value:
        return False
    try:
        return ipaddress.ip_address(value).is_private
    except ValueError:
        return False


def print_mapping_table(state: UpnpState) -> None:
    print(f"Discovered IGD : {state.discovered_url or '-'}")
    print(f"Local LAN IP   : {state.local_ip or '-'}")
    print(f"UPnP external : {state.external_ip or '-'}")
    print()

    if not state.mappings:
        print("No UPnP mappings found.")
        return

    rows = [
        [
            mapping.protocol,
            str(mapping.public_port),
            f"{mapping.internal_host}:{mapping.local_port}",
            mapping.description or "-",
            str(mapping.lease),
        ]
        for mapping in state.mappings
    ]
    headers = ["Proto", "Public", "Internal", "Description", "Lease"]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def warn_if_nested(state: UpnpState, internet_ip: str | None) -> None:
    if state.external_ip and internet_ip and state.external_ip != internet_ip:
        print()
        print("Nested NAT warning:")
        print(f"  UPnP can configure {state.external_ip}, but the internet sees {internet_ip}.")
        print("  You still need the upstream Huawei mapping to reach this from outside.")
    elif is_private_ip(state.external_ip):
        print()
        print("Nested NAT warning:")
        print(f"  UPnP external IP is private ({state.external_ip}), not a public internet IP.")


def command_doctor(_: argparse.Namespace) -> None:
    print(f"upnpc        : {require_upnpc()}")
    print(f"Default GW   : {default_gateway() or '-'}")
    print(f"Local IP     : {local_ip_hint() or '-'}")

    internet_ip = public_ip()
    print(f"Internet IP  : {internet_ip or '-'}")
    state = get_upnp_state()
    print(f"IGD URL      : {state.discovered_url or '-'}")
    print(f"UPnP local   : {state.local_ip or '-'}")
    print(f"UPnP external: {state.external_ip or '-'}")
    print(f"Mappings     : {len(state.mappings)}")
    warn_if_nested(state, internet_ip)


def command_list(args: argparse.Namespace) -> None:
    state = get_upnp_state()
    print_mapping_table(state)
    if args.warn:
        warn_if_nested(state, public_ip())


def command_add(args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    config = load_config(config_path)
    route = route_from_args(args)
    add_mapping(route)
    if not args.no_save:
        upsert_route(config, route)
        save_config(config_path, config)
        print(f"Saved route {route['name']!r} to {config_path}")
    print(
        "Added "
        f"{route['protocol']} {route['public_port']} -> "
        f"{route['host']}:{route['local_port']}"
    )


def command_remove(args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    config = load_config(config_path)

    if args.name:
        route = find_route(config, args.name)
        public_port = normalize_port(route["public_port"], "public_port")
        protocol = normalize_protocol(str(route.get("protocol", "tcp")))
        delete_mapping(public_port, protocol)
        remove_route(config, args.name)
        save_config(config_path, config)
        print(f"Removed route {args.name!r} and deleted {protocol} {public_port}")
        return

    if args.public is None:
        raise CliError("Provide a route name or --public PORT.")
    public_port = normalize_port(args.public, "public")
    protocol = normalize_protocol(args.proto)
    delete_mapping(public_port, protocol)
    print(f"Deleted {protocol} {public_port}")


def command_apply(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    if not config["routes"]:
        print("No routes in config.")
        return
    for route in config["routes"]:
        add_mapping(route)
        print(
            f"Applied {route.get('name', '<unnamed>')}: "
            f"{normalize_protocol(str(route.get('protocol', 'tcp')))} "
            f"{route['public_port']} -> {route.get('host', 'auto')}:{route['local_port']}"
        )


def route_matches_mapping(route: dict[str, Any], mapping: Mapping) -> bool:
    if normalize_protocol(str(route.get("protocol", "tcp"))) != mapping.protocol:
        return False
    if normalize_port(route["public_port"], "public_port") != mapping.public_port:
        return False
    if normalize_port(route["local_port"], "local_port") != mapping.local_port:
        return False
    host = str(route.get("host") or "auto")
    return host == "auto" or host == mapping.internal_host


def command_verify(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    routes = config["routes"]
    if args.name:
        routes = [find_route(config, args.name)]

    state = get_upnp_state()
    ok = True
    for route in routes:
        match = next(
            (mapping for mapping in state.mappings if route_matches_mapping(route, mapping)),
            None,
        )
        name = route.get("name", "<unnamed>")
        if match:
            print(f"OK   {name}: {match.protocol} {match.public_port} -> {match.internal_host}:{match.local_port}")
        else:
            ok = False
            print(f"MISS {name}: expected {route.get('protocol', 'tcp').upper()} {route['public_port']} -> {route.get('host', 'auto')}:{route['local_port']}")

    warn_if_nested(state, public_ip())
    if not ok:
        raise CliError("One or more routes are missing from the active UPnP table.")


def command_publish_test(args: argparse.Namespace) -> None:
    route = {
        "name": args.name,
        "host": "auto",
        "local_port": normalize_port(args.port, "port"),
        "public_port": normalize_port(args.port, "port"),
        "protocol": "TCP",
        "lease": int(args.lease),
        "description": f"{DEFAULT_DESC_PREFIX}:{args.name}",
    }
    add_mapping(route)
    state = get_upnp_state()
    internet_ip = public_ip()
    upstream_ip = state.external_ip or "<unknown>"
    print(f"Local app should listen on 0.0.0.0:{args.port}")
    print(f"Inner route applied: {upstream_ip}:{args.port} -> {state.local_ip or 'this-host'}:{args.port}")
    print()
    print("Huawei rule still required:")
    print(f"  External port : {args.port}")
    print(f"  Internal IP   : {upstream_ip}")
    print(f"  Internal port : {args.port}")
    print("  Protocol      : TCP")
    print()
    if internet_ip:
        print(f"After the Huawei rule exists, public URL should be: http://{internet_ip}:{args.port}/")
    warn_if_nested(state, internet_ip)


def command_huawei_upnp(args: argparse.Namespace) -> None:
    client = huawei_client(args)
    page = client.page("/html/bbsp/upnp/upnp.asp")
    token = extract_named_token(page)
    enabled = "1" if args.enabled else "0"
    result = client.post(
        "/set.cgi?x=InternetGatewayDevice.X_HW_MainUPnP&y=InternetGatewayDevice.X_HW_SlvUPnP&RequestFile=html/bbsp/upnp/upnp.asp",
        {
            "x.Enable": enabled,
            "y.Enable": enabled,
            "x.X_HW_Token": token,
        },
    )
    state = "enabled" if args.enabled else "disabled"
    print(f"Huawei UPnP {state}.")
    if '"error"' in result.lower() or "errorcode" in result.lower():
        print(result.strip())


def command_huawei_publish(args: argparse.Namespace) -> None:
    client = huawei_client(args)
    page = client.page("/html/bbsp/portmapping/portmappingnew.asp")
    token = extract_token(page)
    port = normalize_port(args.port, "port")
    internal_ip = args.internal_ip
    mapping_name = args.name or f"intelikroute-{port}"

    existing_host_mapping = None
    for mapping in huawei_port_mappings(page):
        if mapping.get("client") != internal_ip:
            continue
        existing_host_mapping = mapping
        for port_entry in mapping.get("ports", []):
            if (
                port_entry.get("protocol") in {"TCP", "TCP/UDP"}
                and port_range_matches(port_entry.get("internal_port"), port)
                and port_range_matches(port_entry.get("external_port"), port)
            ):
                print(f"Huawei route already exists: {port} -> {internal_ip}:{port}/TCP")
                return

    if existing_host_mapping:
        domain = existing_host_mapping["domain"]
        url = (
            f"/complexajax.cgi?x={urllib.parse.quote(domain, safe='.')}"
            f"&Add_aa={urllib.parse.quote(domain + '.X_HW_Portlist', safe='.')}"
            "&RequestFile=html/bbsp/portmapping/portmappingnew.asp"
        )
        data = {
            "x.PortMappingEnabled": "1",
            "x.PortMappingDescription": existing_host_mapping.get("description") or mapping_name,
            "x.InternalClient": internal_ip,
            "x.RemoteHost": "",
            "x.X_HW_RemoteHostRange": "",
            "Add_aa.Protocol": "TCP",
            "Add_aa.InternalPort": f"{port}:{port}",
            "Add_aa.ExternalPort": f"{port}:{port}",
            "Add_aa.ExternalSrcPort": "",
            "x.X_HW_Token": token,
        }
        result = client.post(url, data)
        if "error" in result.lower() and '"result": 0' not in result:
            raise CliError(f"Huawei publish failed: {result.strip()}")
        print(f"Huawei route appended: {port} -> {internal_ip}:{port}/TCP")
        return

    interface = extract_wan_interface(page)
    url = (
        f"/addcfgajax.cgi?GROUP_a_x={urllib.parse.quote(interface + '.PortMapping', safe='.')}"
        f"&GROUP_a_ya={urllib.parse.quote(interface + '.PortMapping.X_HW_Portlist', safe='.')}"
        "&RequestFile=html/bbsp/portmapping/portmappingnew.asp"
    )
    data = {
        "GROUP_a_x.PortMappingEnabled": "1",
        "GROUP_a_x.PortMappingDescription": mapping_name,
        "GROUP_a_x.InternalClient": internal_ip,
        "GROUP_a_x.RemoteHost": "",
        "GROUP_a_x.X_HW_RemoteHostRange": "",
        "GROUP_a_ya.Protocol": "TCP",
        "GROUP_a_ya.InternalPort": f"{port}:{port}",
        "GROUP_a_ya.ExternalPort": f"{port}:{port}",
        "GROUP_a_ya.ExternalSrcPort": "",
        "x.X_HW_Token": token,
    }

    result = client.post(url, data)
    if "error" in result.lower() and '"result": 0' not in result:
        raise CliError(f"Huawei publish failed: {result.strip()}")
    print(f"Huawei route created: {port} -> {internal_ip}:{port}/TCP")


def command_huawei_list(args: argparse.Namespace) -> None:
    client = huawei_client(args)
    page = client.page("/html/bbsp/portmapping/portmappingnew.asp")
    mappings = huawei_port_mappings(page)
    if not mappings:
        print("No Huawei port mappings configured.")
        return
    for mapping in mappings:
        print(f"{mapping['description'] or '-'} -> {mapping['client']}")
        for port_entry in mapping.get("ports", []):
            print(
                f"  {port_entry['protocol']} "
                f"{port_entry['external_port']} -> {port_entry['internal_port']}"
            )


def command_huawei_remove(args: argparse.Namespace) -> None:
    client = huawei_client(args)
    page = client.page("/html/bbsp/portmapping/portmappingnew.asp")
    token = extract_token(page)
    port = normalize_port(args.port, "port")

    for mapping in huawei_port_mappings(page):
        if args.internal_ip and mapping.get("client") != args.internal_ip:
            continue
        for port_entry in mapping.get("ports", []):
            if (
                port_entry.get("protocol") in {normalize_protocol(args.proto), "TCP/UDP"}
                and port_range_matches(port_entry.get("external_port"), port)
            ):
                domain = mapping["domain"]
                portlist_domain = port_entry["portlist_domain"]
                url = (
                    f"/complexajax.cgi?x={urllib.parse.quote(domain, safe='.')}"
                    f"&Del_da={urllib.parse.quote(portlist_domain, safe='.')}"
                    "&RequestFile=html/bbsp/portmapping/portmappingnew.asp"
                )
                data = {
                    "x.PortMappingEnabled": mapping.get("enabled", "1"),
                    "x.PortMappingDescription": mapping.get("description", ""),
                    "x.InternalClient": mapping.get("client", ""),
                    "x.RemoteHost": "",
                    "x.X_HW_RemoteHostRange": "",
                    "x.X_HW_Token": token,
                }
                result = client.post(url, data)
                if "error" in result.lower() and '"result": 0' not in result:
                    raise CliError(f"Huawei remove failed: {result.strip()}")
                print(f"Huawei route removed: {port}/{normalize_protocol(args.proto)}")
                if (
                    len(mapping.get("ports", [])) <= 1
                    and str(mapping.get("description", "")).startswith(DEFAULT_DESC_PREFIX)
                ):
                    cleanup_page = client.page("/html/bbsp/portmapping/portmappingnew.asp")
                    cleanup_token = extract_token(cleanup_page)
                    for cleanup_mapping in huawei_port_mappings(cleanup_page):
                        if (
                            cleanup_mapping.get("domain") == domain
                            and cleanup_mapping.get("client") == mapping.get("client")
                            and not cleanup_mapping.get("ports")
                        ):
                            client.post(
                                "/del.cgi?RequestFile=html/bbsp/portmapping/portmappingnew.asp",
                                {
                                    cleanup_mapping["domain"]: "",
                                    "x.X_HW_Token": cleanup_token,
                                },
                            )
                            print("Huawei empty mapping container removed.")
                            break
                return

    raise CliError(f"No Huawei route found for external port {port}/{normalize_protocol(args.proto)}.")


def command_publish_public(args: argparse.Namespace) -> None:
    command_huawei_upnp(args)
    command_huawei_publish(args)
    route = {
        "name": args.name or f"intelikroute-{args.port}",
        "host": "auto",
        "local_port": normalize_port(args.port, "port"),
        "public_port": normalize_port(args.port, "port"),
        "protocol": "TCP",
        "lease": 0,
        "description": f"{DEFAULT_DESC_PREFIX}:{args.name or f'intelikroute-{args.port}'}",
    }
    add_mapping(route)
    print(f"Inner route ensured: {args.port} -> this Mac:{args.port}/TCP")
    internet_ip = public_ip()
    if internet_ip:
        print(f"Public URL: http://{internet_ip}:{args.port}/")


def command_huawei_dns_list(args: argparse.Namespace) -> None:
    client = huawei_client(args)
    page = client.page("/html/bbsp/common/dnshostslist.asp")
    hosts = huawei_dns_hosts(page)
    if not hosts:
        print("No Huawei static DNS hosts configured.")
        return
    for host in hosts:
        print(f"{host['name']} -> {host['ip']} ({host['domain']})")


def command_huawei_dns_add(args: argparse.Namespace) -> None:
    client = huawei_client(args)
    dns_page = client.page("/html/bbsp/common/dnshostslist.asp")
    for host in huawei_dns_hosts(dns_page):
        if host["name"].lower() == args.domain.lower():
            print(f"Huawei DNS host already exists: {host['name']} -> {host['ip']}")
            return

    page = client.page("/html/bbsp/dnsconfiguration/dnshosts.asp")
    token = extract_named_token(page)
    result = client.post(
        "/add.cgi?x=InternetGatewayDevice.X_HW_DNS.HOSTS&RequestFile=html/ipv6/not_find_file.asp",
        {
            "x.IPAddress": args.ip,
            "x.DomainName": args.domain,
            "x.X_HW_Token": token,
        },
    )
    if "error" in result.lower() and "404" not in result.lower():
        raise CliError(f"Huawei DNS add failed: {result.strip()}")
    print(f"Huawei DNS host created: {args.domain} -> {args.ip}")


def command_huawei_dns_remove(args: argparse.Namespace) -> None:
    client = huawei_client(args)
    dns_page = client.page("/html/bbsp/common/dnshostslist.asp")
    target = None
    for host in huawei_dns_hosts(dns_page):
        if host["name"].lower() == args.domain.lower():
            target = host
            break
    if not target:
        raise CliError(f"No Huawei DNS host found for {args.domain}.")

    page = client.page("/html/bbsp/dnsconfiguration/dnshosts.asp")
    token = extract_named_token(page)
    result = client.post(
        "/del.cgi?x=InternetGatewayDevice.X_HW_DNS.HOSTS&RequestFile=html/ipv6/not_find_file.asp",
        {
            target["domain"]: "",
            "x.X_HW_Token": token,
        },
    )
    if "error" in result.lower() and "404" not in result.lower():
        raise CliError(f"Huawei DNS remove failed: {result.strip()}")
    print(f"Huawei DNS host removed: {target['name']} -> {target['ip']}")


def command_proxy(args: argparse.Namespace) -> None:
    ProxyHandler.routes = load_proxy_routes(Path(args.proxy_config))
    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    print(f"Proxy listening on http://{args.host}:{args.port}")
    for host, target in ProxyHandler.routes.items():
        print(f"  {host} -> {target}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nProxy stopped.")


def command_dashboard(args: argparse.Namespace) -> None:
    from intelik_dashboard import serve

    serve(args.host, args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intelikroute",
        description="Manage local UPnP port forwards with miniupnpc.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("INTELIKROUTE_CONFIG", str(DEFAULT_CONFIG)),
        help="Path to routes JSON config. Defaults to routes.json.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Show topology and nested NAT diagnostics.")
    doctor.set_defaults(func=command_doctor)

    list_parser = subparsers.add_parser("list", help="List active UPnP mappings.")
    list_parser.add_argument("--no-warn", action="store_false", dest="warn", help="Skip public-IP comparison.")
    list_parser.set_defaults(func=command_list, warn=True)

    add = subparsers.add_parser("add", help="Add and optionally save a port forward.")
    add.add_argument("name", help="Route name, for example web or api.")
    add.add_argument("--public", required=True, type=int, help="External port on the UPnP router.")
    add.add_argument("--local", required=True, type=int, help="Port on the local host.")
    add.add_argument("--host", default="auto", help="Internal host IP, or auto for this machine.")
    add.add_argument("--proto", default="tcp", help="tcp or udp.")
    add.add_argument("--lease", default=0, type=int, help="Lease seconds. 0 means permanent when supported.")
    add.add_argument("--description", help="UPnP mapping description.")
    add.add_argument("--no-save", action="store_true", help="Apply without updating the JSON config.")
    add.set_defaults(func=command_add)

    remove = subparsers.add_parser("remove", help="Remove a route by name or public port.")
    remove.add_argument("name", nargs="?", help="Saved route name.")
    remove.add_argument("--public", type=int, help="External port to delete when no name is given.")
    remove.add_argument("--proto", default="tcp", help="tcp or udp.")
    remove.set_defaults(func=command_remove)

    apply = subparsers.add_parser("apply", help="Apply all routes from config.")
    apply.set_defaults(func=command_apply)

    verify = subparsers.add_parser("verify", help="Verify saved routes are active in UPnP.")
    verify.add_argument("name", nargs="?", help="Optional route name.")
    verify.set_defaults(func=command_verify)

    publish_test = subparsers.add_parser(
        "publish-test",
        help="Apply the inner route and print the exact Huawei rule needed.",
    )
    publish_test.add_argument("--port", default=8090, type=int, help="TCP port to publish.")
    publish_test.add_argument("--lease", default=0, type=int, help="UPnP lease seconds for the inner route.")
    publish_test.add_argument("--name", default="web-test", help="Route name.")
    publish_test.set_defaults(func=command_publish_test)

    huawei = subparsers.add_parser("huawei-upnp", help="Enable or disable Huawei UPnP.")
    huawei.add_argument("--base-url", default=os.environ.get("HUAWEI_URL", HUAWEI_BASE_URL))
    huawei.add_argument("--username", help="Huawei username. Can also use HUAWEI_USER.")
    huawei.add_argument("--password", help="Huawei password. Can also use HUAWEI_PASS.")
    huawei_group = huawei.add_mutually_exclusive_group(required=True)
    huawei_group.add_argument("--enable", action="store_true", dest="enabled")
    huawei_group.add_argument("--disable", action="store_false", dest="enabled")
    huawei.set_defaults(func=command_huawei_upnp)

    huawei_publish = subparsers.add_parser(
        "huawei-publish",
        help="Create/update the Huawei public port mapping for the inner router.",
    )
    huawei_publish.add_argument("--base-url", default=os.environ.get("HUAWEI_URL", HUAWEI_BASE_URL))
    huawei_publish.add_argument("--username", help="Huawei username. Can also use HUAWEI_USER.")
    huawei_publish.add_argument("--password", help="Huawei password. Can also use HUAWEI_PASS.")
    huawei_publish.add_argument("--port", default=8090, type=int)
    huawei_publish.add_argument("--internal-ip", default="192.168.18.56")
    huawei_publish.add_argument("--name")
    huawei_publish.set_defaults(func=command_huawei_publish)

    huawei_list = subparsers.add_parser("huawei-list", help="List Huawei public port mappings.")
    huawei_list.add_argument("--base-url", default=os.environ.get("HUAWEI_URL", HUAWEI_BASE_URL))
    huawei_list.add_argument("--username", help="Huawei username. Can also use HUAWEI_USER.")
    huawei_list.add_argument("--password", help="Huawei password. Can also use HUAWEI_PASS.")
    huawei_list.set_defaults(func=command_huawei_list)

    huawei_remove = subparsers.add_parser("huawei-remove", help="Remove a Huawei public port mapping row.")
    huawei_remove.add_argument("--base-url", default=os.environ.get("HUAWEI_URL", HUAWEI_BASE_URL))
    huawei_remove.add_argument("--username", help="Huawei username. Can also use HUAWEI_USER.")
    huawei_remove.add_argument("--password", help="Huawei password. Can also use HUAWEI_PASS.")
    huawei_remove.add_argument("--port", required=True, type=int, help="External port to remove.")
    huawei_remove.add_argument("--proto", default="tcp", help="tcp or udp.")
    huawei_remove.add_argument("--internal-ip", help="Optional internal IP filter.")
    huawei_remove.set_defaults(func=command_huawei_remove)

    publish_public = subparsers.add_parser(
        "publish-public",
        help="Disable Huawei UPnP and publish one TCP port through Huawei and TP-Link.",
    )
    publish_public.add_argument("--base-url", default=os.environ.get("HUAWEI_URL", HUAWEI_BASE_URL))
    publish_public.add_argument("--username", help="Huawei username. Can also use HUAWEI_USER.")
    publish_public.add_argument("--password", help="Huawei password. Can also use HUAWEI_PASS.")
    publish_public.add_argument("--port", default=8090, type=int)
    publish_public.add_argument("--internal-ip", default="192.168.18.56")
    publish_public.add_argument("--name")
    publish_public.set_defaults(func=command_publish_public, enabled=False)

    dns_list = subparsers.add_parser("huawei-dns-list", help="List Huawei Static DNS host entries.")
    dns_list.add_argument("--base-url", default=os.environ.get("HUAWEI_URL", HUAWEI_BASE_URL))
    dns_list.add_argument("--username", help="Huawei username. Can also use HUAWEI_USER.")
    dns_list.add_argument("--password", help="Huawei password. Can also use HUAWEI_PASS.")
    dns_list.set_defaults(func=command_huawei_dns_list)

    dns_add = subparsers.add_parser("huawei-dns-add", help="Add a Huawei Static DNS host entry.")
    dns_add.add_argument("domain", help="Local domain name, for example welcome.intelik.lan.")
    dns_add.add_argument("ip", help="IPv4/IPv6 address to return.")
    dns_add.add_argument("--base-url", default=os.environ.get("HUAWEI_URL", HUAWEI_BASE_URL))
    dns_add.add_argument("--username", help="Huawei username. Can also use HUAWEI_USER.")
    dns_add.add_argument("--password", help="Huawei password. Can also use HUAWEI_PASS.")
    dns_add.set_defaults(func=command_huawei_dns_add)

    dns_remove = subparsers.add_parser("huawei-dns-remove", help="Remove a Huawei Static DNS host entry.")
    dns_remove.add_argument("domain", help="Local domain name to remove.")
    dns_remove.add_argument("--base-url", default=os.environ.get("HUAWEI_URL", HUAWEI_BASE_URL))
    dns_remove.add_argument("--username", help="Huawei username. Can also use HUAWEI_USER.")
    dns_remove.add_argument("--password", help="Huawei password. Can also use HUAWEI_PASS.")
    dns_remove.set_defaults(func=command_huawei_dns_remove)

    proxy = subparsers.add_parser("proxy", help="Run the host-header reverse proxy.")
    proxy.add_argument("--proxy-config", default=str(DEFAULT_PROXY_CONFIG), help="Proxy route JSON file.")
    proxy.add_argument("--host", default="0.0.0.0", help="Bind address.")
    proxy.add_argument("--port", default=8080, type=int, help="Listen port.")
    proxy.set_defaults(func=command_proxy)

    dashboard = subparsers.add_parser("dashboard", help="Run the network management dashboard.")
    dashboard.add_argument("--host", default="0.0.0.0", help="Bind address.")
    dashboard.add_argument("--port", default=5050, type=int, help="Dashboard listen port.")
    dashboard.set_defaults(func=command_dashboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
