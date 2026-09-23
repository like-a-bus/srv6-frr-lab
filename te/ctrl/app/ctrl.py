#!/usr/bin/env python3
"""
Контроллер явных путей SRv6 для лабы te.

Топология берётся из BGP-LS на ctrl, DT4-сид выходного PE — из VPNv4 на ctrl,
маршрут на входные PE ставится через vtysh внутри их контейнеров (Docker API
через проброшенный /var/run/docker.sock). Только стандартная библиотека.

Топология читается один раз, при первом запросе, и дальше не обновляется:
перезапуск процесса и есть обновление. Пустую топологию не запоминаем, чтобы
не застрять на ней, если страницу открыли до конца setup.sh.

Загрузка линков для схемы берётся у сборщика (te/monitoring/collector.py):
скорость передачи с интерфейса узла в сторону соседа за последнюю секунду.
"""
import http.client
import http.server
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LAB = os.environ.get("CTRL_LAB", "clab-srv6-lab2")
VRF = os.environ.get("CTRL_VRF", "RED")
IFACE = os.environ.get("CTRL_IFACE", "eth3")  # интерфейс PE в VRF, см. NOTES.md
PE_RE = re.compile(os.environ.get("CTRL_PE_RE", r"^pe\d+$"))
PORT = int(os.environ.get("CTRL_PORT", "8080"))
COLLECTOR_URL = os.environ.get("CTRL_COLLECTOR", f"http://{LAB}-collector:9100")
# Плечи ядра: верхнее — p1 и p3, нижнее — p2 и p4. Приоритет задаётся метрикой
# IS-IS на интерфейсах в сторону нелюбимого плеча: путь через него становится
# дороже, но остаётся, поэтому при отказе IGP сам уводит трафик туда.
LEGS = {"upper": ("p1", "p3"), "lower": ("p2", "p4")}
LEG_METRIC = int(os.environ.get("CTRL_LEG_METRIC", "100"))
DOCKER_SOCK = "/var/run/docker.sock"
HERE = Path(__file__).resolve().parent


class ApiError(Exception):
    """Ошибка, которую показываем пользователю как есть."""


# --- чтение с ctrl ----------------------------------------------------------

def vtysh_json(cmd):
    out = subprocess.run(["vtysh", "-c", cmd], capture_output=True,
                         text=True, timeout=30).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


def best_path(paths):
    for p in paths:
        if p.get("bestpath"):
            return p
    return paths[0] if paths else None


def read_topology():
    routes = vtysh_json("show bgp link-state link-state json").get("routes", {})
    names, links, locators = {}, [], []
    for paths in routes.values():
        p = best_path(paths)
        if not p:
            continue
        nlri = p.get("nlri", {})
        attrs = p.get("linkStateAttrs") or {}
        kind = nlri.get("nlriType")
        local = nlri.get("localNodeDescriptors", {}).get("igpRouterId")
        if kind == "node":
            names[local] = attrs.get("nodeName") or local
        elif kind == "link":
            d = nlri.get("linkDescriptors", {})
            sids = [s["sid"] for s in attrs.get("srv6EndxSids", []) if "sid" in s]
            links.append({
                "src": local,
                "dst": nlri.get("remoteNodeDescriptors", {}).get("igpRouterId"),
                "local": d.get("ipv6InterfaceAddress"),
                "remote": d.get("ipv6NeighborAddress"),
                "metric": attrs.get("igpMetric"),
                "sid": sids[0] if sids else None,
            })
        elif kind == "ipv6Prefix" and "srv6Locator" in attrs:
            pfx = nlri.get("prefixDescriptors", {}).get("ipReachabilityInformation")
            if pfx:
                locators.append([pfx, local])

    for l in links:
        l["id"] = f'{l["src"]}>{l["local"]}'
        l["srcName"] = names.get(l["src"], l["src"])
        l["dstName"] = names.get(l["dst"], l["dst"])
    links.sort(key=lambda l: (l["srcName"], l["dstName"]))
    nodes = [{"id": i, "name": n, "pe": bool(PE_RE.match(n))}
             for i, n in sorted(names.items(), key=lambda kv: kv[1])]
    locators = [[pfx, names.get(i, i)] for pfx, i in locators]
    return {"nodes": nodes, "links": links, "locators": locators}


_topo = None
_topo_lock = threading.Lock()


def topology():
    global _topo
    with _topo_lock:
        if _topo is not None:
            return _topo
        t = read_topology()
        if t["links"]:
            _topo = t
        return t


def pe_names():
    return [n["name"] for n in topology()["nodes"] if n["pe"]]


