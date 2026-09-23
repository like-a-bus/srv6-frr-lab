#!/usr/bin/env python3
"""
Сборщик счётчиков интерфейсов для лабы te.

Раз в MON_INTERVAL (по умолчанию 20 мс) читает /proc/<pid>/net/dev каждого
контейнера лабы через смонтированный хостовый /proc: этот файл показывает
счётчики сетевого namespace процесса с таким PID, поэтому ни агентов в узлах,
ни docker exec не нужно. Раз в секунду отправляет накопленное в VictoriaMetrics
(/api/v1/import) с метками времени в миллисекундах.

Контейнеры ищутся через Docker API по имени <лаба>-<узел> раз в 5 секунд,
так что передеплой и новые PID подхватываются сами. Соседа на другом конце
линка берём из topology-файла containerlab: метка peer.

Для живой схемы в контроллере сборщик сам отдаёт текущую скорость по линкам:
GET :9100/rates. Идти за этим в VictoriaMetrics было бы медленнее: свежие
точки становятся видны в запросах пачками, с задержкой до нескольких секунд.

Только стандартная библиотека.
"""
import http.client
import http.server
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from urllib.parse import urlparse

LAB = os.environ.get("MON_LAB", "clab-srv6-lab2")
PROC = os.environ.get("MON_PROC", "/host/proc")
VM = os.environ.get("MON_VM", f"http://{LAB}-vm:8428")
TOPO = os.environ.get("MON_TOPO", "/opt/mon/topology.clab.yml")
DOCKER_SOCK = os.environ.get("MON_DOCKER", "/var/run/docker.sock")
INTERVAL = float(os.environ.get("MON_INTERVAL", "0.02"))
FLUSH = float(os.environ.get("MON_FLUSH", "1"))
DISCOVER = float(os.environ.get("MON_DISCOVER", "5"))
IFACE_RE = re.compile(os.environ.get("MON_IFACE_RE", r"^eth[1-9][0-9]*$"))
PORT = int(os.environ.get("MON_PORT", "9100"))
RATE_WINDOW = float(os.environ.get("MON_RATE_WINDOW", "1"))  # окно для /rates, секунды
MAX_PENDING = 600_000  # точек в очереди, пока VictoriaMetrics недоступна

# позиции полей в строке /proc/net/dev после "ethN:"
FIELDS = {"rx_bytes": 0, "rx_packets": 1, "tx_bytes": 8, "tx_packets": 9}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


# --- соседи из topology-файла -------------------------------------------------

ENDPOINTS_RE = re.compile(r'endpoints:\s*\[\s*"([\w-]+):([\w-]+)"\s*,\s*"([\w-]+):([\w-]+)"\s*\]')


def read_peers(path):
    """{(узел, интерфейс): узел на другом конце линка}."""
    peers = {}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as e:
        log(f"topology-файл {path} не прочитан ({e}), метки peer не будет")
        return peers
    for a, ai, b, bi in ENDPOINTS_RE.findall(text):
        peers[(a, ai)] = b
        peers[(b, bi)] = a
    return peers


# --- Docker API ---------------------------------------------------------------

class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("docker", timeout=10)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(DOCKER_SOCK)


def docker_get(path):
    conn = DockerConnection()
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return json.loads(resp.read())
    finally:
        conn.close()


def lab_pids():
    """{узел: PID} для запущенных контейнеров лабы."""
    found = {}
    for c in docker_get("/containers/json"):
        for name in c.get("Names", []):
            name = name.lstrip("/")
            if name.startswith(LAB + "-"):
                info = docker_get(f"/containers/{c['Id']}/json")
                pid = (info.get("State") or {}).get("Pid") or 0
                if pid:
                    found[name[len(LAB) + 1:]] = pid
    return found


# --- состояние ----------------------------------------------------------------

class Target:
    def __init__(self, node, pid):
        self.node, self.pid = node, pid
        self.f = open(f"{PROC}/{pid}/net/dev", "rb", buffering=0)

    def read(self):
        self.f.seek(0)
        return self.f.read()

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass


targets = {}            # узел -> Target
targets_lock = threading.Lock()
buf = {}                # (узел, интерфейс, метрика) -> ([ts], [значения])
buf_lock = threading.Lock()
# последние снимки tx_bytes для /rates: (время, {(узел, интерфейс): байты})
history = deque(maxlen=max(2, int(RATE_WINDOW / INTERVAL) + 1))
stop = threading.Event()


class Clock:
    """Метки времени в мс, которые никогда не идут назад.

    Часы WSL подстраиваются под Windows скачками. Шаг назад даёт две точки
    счётчика с перепутанным порядком, VictoriaMetrics видит в этом сброс счётчика,
    и rate() выдаёт всплеск размером со всё значение счётчика — гигабиты на
    линке со 100 Мбит/с. Поэтому время считаем от монотонных часов, привязанных
    к системным при старте, и перепривязываемся, когда системные ушли больше
    чем на секунду в любую сторону.

    Метки при этом никогда не идут назад: если после перепривязки время оказалось
    не позже уже отправленного, точки не пишутся, пока часы не догонят. Получается
    разрыв на графике — это честно и лечится само. Раньше в такой ситуации
    сборщик держал монотонное время и писал точки в будущее (VictoriaMetrics
    принимает их на два дня вперёд): когда реальное время до них доходило, старые
    значения перемешивались со свежими в одной серии, и rate() показывал терабиты.
    """

    def __init__(self):
        self.last = 0
        self.behind_logged = False
        self._anchor()

    def _anchor(self):
        self.wall0, self.mono0 = time.time(), time.monotonic()

    def now_ms(self):
        """Метка в мс или None, если время ещё не дошло до последней отправленной."""
        t = self.wall0 + (time.monotonic() - self.mono0)
        drift = time.time() - t
        if abs(drift) > 1.0:
            log(f"системные часы сместились на {drift:+.1f} с, перепривязываюсь")
            self._anchor()
            t = self.wall0
        ms = int(t * 1000)
        if ms <= self.last:
            if not self.behind_logged:
                log(f"время отстаёт от последней записи на {(self.last - ms) / 1000:.1f} с, "
                    f"паузу держу до тех пор, пока не догонит")
                self.behind_logged = True
            return None
        self.behind_logged = False
        self.last = ms
        return ms


