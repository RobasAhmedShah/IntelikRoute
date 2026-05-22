#!/usr/bin/env python3
"""Dependency-free management dashboard for IntelikRoute."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from intelikroute import (
    HUAWEI_BASE_URL,
    HuaweiClient,
    add_mapping,
    delete_mapping,
    default_gateway,
    get_upnp_state,
    huawei_dns_hosts,
    huawei_port_mappings,
    load_proxy_routes,
    load_saved_credentials,
    local_ip_hint,
    public_ip,
)


ROOT = Path(__file__).resolve().parent

def _get_dashboard_paths() -> tuple[Path, Path, Path]:
    # Check local first for backwards compatibility
    local_config = Path("proxy-routes.json")
    if local_config.exists():
        return local_config, Path(".intelikroute-proxy.pid"), Path(".intelikroute-proxy.log")
    
    # Use user-specific configuration directory
    home_dir = Path.home() / ".intelikroute"
    home_dir.mkdir(parents=True, exist_ok=True)
    home_config = home_dir / "proxy-routes.json"
    if not home_config.exists():
        try:
            home_config.write_text('{\n  "routes": {}\n}\n')
        except Exception:
            pass
    return home_config, home_dir / "proxy.pid", home_dir / "proxy.log"

PROXY_CONFIG, PROXY_PID, PROXY_LOG = _get_dashboard_paths()
DEFAULT_PROXY_PORT = 8080


def huawei_client_from_env() -> HuaweiClient | None:
    user = os.environ.get("HUAWEI_USER")
    password = os.environ.get("HUAWEI_PASS")
    if not user or not password:
        user, password = load_saved_credentials()
    if not user or not password:
        return None
    client = HuaweiClient(os.environ.get("HUAWEI_URL", HUAWEI_BASE_URL), user, password)
    client.login()
    return client


def run_cli(args: list[str]) -> tuple[int, str]:
    env = os.environ.copy()
    local_script = ROOT / "intelikroute.py"
    if local_script.exists():
        cmd = [sys.executable, str(local_script)]
        cwd = ROOT
    else:
        cmd = [sys.executable, "-m", "intelikroute"]
        cwd = None

    proc = subprocess.run(
        cmd + args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def proxy_status() -> dict[str, Any]:
    if not PROXY_PID.exists():
        return {"running": False, "pid": None, "port": DEFAULT_PROXY_PORT}
    try:
        pid = int(PROXY_PID.read_text().strip())
    except ValueError:
        PROXY_PID.unlink(missing_ok=True)
        return {"running": False, "pid": None, "port": DEFAULT_PROXY_PORT}
    if not pid_is_running(pid):
        PROXY_PID.unlink(missing_ok=True)
        return {"running": False, "pid": None, "port": DEFAULT_PROXY_PORT}
    return {"running": True, "pid": pid, "port": DEFAULT_PROXY_PORT}


def start_proxy_service(port: int = DEFAULT_PROXY_PORT) -> dict[str, Any]:
    status = proxy_status()
    if status["running"]:
        return status | {"message": "Proxy is already running."}
    log = PROXY_LOG.open("ab")

    local_script = ROOT / "intelikroute.py"
    if local_script.exists():
        cmd = [sys.executable, str(local_script)]
        cwd = ROOT
    else:
        cmd = [sys.executable, "-m", "intelikroute"]
        cwd = None

    proc = subprocess.Popen(
        cmd + [
            "proxy",
            "--proxy-config",
            str(PROXY_CONFIG),
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
        ],
        cwd=cwd,
        env=os.environ.copy(),
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    PROXY_PID.write_text(str(proc.pid))
    return {"running": True, "pid": proc.pid, "port": port, "message": "Proxy started."}


def stop_proxy_service() -> dict[str, Any]:
    status = proxy_status()
    if not status["running"]:
        return {"running": False, "pid": None, "port": DEFAULT_PROXY_PORT, "message": "Proxy is already stopped."}
    pid = int(status["pid"])
    try:
        os.kill(pid, 15)
    finally:
        PROXY_PID.unlink(missing_ok=True)
    return {"running": False, "pid": None, "port": DEFAULT_PROXY_PORT, "message": "Proxy stopped."}


def port_open(url: str) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "IntelikRouteDashboard/1.0"})
        with urllib.request.urlopen(req, timeout=4) as response:
            response.read(256)
            return {
                "url": url,
                "ok": True,
                "status": response.status,
                "latency_ms": round((time.perf_counter() - start) * 1000),
            }
    except Exception as exc:
        return {
            "url": url,
            "ok": False,
            "error": str(exc),
            "latency_ms": round((time.perf_counter() - start) * 1000),
        }


def dashboard_status() -> dict[str, Any]:
    upnp_state = get_upnp_state()
    internet_ip = public_ip()

    huawei: dict[str, Any] = {"available": False, "error": None, "mappings": [], "dns": []}
    try:
        client = huawei_client_from_env()
        if client:
            huawei["available"] = True
            huawei["mappings"] = huawei_port_mappings(
                client.page("/html/bbsp/portmapping/portmappingnew.asp")
            )
            huawei["dns"] = huawei_dns_hosts(
                client.page("/html/bbsp/common/dnshostslist.asp")
            )
        else:
            huawei["error"] = "Huawei credentials not found. Run 'intelikroute auth' or set HUAWEI_USER/HUAWEI_PASS before starting the dashboard."
    except Exception as exc:
        huawei["error"] = str(exc)

    proxy_routes = {}
    try:
        proxy_routes = load_proxy_routes(PROXY_CONFIG)
    except Exception:
        proxy_routes = {}

    service_urls = [
        "http://intelik.network/",
        "http://58.65.197.74:8090/",
        "http://58.65.197.74:8181/",
    ]

    return {
        "generated_at": int(time.time()),
        "internet_ip": internet_ip,
        "default_gateway": default_gateway(),
        "local_ip": local_ip_hint(),
        "upnp": {
            "discovered_url": upnp_state.discovered_url,
            "local_ip": upnp_state.local_ip,
            "external_ip": upnp_state.external_ip,
            "mappings": [mapping.__dict__ for mapping in upnp_state.mappings],
        },
        "huawei": huawei,
        "proxy_routes": proxy_routes,
        "proxy_service": proxy_status(),
        "checks": [port_open(url) for url in service_urls],
    }


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if not length:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def write_json(handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: int = 200) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def update_proxy_route(host: str, target: str) -> None:
    data = json.loads(PROXY_CONFIG.read_text())
    routes = data.setdefault("routes", {})
    routes[host.lower()] = target
    PROXY_CONFIG.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def remove_proxy_route(host: str) -> None:
    data = json.loads(PROXY_CONFIG.read_text())
    routes = data.setdefault("routes", {})
    routes.pop(host.lower(), None)
    PROXY_CONFIG.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def publish_domain_service(payload: dict[str, Any]) -> tuple[bool, str]:
    domain = str(payload["domain"]).strip().lower()
    backend_port = int(payload["backend_port"])
    backend_host = str(payload.get("backend_host") or "127.0.0.1").strip()
    internal_ip = str(payload.get("internal_ip") or "192.168.18.56").strip()
    target = f"http://{backend_host}:{backend_port}"
    output: list[str] = []

    update_proxy_route(domain, target)
    output.append(f"Proxy route: {domain} -> {target}")

    code, text = run_cli(["huawei-dns-add", domain, internal_ip])
    output.append(text)
    if code != 0:
        return False, "\n".join(output)

    code, text = run_cli(["huawei-upnp", "--disable"])
    output.append(text)
    if code != 0:
        return False, "\n".join(output)

    code, text = run_cli(["huawei-publish", "--port", "80", "--internal-ip", internal_ip, "--name", "intelikroute-proxy-http"])
    output.append(text)
    if code != 0:
        return False, "\n".join(output)

    try:
        add_mapping(
            {
                "name": "proxy-http",
                "host": "auto",
                "local_port": DEFAULT_PROXY_PORT,
                "public_port": 80,
                "protocol": "TCP",
                "lease": 0,
                "description": "intelikroute:proxy-http",
            }
        )
        output.append(f"TP-Link route: 80 -> this Mac:{DEFAULT_PROXY_PORT}/TCP")
    except Exception as exc:
        output.append(f"TP-Link route failed: {exc}")
        return False, "\n".join(output)

    status = start_proxy_service(DEFAULT_PROXY_PORT)
    output.append(status["message"])
    output.append(f"Open locally: http://{domain}/")
    return True, "\n".join(output)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>IntelikRoute Network Operations</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f3f6f9;
      --panel: #ffffff;
      --panel-2: #f8fafc;
      --ink: #142033;
      --muted: #617086;
      --line: #d9e1ea;
      --blue: #1f6feb;
      --teal: #0f766e;
      --green: #15803d;
      --amber: #b45309;
      --red: #b91c1c;
      --shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--ink); }
    button, input, select { font: inherit; }
    .shell { min-height: 100vh; display: grid; grid-template-columns: 260px 1fr; }
    aside { background: #102033; color: #dbeafe; padding: 22px 18px; }
    .brand { font-size: 18px; font-weight: 800; letter-spacing: 0; margin-bottom: 28px; }
    .nav { display: grid; gap: 8px; }
    .nav a { color: #cbd5e1; text-decoration: none; padding: 10px 12px; border-radius: 6px; }
    .nav a.active, .nav a:hover { background: #1d324d; color: white; }
    main { min-width: 0; }
    header { background: var(--panel); border-bottom: 1px solid var(--line); padding: 18px 28px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    h1 { font-size: 22px; margin: 0; }
    h2 { font-size: 16px; margin: 0 0 14px; }
    .sub { color: var(--muted); font-size: 13px; margin-top: 4px; }
    .content { padding: 24px 28px 40px; display: grid; gap: 20px; }
    .metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
    .metric, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); }
    .metric { padding: 16px; min-height: 96px; }
    .label { color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }
    .value { font-size: 22px; font-weight: 800; margin-top: 10px; word-break: break-word; }
    .grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(360px, .8fr); gap: 20px; align-items: start; }
    .panel { padding: 18px; overflow: hidden; }
    .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .btn { border: 1px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 6px; padding: 8px 12px; cursor: pointer; }
    .btn.primary { background: var(--blue); border-color: var(--blue); color: white; }
    .btn.danger { color: var(--red); border-color: #fecaca; background: #fff5f5; }
    .btn:disabled { opacity: .55; cursor: wait; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid var(--line); padding: 10px 8px; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; background: var(--panel-2); }
    code { background: #eef2f7; padding: 2px 5px; border-radius: 4px; }
    .status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 700; border-radius: 999px; padding: 4px 8px; }
    .ok { background: #dcfce7; color: #166534; }
    .warn { background: #fef3c7; color: #92400e; }
    .bad { background: #fee2e2; color: #991b1b; }
    .topology { min-height: 300px; background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%); }
    .flow { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; align-items: center; margin-top: 18px; }
    .node { border: 1px solid var(--line); border-radius: 8px; padding: 14px; background: white; min-height: 118px; position: relative; }
    .node:not(:last-child)::after { content: ""; position: absolute; right: -13px; top: 50%; width: 14px; border-top: 2px solid #94a3b8; }
    .node-title { font-weight: 800; margin-bottom: 8px; }
    .node-meta { color: var(--muted); font-size: 12px; line-height: 1.45; }
    .forms { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .form { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 14px; display: grid; gap: 10px; }
    .form h3 { margin: 0; font-size: 14px; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    input, select { width: 100%; border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px 10px; background: white; color: var(--ink); min-width: 0; }
    .toast { position: fixed; right: 20px; bottom: 20px; background: #102033; color: white; padding: 12px 14px; border-radius: 8px; max-width: 520px; box-shadow: 0 10px 30px rgba(2, 6, 23, .25); display: none; white-space: pre-wrap; }
    .muted { color: var(--muted); }
    @media (max-width: 1100px) {
      .shell { grid-template-columns: 1fr; }
      aside { display: none; }
      .metrics, .grid, .forms, .flow { grid-template-columns: 1fr; }
      .node::after { display: none; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="brand">IntelikRoute NOC</div>
      <nav class="nav">
        <a class="active" href="#overview">Overview</a>
        <a href="#routes">Routes</a>
        <a href="#dns">DNS</a>
        <a href="#actions">Actions</a>
      </nav>
    </aside>
    <main>
      <header>
        <div>
          <h1>Network Management Dashboard</h1>
          <div class="sub">Huawei EG8247H5, TP-Link UPnP, static DNS, and host-based service routing</div>
        </div>
        <div class="toolbar">
          <button class="btn" onclick="refresh()">Refresh</button>
          <button class="btn primary" onclick="action('/api/upnp/disable', {})">Disable Huawei UPnP</button>
        </div>
      </header>
      <section class="content">
        <div class="metrics" id="metrics"></div>

        <section class="panel topology" id="overview">
          <h2>Topology</h2>
          <div class="flow">
            <div class="node"><div class="node-title">Internet</div><div class="node-meta" id="node-internet"></div></div>
            <div class="node"><div class="node-title">Huawei ONT</div><div class="node-meta">EG8247H5<br>Management: 192.168.18.1<br>Public mappings and Static DNS</div></div>
            <div class="node"><div class="node-title">TP-Link C60</div><div class="node-meta" id="node-c60"></div></div>
            <div class="node"><div class="node-title">Mac Host</div><div class="node-meta" id="node-mac"></div></div>
          </div>
        </section>

        <div class="grid">
          <section class="panel" id="routes">
            <h2>Public And Inner Routes</h2>
            <div id="routes-table"></div>
          </section>
          <section class="panel">
            <h2>Service Health</h2>
            <div id="checks"></div>
          </section>
        </div>

        <div class="grid">
          <section class="panel" id="dns">
            <h2>Static DNS And Proxy Routes</h2>
            <div id="dns-table"></div>
          </section>
          <section class="panel">
            <h2>TP-Link UPnP Table</h2>
            <div id="upnp-table"></div>
          </section>
        </div>

        <section class="panel" id="actions">
          <h2>Controlled Actions</h2>
          <div class="forms">
            <form class="form" onsubmit="publishPort(event)">
              <h3>Publish Public Port</h3>
              <div class="row">
                <input name="port" type="number" min="1" max="65535" placeholder="Port, e.g. 8181" required>
                <input name="internal_ip" value="192.168.18.56" required>
              </div>
              <input name="name" placeholder="Optional route name">
              <button class="btn primary" type="submit">Publish</button>
            </form>
            <form class="form" onsubmit="addDns(event)">
              <h3>Add Local DNS</h3>
              <div class="row">
                <input name="domain" placeholder="domain, e.g. app.intelik.network" required>
                <input name="ip" value="192.168.18.56" required>
              </div>
              <button class="btn primary" type="submit">Add DNS</button>
            </form>
            <form class="form" onsubmit="addProxy(event)">
              <h3>Add Proxy Route</h3>
              <div class="row">
                <input name="host" placeholder="host, e.g. app.intelik.network" required>
                <input name="target" placeholder="http://127.0.0.1:3000" required>
              </div>
              <button class="btn primary" type="submit">Add Proxy Route</button>
            </form>
            <form class="form" onsubmit="removePort(event)">
              <h3>Remove Huawei Port Row</h3>
              <div class="row">
                <input name="port" type="number" min="1" max="65535" placeholder="External port" required>
                <input name="internal_ip" value="192.168.18.56">
              </div>
              <button class="btn danger" type="submit">Remove Port</button>
            </form>
          </div>
        </section>
      </section>
    </main>
  </div>
  <div class="toast" id="toast"></div>
  <script>
    let current = null;
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

    function toast(message) {
      const box = $('toast');
      box.textContent = message;
      box.style.display = 'block';
      setTimeout(() => { box.style.display = 'none'; }, 5200);
    }

    async function api(path, payload) {
      const options = payload === undefined ? {} : {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      };
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || data.output || 'Request failed');
      return data;
    }

    async function action(path, payload) {
      try {
        const data = await api(path, payload);
        toast(data.output || data.message || 'Action completed');
        await refresh();
      } catch (error) {
        toast(error.message);
      }
    }

    function renderMetrics(data) {
      const huaweiStatus = data.huawei.available ? 'Online' : 'Needs credentials';
      const checksOk = data.checks.filter(c => c.ok).length + '/' + data.checks.length;
      $('metrics').innerHTML = [
        ['Public IP', data.internet_ip || '-'],
        ['Huawei', huaweiStatus],
        ['UPnP External', data.upnp.external_ip || '-'],
        ['Service Checks', checksOk],
      ].map(([label, value]) => `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`).join('');
    }

    function renderTopology(data) {
      $('node-internet').innerHTML = `Public IP: <code>${esc(data.internet_ip || '-')}</code><br>HTTP front door: <code>80</code>`;
      $('node-c60').innerHTML = `WAN: <code>${esc(data.upnp.external_ip || '-')}</code><br>UPnP IGD: ${esc(data.upnp.discovered_url || '-')}`;
      $('node-mac').innerHTML = `LAN: <code>${esc(data.local_ip || data.upnp.local_ip || '-')}</code><br>Proxy: <code>8080</code><br>Backends: <code>8090</code>, <code>8181</code>`;
    }

    function renderRoutes(data) {
      const rows = [];
      for (const mapping of data.huawei.mappings || []) {
        for (const port of mapping.ports || []) {
          rows.push(`<tr><td>Huawei</td><td>${esc(port.protocol)}</td><td>${esc(port.external_port)}</td><td>${esc(mapping.client)}:${esc(port.internal_port)}</td><td>${esc(mapping.description)}</td></tr>`);
        }
      }
      for (const mapping of data.upnp.mappings || []) {
        rows.push(`<tr><td>TP-Link</td><td>${esc(mapping.protocol)}</td><td>${esc(mapping.public_port)}</td><td>${esc(mapping.internal_host)}:${esc(mapping.local_port)}</td><td>${esc(mapping.description)}</td></tr>`);
      }
      $('routes-table').innerHTML = table(['Layer', 'Proto', 'Public', 'Internal', 'Description'], rows);
    }

    function renderChecks(data) {
      $('checks').innerHTML = `<table><thead><tr><th>Target</th><th>Status</th><th>Latency</th></tr></thead><tbody>${
        data.checks.map(c => `<tr><td><code>${esc(c.url)}</code></td><td><span class="status ${c.ok ? 'ok' : 'bad'}">${c.ok ? 'OK ' + c.status : 'Fail'}</span></td><td>${esc(c.latency_ms)} ms</td></tr>`).join('')
      }</tbody></table>`;
    }

    function renderDns(data) {
      const dnsRows = (data.huawei.dns || []).map(d => `<tr><td>Huawei DNS</td><td>${esc(d.name)}</td><td><code>${esc(d.ip)}</code></td><td><button class="btn danger" onclick="action('/api/dns/remove',{domain:'${esc(d.name)}'})">Remove</button></td></tr>`);
      const proxyRows = Object.entries(data.proxy_routes || {}).map(([host, target]) => `<tr><td>Proxy</td><td>${esc(host)}</td><td><code>${esc(target)}</code></td><td><button class="btn danger" onclick="action('/api/proxy/remove',{host:'${esc(host)}'})">Remove</button></td></tr>`);
      $('dns-table').innerHTML = table(['Type', 'Name', 'Target', 'Action'], [...dnsRows, ...proxyRows]);
    }

    function renderUpnp(data) {
      const rows = (data.upnp.mappings || []).map(m => `<tr><td>${esc(m.protocol)}</td><td>${esc(m.public_port)}</td><td>${esc(m.internal_host)}:${esc(m.local_port)}</td><td>${esc(m.lease)}</td></tr>`);
      $('upnp-table').innerHTML = table(['Proto', 'Public', 'Internal', 'Lease'], rows);
    }

    function table(headers, rows) {
      if (!rows.length) return '<div class="muted">No records found.</div>';
      return `<table><thead><tr>${headers.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`;
    }

    async function refresh() {
      try {
        current = await api('/api/status');
        renderMetrics(current);
        renderTopology(current);
        renderRoutes(current);
        renderChecks(current);
        renderDns(current);
        renderUpnp(current);
      } catch (error) {
        toast(error.message);
      }
    }

    function formData(form) {
      return Object.fromEntries(new FormData(form).entries());
    }

    async function publishPort(event) {
      event.preventDefault();
      const data = formData(event.target);
      data.port = Number(data.port);
      await action('/api/publish', data);
    }

    async function addDns(event) {
      event.preventDefault();
      await action('/api/dns/add', formData(event.target));
    }

    async function addProxy(event) {
      event.preventDefault();
      await action('/api/proxy/add', formData(event.target));
    }

    async function removePort(event) {
      event.preventDefault();
      const data = formData(event.target);
      data.port = Number(data.port);
      await action('/api/huawei/remove', data);
    }

    refresh();
    setInterval(refresh, 30000);
  </script>
</body>
</html>"""


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>IntelikRoute Control Center</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef3f7;
      --panel: #ffffff;
      --panel-soft: #f7fafc;
      --ink: #122034;
      --muted: #607086;
      --line: #d8e1ea;
      --blue: #2563eb;
      --green: #15803d;
      --amber: #a16207;
      --red: #b91c1c;
      --side: #102033;
      --shadow: 0 10px 24px rgba(15, 23, 42, .08);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--ink); }
    button, input, select { font: inherit; }
    .shell { min-height: 100vh; display: grid; grid-template-columns: 248px minmax(0, 1fr); }
    aside { background: var(--side); color: #dbeafe; padding: 22px 18px; }
    .brand { font-weight: 850; font-size: 18px; margin-bottom: 24px; }
    .nav { display: grid; gap: 8px; }
    .nav a { color: #cbd5e1; text-decoration: none; padding: 10px 12px; border-radius: 6px; }
    .nav a:hover, .nav a.active { background: #1d324d; color: white; }
    main { min-width: 0; }
    header { background: var(--panel); border-bottom: 1px solid var(--line); padding: 18px 28px; display: flex; justify-content: space-between; gap: 16px; align-items: center; }
    h1 { margin: 0; font-size: 22px; }
    h2 { margin: 0 0 14px; font-size: 16px; }
    h3 { margin: 0; font-size: 14px; }
    .sub { color: var(--muted); font-size: 13px; margin-top: 4px; }
    .content { padding: 24px 28px 42px; display: grid; gap: 20px; }
    .panel, .metric, .step { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); }
    .panel { padding: 18px; overflow: hidden; }
    .metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
    .metric { padding: 16px; min-height: 92px; }
    .label { color: var(--muted); font-size: 12px; font-weight: 750; text-transform: uppercase; letter-spacing: .04em; }
    .value { margin-top: 9px; font-size: 21px; font-weight: 850; word-break: break-word; }
    .wizard { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(340px, .9fr); gap: 18px; align-items: start; }
    .steps { display: grid; gap: 10px; counter-reset: steps; }
    .step { display: grid; grid-template-columns: 42px 1fr; gap: 12px; padding: 14px; }
    .step::before { counter-increment: steps; content: counter(steps); width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; background: #dbeafe; color: #1d4ed8; font-weight: 850; }
    .step.done::before { background: #dcfce7; color: #166534; content: "OK"; font-size: 11px; }
    .step.warn::before { background: #fef3c7; color: #92400e; content: "!"; }
    .step p { margin: 5px 0 0; color: var(--muted); font-size: 13px; line-height: 1.45; }
    .form { background: var(--panel-soft); border: 1px solid var(--line); border-radius: 8px; padding: 14px; display: grid; gap: 12px; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    input { width: 100%; border: 1px solid #cbd5e1; border-radius: 6px; padding: 9px 10px; background: white; color: var(--ink); min-width: 0; }
    .hint { color: var(--muted); font-size: 12px; line-height: 1.45; }
    .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .btn { border: 1px solid var(--line); background: white; color: var(--ink); border-radius: 6px; padding: 8px 12px; cursor: pointer; }
    .btn.primary { background: var(--blue); border-color: var(--blue); color: white; }
    .btn.danger { color: var(--red); border-color: #fecaca; background: #fff5f5; }
    .btn:disabled { opacity: .55; cursor: wait; }
    .grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(360px, .8fr); gap: 20px; align-items: start; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid var(--line); padding: 10px 8px; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; background: var(--panel-soft); }
    code { background: #eef2f7; padding: 2px 5px; border-radius: 4px; }
    .status { display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 8px; font-size: 12px; font-weight: 760; }
    .ok { background: #dcfce7; color: #166534; }
    .warn-badge { background: #fef3c7; color: #92400e; }
    .bad { background: #fee2e2; color: #991b1b; }
    .muted { color: var(--muted); }
    .toast { position: fixed; right: 20px; bottom: 20px; background: #102033; color: white; padding: 12px 14px; border-radius: 8px; max-width: 560px; box-shadow: 0 10px 30px rgba(2, 6, 23, .25); display: none; white-space: pre-wrap; }
    @media (max-width: 1050px) {
      .shell { grid-template-columns: 1fr; }
      aside { display: none; }
      .metrics, .wizard, .grid, .row { grid-template-columns: 1fr; }
      header { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="brand">IntelikRoute</div>
      <nav class="nav">
        <a class="active" href="#wizard">Wizard</a>
        <a href="#status">Status</a>
        <a href="#routes">Routes</a>
        <a href="#advanced">Advanced</a>
      </nav>
    </aside>
    <main>
      <header>
        <div>
          <h1>Network Control Center</h1>
          <div class="sub">Guided publishing for Huawei DNS, Huawei forwarding, TP-Link UPnP, and the local proxy</div>
        </div>
        <div class="toolbar">
          <button class="btn" onclick="refresh()">Refresh</button>
          <button class="btn" onclick="action('/api/proxy/start', {})">Start Proxy</button>
          <button class="btn danger" onclick="action('/api/proxy/stop', {})">Stop Proxy</button>
        </div>
      </header>

      <section class="content">
        <div class="metrics" id="metrics"></div>

        <section class="wizard" id="wizard">
          <div class="panel">
            <h2>Publish A Service Without Typing A Port</h2>
            <form class="form" onsubmit="publishDomain(event)">
              <div>
                <label class="label">Domain employees will open</label>
                <input name="domain" placeholder="intelik.network" value="intelik.network" required>
                <div class="hint">For local employees, this is added to Huawei Static DNS and points to the TP-Link WAN IP.</div>
              </div>
              <div class="row">
                <div>
                  <label class="label">Local backend host</label>
                  <input name="backend_host" value="127.0.0.1" required>
                </div>
                <div>
                  <label class="label">Local backend port</label>
                  <input name="backend_port" type="number" min="1" max="65535" placeholder="3000" required>
                </div>
              </div>
              <div>
                <label class="label">TP-Link WAN IP on Huawei</label>
                <input name="internal_ip" value="192.168.18.56" required>
              </div>
              <button class="btn primary" type="submit">Publish Domain</button>
              <div class="hint">This runs the safe order: add DNS, keep Huawei UPnP disabled, publish Huawei port 80, publish TP-Link port 80 to proxy 8080, then start the proxy.</div>
            </form>
          </div>
          <div class="steps" id="steps"></div>
        </section>

        <section class="grid" id="status">
          <div class="panel">
            <h2>Live Service Checks</h2>
            <div id="checks"></div>
          </div>
          <div class="panel">
            <h2>Proxy Routes</h2>
            <div id="proxy-routes"></div>
          </div>
        </section>

        <section class="grid" id="routes">
          <div class="panel">
            <h2>Forwarding Routes</h2>
            <div id="routes-table"></div>
          </div>
          <div class="panel">
            <h2>Local DNS</h2>
            <div id="dns-table"></div>
          </div>
        </section>

        <section class="panel" id="advanced">
          <h2>Advanced Cleanup</h2>
          <div class="row">
            <form class="form" onsubmit="removeHuaweiPort(event)">
              <h3>Remove Huawei Port</h3>
              <input name="port" type="number" min="1" max="65535" placeholder="External port" required>
              <input name="internal_ip" value="192.168.18.56">
              <button class="btn danger" type="submit">Remove Huawei Port</button>
            </form>
            <form class="form" onsubmit="addProxy(event)">
              <h3>Add Proxy Route Only</h3>
              <input name="host" placeholder="app.intelik.network" required>
              <input name="target" placeholder="http://127.0.0.1:3000" required>
              <button class="btn" type="submit">Save Proxy Route</button>
            </form>
          </div>
        </section>
      </section>
    </main>
  </div>
  <div class="toast" id="toast"></div>
  <script>
    let current = null;
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

    function toast(message) {
      const box = $('toast');
      box.textContent = message;
      box.style.display = 'block';
      setTimeout(() => { box.style.display = 'none'; }, 7000);
    }

    async function api(path, payload) {
      const options = payload === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)};
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || data.output || 'Request failed');
      return data;
    }

    async function action(path, payload) {
      try {
        const data = await api(path, payload);
        toast(data.output || data.message || 'Action completed');
        await refresh();
      } catch (error) {
        toast(error.message);
      }
    }

    function formData(form) {
      return Object.fromEntries(new FormData(form).entries());
    }

    async function publishDomain(event) {
      event.preventDefault();
      const data = formData(event.target);
      data.backend_port = Number(data.backend_port);
      await action('/api/wizard/domain', data);
    }

    async function removeHuaweiPort(event) {
      event.preventDefault();
      const data = formData(event.target);
      data.port = Number(data.port);
      await action('/api/huawei/remove', data);
    }

    async function addProxy(event) {
      event.preventDefault();
      await action('/api/proxy/add', formData(event.target));
    }

    function renderMetrics(data) {
      const proxy = data.proxy_service?.running ? `Running :${data.proxy_service.port}` : 'Stopped';
      const huawei = data.huawei.available ? 'Reachable' : 'Needs login';
      const managedHuawei = (data.huawei.mappings || []).reduce((sum, item) => {
        const owned = String(item.description || '').startsWith('intelikroute');
        return sum + (owned ? (item.ports || []).length : 0);
      }, 0);
      const managedUpnp = (data.upnp.mappings || []).filter(m => String(m.description || '').startsWith('intelikroute')).length;
      $('metrics').innerHTML = [
        ['Public IP', data.internet_ip || '-'],
        ['Huawei', huawei],
        ['Proxy', proxy],
        ['Managed Routes', managedHuawei + managedUpnp],
      ].map(([label, value]) => `<div class="metric"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`).join('');
    }

    function renderSteps(data) {
      const hasDns = (data.huawei.dns || []).length > 0;
      const hasProxyRoute = Object.keys(data.proxy_routes || {}).length > 0;
      const proxyRunning = Boolean(data.proxy_service?.running);
      const hasHttpRoute = (data.upnp.mappings || []).some(m => Number(m.public_port) === 80) ||
        (data.huawei.mappings || []).some(m => (m.ports || []).some(p => String(p.external_port).includes('80')));
      const items = [
        [hasDns, 'Choose a local domain', hasDns ? 'Huawei DNS has at least one hostname.' : 'Start with the wizard form and choose the name employees should type.'],
        [hasProxyRoute, 'Point the domain to a local app', hasProxyRoute ? 'The proxy has a host to backend route saved.' : 'Enter the backend host and port, for example 127.0.0.1 and 3000.'],
        [hasHttpRoute, 'Open the HTTP path', hasHttpRoute ? 'Port 80 forwarding exists in the route tables.' : 'The wizard will publish port 80 through Huawei and TP-Link.'],
        [proxyRunning, 'Run the local proxy', proxyRunning ? `Proxy is running with PID ${data.proxy_service.pid}.` : 'Start the proxy after saving routes. The wizard does this automatically.'],
      ];
      $('steps').innerHTML = items.map(([done, title, text]) => `<div class="step ${done ? 'done' : 'warn'}"><div><h3>${esc(title)}</h3><p>${esc(text)}</p></div></div>`).join('');
    }

    function renderChecks(data) {
      if (!data.checks.length) {
        $('checks').innerHTML = '<div class="muted">No checks configured.</div>';
        return;
      }
      $('checks').innerHTML = `<table><thead><tr><th>Target</th><th>Status</th><th>Latency</th></tr></thead><tbody>${
        data.checks.map(c => `<tr><td><code>${esc(c.url)}</code></td><td><span class="status ${c.ok ? 'ok' : 'bad'}">${c.ok ? 'OK ' + c.status : 'Fail'}</span></td><td>${esc(c.latency_ms)} ms</td></tr>`).join('')
      }</tbody></table>`;
    }

    function renderProxyRoutes(data) {
      const rows = Object.entries(data.proxy_routes || {}).map(([host, target]) => `<tr><td>${esc(host)}</td><td><code>${esc(target)}</code></td><td><button class="btn danger" onclick="action('/api/proxy/remove',{host:'${esc(host)}'})">Remove</button></td></tr>`);
      $('proxy-routes').innerHTML = table(['Host', 'Backend', 'Action'], rows);
    }

    function renderRoutes(data) {
      const rows = [];
      for (const mapping of data.huawei.mappings || []) {
        for (const port of mapping.ports || []) {
          rows.push(`<tr><td>Huawei</td><td>${esc(port.protocol)}</td><td>${esc(port.external_port)}</td><td>${esc(mapping.client)}:${esc(port.internal_port)}</td><td>${esc(mapping.description)}</td></tr>`);
        }
      }
      for (const mapping of data.upnp.mappings || []) {
        rows.push(`<tr><td>TP-Link</td><td>${esc(mapping.protocol)}</td><td>${esc(mapping.public_port)}</td><td>${esc(mapping.internal_host)}:${esc(mapping.local_port)}</td><td>${esc(mapping.description)}</td></tr>`);
      }
      $('routes-table').innerHTML = table(['Layer', 'Proto', 'External', 'Internal', 'Description'], rows);
    }

    function renderDns(data) {
      const rows = (data.huawei.dns || []).map(d => `<tr><td>${esc(d.name)}</td><td><code>${esc(d.ip)}</code></td><td><button class="btn danger" onclick="action('/api/dns/remove',{domain:'${esc(d.name)}'})">Remove</button></td></tr>`);
      $('dns-table').innerHTML = table(['Domain', 'IP', 'Action'], rows);
    }

    function table(headers, rows) {
      if (!rows.length) return '<div class="muted">No records found.</div>';
      return `<table><thead><tr>${headers.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`;
    }

    async function refresh() {
      try {
        current = await api('/api/status');
        renderMetrics(current);
        renderSteps(current);
        renderChecks(current);
        renderProxyRoutes(current);
        renderRoutes(current);
        renderDns(current);
      } catch (error) {
        toast(error.message);
      }
    }

    refresh();
    setInterval(refresh, 30000);
  </script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/dashboard"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/status":
            try:
                write_json(self, dashboard_status())
            except Exception as exc:
                write_json(self, {"ok": False, "error": str(exc)}, 500)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        try:
            payload = read_json(self)
            if self.path == "/api/publish":
                args = ["publish-public", "--port", str(payload["port"]), "--internal-ip", payload.get("internal_ip") or "192.168.18.56"]
                if payload.get("name"):
                    args.extend(["--name", payload["name"]])
                code, output = run_cli(args)
                write_json(self, {"ok": code == 0, "output": output}, 200 if code == 0 else 500)
                return
            if self.path == "/api/wizard/domain":
                ok, output = publish_domain_service(payload)
                write_json(self, {"ok": ok, "output": output}, 200 if ok else 500)
                return
            if self.path == "/api/proxy/start":
                status = start_proxy_service(int(payload.get("port") or DEFAULT_PROXY_PORT))
                write_json(self, {"ok": True, **status})
                return
            if self.path == "/api/proxy/stop":
                status = stop_proxy_service()
                write_json(self, {"ok": True, **status})
                return
            if self.path == "/api/huawei/remove":
                args = ["huawei-remove", "--port", str(payload["port"])]
                if payload.get("internal_ip"):
                    args.extend(["--internal-ip", payload["internal_ip"]])
                code, output = run_cli(args)
                write_json(self, {"ok": code == 0, "output": output}, 200 if code == 0 else 500)
                return
            if self.path == "/api/upnp/disable":
                code, output = run_cli(["huawei-upnp", "--disable"])
                write_json(self, {"ok": code == 0, "output": output}, 200 if code == 0 else 500)
                return
            if self.path == "/api/dns/add":
                code, output = run_cli(["huawei-dns-add", payload["domain"], payload["ip"]])
                write_json(self, {"ok": code == 0, "output": output}, 200 if code == 0 else 500)
                return
            if self.path == "/api/dns/remove":
                code, output = run_cli(["huawei-dns-remove", payload["domain"]])
                write_json(self, {"ok": code == 0, "output": output}, 200 if code == 0 else 500)
                return
            if self.path == "/api/proxy/add":
                update_proxy_route(payload["host"], payload["target"])
                write_json(self, {"ok": True, "message": "Proxy route updated. Restart the proxy process to load it."})
                return
            if self.path == "/api/proxy/remove":
                remove_proxy_route(payload["host"])
                write_json(self, {"ok": True, "message": "Proxy route removed. Restart the proxy process to unload it."})
                return
        except Exception as exc:
            write_json(self, {"ok": False, "error": str(exc)}, 500)
            return
        self.send_error(404)


def serve(host: str = "0.0.0.0", port: int = 5050) -> None:
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    serve()