def parse_prefix(text):
    try:
        return str(ipaddress.IPv4Network((text or "").strip(), strict=True))
    except ValueError:
        raise ApiError("Нужна IPv4-сеть без хостовых битов, например 10.2.0.0/24")


def egress_options(prefix):
    """Какие PE анонсируют префикс в VPNv4 и с каким DT4-сидом.

    PE определяем не по hostname (там рефлектор) и не по RD (это соглашение
    из конфигов), а по локатору из BGP-LS, в который попадает сид.
    """
    data = vtysh_json(f"show bgp ipv4 vpn {prefix} json")
    locs = [(ipaddress.ip_network(p), name) for p, name in topology()["locators"]]
    found = {}
    for rd, entry in data.items():
        if not isinstance(entry, dict):
            continue
        for p in entry.get("paths", []):
            sid = p.get("remoteTransposedSid") or p.get("remoteSid")
            if not sid:
                continue
            addr = ipaddress.ip_address(sid)
            pe = next((name for net, name in locs if addr in net), None)
            if pe and pe not in found:
                found[pe] = {"pe": pe, "sid": str(addr), "rd": rd}
    return sorted(found.values(), key=lambda o: o["pe"])


def link_load():
    """{"узел>сосед": бит/с} — ключ совпадает с srcName>dstName линка на схеме."""
    try:
        with urllib.request.urlopen(f"{COLLECTOR_URL}/rates", timeout=3) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ApiError(f"сборщик не отвечает ({COLLECTOR_URL}): {e}")
    return {"time": data.get("time") or time.time(), "links": data.get("links") or {}}


# --- плечи ядра ------------------------------------------------------------

_ifaces = {}


def node_ifaces(node):
    """{IPv6-адрес линка: интерфейс} — чтобы связать линк из BGP-LS с ethN."""
    if node not in _ifaces:
        _, out = pe_exec(node, ["ip", "-o", "-6", "addr", "show", "scope", "global"])
        table = {}
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 4 and f[2] == "inet6":
                try:
                    table[str(ipaddress.IPv6Interface(f[3]).ip)] = f[1]
                except ValueError:
                    pass
        _ifaces[node] = table
    return _ifaces[node]


def leg_ifaces(nodes):
    """{узел: [интерфейсы в сторону nodes]} — сами эти узлы между собой не трогаем."""
    plan = {}
    for l in topology()["links"]:
        if l["dstName"] in nodes and l["srcName"] not in nodes:
            iface = node_ifaces(l["srcName"]).get(l["local"])
            if iface:
                plan.setdefault(l["srcName"], []).append(iface)
    return plan


def leg_metrics():
    """{сосед: метрика} со стороны pe1 — по ним и определяем текущее плечо."""
    # именно detail: без него команда печатает краткую таблицу без метрик
    _, out = pe_exec("pe1", ["vtysh", "-c", "show isis interface detail"])
    by_iface, iface = {}, None
    for line in out.splitlines():
        m = re.match(r"\s*Interface:\s+(\S+?),", line)
        if m:
            iface = m.group(1)
            continue
        m = re.match(r"\s*Metric:\s+(\d+)", line)
        if m and iface:
            by_iface[iface] = int(m.group(1))
            iface = None
    table = node_ifaces("pe1")
    metrics = {}
    for l in topology()["links"]:
        if l["srcName"] == "pe1":
            iface = table.get(l["local"])
            if iface in by_iface:
                metrics[l["dstName"]] = by_iface[iface]
    return metrics


def leg_status():
    metrics = leg_metrics()
    raised = {leg for leg, nodes in LEGS.items()
              if any(metrics.get(n, 0) > 10 for n in nodes)}
    if raised == {"lower"}:
        leg = "upper"
    elif raised == {"upper"}:
        leg = "lower"
    else:
        leg = "none"
    return {"leg": leg, "metric": LEG_METRIC, "neighbors": metrics}


def set_leg(body):
    leg = (body or {}).get("leg")
    if leg not in ("upper", "lower", "none"):
        raise ApiError("Плечо: upper, lower или none")
    results = []
    for name, nodes in LEGS.items():
        command = (f"isis metric {LEG_METRIC}"
                   if leg != "none" and name != leg else "no isis metric")
        for node, ifaces in leg_ifaces(nodes).items():
            commands = []
            for iface in ifaces:
                commands += [f"interface {iface}", command]
            code, out = run_config(node, commands)
            results.append({"node": node, "ifaces": ifaces, "command": command,
                            "ok": code == 0, "out": out.strip()})
    status = leg_status()
    if leg != status["leg"]:
        raise ApiError(f"Метрики применились не полностью, сейчас: {status['leg']}")
    return {"applied": leg, "results": results, **status}