clock = Clock()


def discover_loop():
    last_err = None
    while not stop.is_set():
        try:
            pids = lab_pids()
            err = None
        except (OSError, ValueError) as e:
            pids, err = None, f"Docker API недоступен: {e}"
        if err != last_err:
            log(err or "Docker API снова доступен")
            last_err = err
        if pids is not None:
            with targets_lock:
                for node in list(targets):
                    if pids.get(node) != targets[node].pid:
                        targets.pop(node).close()
                        log(f"- {node}")
                for node, pid in sorted(pids.items()):
                    if node in targets:
                        continue
                    try:
                        targets[node] = Target(node, pid)
                        log(f"+ {node} (pid {pid})")
                    except OSError as e:
                        log(f"! {node}: нет доступа к {PROC}/{pid}/net/dev: {e}")
        stop.wait(DISCOVER)


def sample_once():
    ts = clock.now_ms()
    if ts is None:
        return
    with targets_lock:
        snapshot = list(targets.values())
    rows = []
    for t in snapshot:
        try:
            data = t.read()
        except OSError:
            continue  # контейнер пропал, discover_loop уберёт
        for line in data.split(b"\n")[2:]:
            name, sep, rest = line.partition(b":")
            if not sep:
                continue
            iface = name.strip().decode()
            if not IFACE_RE.match(iface):
                continue
            vals = rest.split()
            for metric, idx in FIELDS.items():
                rows.append(((t.node, iface, metric), int(vals[idx])))
    with buf_lock:
        for key, v in rows:
            entry = buf.get(key)
            if entry is None:
                entry = buf[key] = ([], [])
            entry[0].append(ts)
            entry[1].append(v)
    history.append((ts / 1000, {(n, i): v for (n, i, m), v in rows if m == "tx_bytes"}))


def current_rates(peers):
    """Бит/с с интерфейса в сторону соседа за последние RATE_WINDOW секунд."""
    if len(history) < 2:
        return time.time(), {}
    (t0, old), (t1, new) = history[0], history[-1]
    dt = t1 - t0
    links = {}
    for (node, iface), v in new.items():
        peer = peers.get((node, iface))
        if peer and (node, iface) in old and dt > 0:
            links[f"{node}>{peer}"] = max(0.0, (v - old[(node, iface)]) * 8 / dt)
    return t1, links


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


def serve(peers):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if urlparse(self.path).path != "/rates":
                self.send_error(404)
                return
            t, links = current_rates(peers)
            body = json.dumps({"time": t, "window": RATE_WINDOW, "links": links}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    make_server(PORT, Handler).serve_forever()


def encode(batch, peers):
    lines = []
    for (node, iface, metric), (ts, vals) in batch.items():
        m = {"__name__": f"lab_if_{metric}_total", "node": node, "iface": iface}
        peer = peers.get((node, iface))
        if peer:
            m["peer"] = peer
        lines.append(json.dumps({"metric": m, "values": vals, "timestamps": ts},
                                separators=(",", ":")))
    return "\n".join(lines).encode()


def flush_loop(peers):
    global buf
    pending = deque()   # (тело запроса, число точек)
    npending = 0
    down_since = None
    sent = 0
    last_report = time.monotonic()
    while not stop.wait(FLUSH):
        with buf_lock:
            batch, buf = buf, {}
        n = sum(len(ts) for ts, _ in batch.values())
        if n:
            pending.append((encode(batch, peers), n))
            npending += n
        while npending > MAX_PENDING:
            npending -= pending.popleft()[1]
        while pending:
            body, n = pending[0]
            try:
                req = urllib.request.Request(VM + "/api/v1/import", data=body, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
            except (urllib.error.URLError, OSError) as e:
                if down_since is None:
                    down_since = time.monotonic()
                    log(f"VictoriaMetrics недоступна ({e}), держу точки в очереди")
                break
            pending.popleft()
            npending -= n
            sent += n
            if down_since is not None:
                log(f"VictoriaMetrics доступна, очередь отправлена "
                    f"(перерыв {time.monotonic() - down_since:.0f} с)")
                down_since = None
        if time.monotonic() - last_report >= 60:
            with targets_lock:
                nodes = len(targets)
            log(f"узлов {nodes}, отправлено точек за минуту {sent}, в очереди {npending}")
            sent, last_report = 0, time.monotonic()


def main():
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    peers = read_peers(TOPO)
    log(f"сборщик: лаба {LAB}, опрос {INTERVAL * 1000:.0f} мс, VictoriaMetrics {VM}, "
        f"линков в topology-файле {len(peers) // 2}, /rates на :{PORT}")
    threading.Thread(target=discover_loop, daemon=True).start()
    threading.Thread(target=flush_loop, args=(peers,), daemon=True).start()
    threading.Thread(target=serve, args=(peers,), daemon=True).start()

    next_t = time.monotonic()
    while not stop.is_set():
        sample_once()
        next_t += INTERVAL
        delay = next_t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.monotonic()  # отстали — не догоняем, просто идём дальше
    log("остановлен")


if __name__ == "__main__":
    main()