# --- выполнение на PE через Docker API --------------------------------------

class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("docker", timeout=60)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(DOCKER_SOCK)


def docker(method, path, body=None):
    conn = DockerConnection()
    try:
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, resp.read()
    except OSError as e:
        raise ApiError(f"Нет доступа к Docker через {DOCKER_SOCK}: {e}")
    finally:
        conn.close()


def demux(raw):
    """Без TTY Docker склеивает stdout и stderr в кадры с 8-байтным заголовком."""
    if not raw or raw[0] not in (0, 1, 2):
        return raw.decode(errors="replace")
    parts, i = [], 0
    while i + 8 <= len(raw):
        size = struct.unpack(">I", raw[i + 4:i + 8])[0]
        parts.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(parts).decode(errors="replace")


def pe_exec(pe, cmd):
    name = f"{LAB}-{pe}"
    status, data = docker("POST", f"/containers/{name}/exec", {
        "AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": cmd})
    if status != 201:
        raise ApiError(f"{name}: Docker не создал exec: {data.decode(errors='replace').strip()}")
    exec_id = json.loads(data)["Id"]
    status, raw = docker("POST", f"/exec/{exec_id}/start", {"Detach": False, "Tty": False})
    if status != 200:
        raise ApiError(f"{name}: Docker не запустил exec: {raw.decode(errors='replace').strip()}")
    code = None
    for _ in range(30):
        _, info = docker("GET", f"/exec/{exec_id}/json")
        info = json.loads(info)
        if not info.get("Running"):
            code = info.get("ExitCode")
            break
        time.sleep(0.1)
    return code, demux(raw)


ROUTE_RE = re.compile(r"^\s*ip route (\S+) (\S+) segments (\S+)(?: vrf (\S+))?\s*$")
SEGS_RE = re.compile(r"encap seg6 mode \S+ segs \d+ \[ ([^\]]*) \]")


def norm_sids(sids):
    return [str(ipaddress.IPv6Address(s)) for s in sids]


def pe_te_routes(pe):
    """TE-маршруты в нашей VRF из running-config PE."""
    _, out = pe_exec(pe, ["vtysh", "-c", "show running-config"])
    vrf, routes = None, []
    for line in out.splitlines():
        if line.startswith("vrf "):
            vrf = line.split()[1]
            continue
        if line.startswith("exit-vrf"):
            vrf = None
            continue
        m = ROUTE_RE.match(line)
        if m and (m.group(4) or vrf) == VRF:
            routes.append({"prefix": m.group(1), "iface": m.group(2),
                           "segments": m.group(3).split("/")})
    return routes


def kernel_route(pe, prefix):
    _, out = pe_exec(pe, ["ip", "route", "show", "vrf", VRF, prefix])
    m = SEGS_RE.search(out)
    return (norm_sids(m.group(1).split()) if m else None), out.strip()


def run_config(pe, commands):
    args = ["vtysh", "-c", "conf t"]
    for c in commands:
        args += ["-c", c]
    args += ["-c", "end"]
    return pe_exec(pe, args)


def wait_kernel(pe, prefix, check, seconds=6):
    segs, line = kernel_route(pe, prefix)
    deadline = time.time() + seconds
    while not check(segs) and time.time() < deadline:
        time.sleep(0.5)
        segs, line = kernel_route(pe, prefix)
    return check(segs), line


def apply_route(pe, prefix, segs):
    existing = [r for r in pe_te_routes(pe) if r["prefix"] == prefix]
    want = "/".join(segs)
    commands = [f"no ip route {prefix} {r['iface']} segments {'/'.join(r['segments'])} vrf {VRF}"
                for r in existing if (r["iface"], "/".join(r["segments"])) != (IFACE, want)]
    if not any((r["iface"], "/".join(r["segments"])) == (IFACE, want) for r in existing):
        commands.append(f"ip route {prefix} {IFACE} segments {want} vrf {VRF}")
    _, out = run_config(pe, commands) if commands else (0, "")
    ok, line = wait_kernel(pe, prefix, lambda s: s == segs)
    if ok:
        msg = "Маршрут установлен" if commands else "Этот путь уже стоял, ничего не менял"
    else:
        msg = "В ядре PE нет маршрута с этим сеглистом"
    return {"pe": pe, "ok": ok, "message": msg, "commands": commands,
            "vtysh": out.strip(), "kernel": line}


def remove_route(pe, prefix):
    existing = [r for r in pe_te_routes(pe) if r["prefix"] == prefix]
    if not existing:
        return {"pe": pe, "ok": True, "message": "TE-маршрута на этот префикс не было",
                "commands": [], "vtysh": "", "kernel": kernel_route(pe, prefix)[1]}
    commands = [f"no ip route {prefix} {r['iface']} segments {'/'.join(r['segments'])} vrf {VRF}"
                for r in existing]
    _, out = run_config(pe, commands)
    ok, line = wait_kernel(pe, prefix, lambda s: s is None)
    msg = "Маршрут снят, трафик идёт по BGP" if ok else "В ядре PE всё ещё SRv6-инкапсуляция"
    return {"pe": pe, "ok": ok, "message": msg, "commands": commands,
            "vtysh": out.strip(), "kernel": line}


# --- API --------------------------------------------------------------------

def check_pe(pe):
    if pe not in pe_names():
        raise ApiError(f"{pe!r} — не PE из топологии")
    return pe


def commit(body):
    prefix = parse_prefix(body.get("prefix"))
    path = body.get("path") or []
    ingress = body.get("ingress") or []
    egress = body.get("egress")
    if not path:
        raise ApiError("Выберите на схеме хотя бы один линк")
    if not ingress:
        raise ApiError("Отметьте хотя бы один входной PE")

    by_id = {l["id"]: l for l in topology()["links"]}
    sids = []
    for link_id in path:
        l = by_id.get(link_id)
        if not l:
            raise ApiError("Линка из пути нет в топологии. Перезапустите контроллер и соберите путь заново")
        if not l["sid"]:
            raise ApiError(f"У линка {l['srcName']} → {l['dstName']} нет End.X-сида в BGP-LS")
        sids.append(l["sid"])

    options = {o["pe"]: o for o in egress_options(prefix)}
    if egress not in options:
        raise ApiError(f"{egress} не анонсирует {prefix} в VPNv4")
    segs = norm_sids(sids + [options[egress]["sid"]])

    for pe in ingress:  # проверяем все до того, как что-то менять
        check_pe(pe)
    results = []
    for pe in ingress:
        if pe == egress:
            results.append({"pe": pe, "ok": False, "message": "Это выходной PE, путь на нём не ставится",
                            "commands": [], "vtysh": "", "kernel": ""})
            continue
        results.append(apply_route(pe, prefix, segs))
    return {"prefix": prefix, "segments": segs, "results": results}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "srv6-ctrl"

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/load"):
            return  # схема опрашивает раз в секунду, лог бы только рос
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def send_body(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, obj):
        self.send_body(code, json.dumps(obj, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

    def handle_api(self, fn):
        try:
            self.send_json(200, fn())
        except ApiError as e:
            self.send_json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 — показать причину в интерфейсе
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            self.send_body(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/topology":
            self.handle_api(lambda: {k: v for k, v in topology().items() if k != "locators"})
        elif url.path == "/api/egress":
            def egress():
                prefix = parse_prefix(query.get("prefix", [""])[0])
                return {"prefix": prefix, "options": egress_options(prefix)}
            self.handle_api(egress)
        elif url.path == "/api/load":
            self.handle_api(link_load)
        elif url.path == "/api/leg":
            self.handle_api(leg_status)
        elif url.path == "/api/routes":
            self.handle_api(lambda: {"routes": [{"pe": pe, "routes": pe_te_routes(pe)}
                                                for pe in pe_names()]})
        else:
            self.send_json(404, {"error": "Нет такого адреса"})

    def do_POST(self):
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "Тело запроса — не JSON"})
            return
        if url.path == "/api/commit":
            self.handle_api(lambda: commit(body))
        elif url.path == "/api/leg":
            self.handle_api(lambda: set_leg(body))
        elif url.path == "/api/remove":
            self.handle_api(lambda: {"results": [remove_route(check_pe(body.get("pe")),
                                                              parse_prefix(body.get("prefix")))]})
        else:
            self.send_json(404, {"error": "Нет такого адреса"})


class DualStackServer(http.server.ThreadingHTTPServer):
    """Слушает и IPv4, и IPv6. Docker DNS отдаёт имя контейнера с AAAA, curl localhost
    идёт на ::1, и сокет только на IPv4 даёт им Connection refused / reset."""
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(port, handler):
    """Двухстековый сервер, а где IPv6 выключен — обычный IPv4."""
    try:
        return DualStackServer(("::", port), handler)
    except OSError:
        server = http.server.ThreadingHTTPServer(("", port), handler)
        server.daemon_threads = True
        return server


def main():
    server = make_server(PORT, Handler)
    print(f"srv6-ctrl слушает :{PORT}, лаба {LAB}, VRF {VRF}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
