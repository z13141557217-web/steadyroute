"""A real Mihomo core for the integration tests (tests/test_real_core.py).

The core binary comes from STEADYROUTE_MIHOMO (scripts/build-core.sh builds the pinned version;
CI does that). Laid out like Clash Verge in service mode: the core's home is one folder, the
runtime config Clash Verge generates is in another, and the controller is a Unix socket.
Nothing here needs the network: geodata is a tiny generated GeoSite.dat, and the download
addresses point at a closed local port so a missing database fails at once.
"""

import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time

import geosite_fixture

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "clash_verge"
CORE = os.environ.get("STEADYROUTE_MIHOMO") or ""
# Made-up names in the shapes real subscriptions use (brackets, a pipe, no spaces, a trailing flag),
# including ones that must never be picked.
NODES = [
    "台湾 HiNet 家宽 01 🇨🇳", "台湾 HiNet 家宽 02 🇨🇳", "【2x】优化线路|台湾hinet动态住宅A1", "[A2]台湾hinet住宅hy2",
    "🇹🇼 Taiwan Residential 03", "台灣 住宅 04", "🇹🇼 台湾 01", "台湾 BGP 02", "台湾家宽 到期：2026-12-01",
    "香港 家宽 01", "香港住宅hy2🇭🇰", "【2x】优化线路|香港动态住宅🇭🇰", "HK HKT 家宽 03", "香港 BGP 01",
    "🇯🇵 日本 家宽 01", "Japan residential 02", "东京 IPLC 03", "🇺🇸 美国 家宽 01", "US ISP 02", "USA 洛杉矶 03",
    "🇰🇷 韩国 家宽 01", "新加坡 住宅 01", "🇬🇧 英国 家宽 01", "加拿大 家宽 01", "泰国 家宽 01", "越南 家宽 01",
    "剩余流量：100G", "套餐到期：2026-12-01", "官网 example.com",
]
GEO = ("geo-auto-update: false\n"
       "geox-url:\n"
       "  geosite: http://127.0.0.1:9/geosite.dat\n"
       "  geoip: http://127.0.0.1:9/geoip.dat\n"
       "  mmdb: http://127.0.0.1:9/country.mmdb\n"
       "  asn: http://127.0.0.1:9/asn.mmdb\n")


def available():
    return bool(CORE) and os.access(CORE, os.X_OK)


def runtime_text():
    """The fixture's runtime config with more nodes, no listening port and no downloads."""
    text = (FIXTURE / "clash-verge.yaml").read_text(encoding="utf-8")
    known = set(line.split("name:", 1)[1].strip().strip("'\"") for line in text.splitlines()
                if line.startswith("- name:") and "type" not in line)
    extra = "".join("- name: %s\n  type: socks5\n  server: 203.0.113.%d\n  port: 443\n" % (
        json.dumps(name, ensure_ascii=False), 20 + index) for index, name in enumerate(NODES) if name not in known)
    text = text.replace("mixed-port: 7897\n", "mixed-port: 0\n" + GEO, 1)
    return text.replace("proxy-groups:\n", extra + "proxy-groups:\n", 1)


class Core(object):
    def __init__(self, geosite_lists):
        # A short path: a Unix socket address holds about 100 bytes.
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="sr-", dir="/tmp"))
        self.home = self.root / "core"          # the core's own folder (Clash Verge service mode)
        self.verge = self.root / "verge"        # Clash Verge's folder: profiles and the runtime config
        self.socket = str(self.root / "c.sock")
        self.home.mkdir()
        shutil.copytree(str(FIXTURE), str(self.verge))
        self.original = runtime_text()
        (self.verge / "clash-verge.yaml").write_text(self.original, encoding="utf-8")
        data = geosite_fixture.geosite_dat(geosite_lists)
        for folder in (self.home, self.verge):   # the core validates (-t) with Clash Verge's folder as home
            (folder / "GeoSite.dat").write_bytes(data)
        self.log = open(str(self.root / "core.log"), "wb")
        self.process = subprocess.Popen(
            [CORE, "-d", str(self.home), "-f", str(self.verge / "clash-verge.yaml"), "-ext-ctl-unix", self.socket],
            stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while True:
            try:
                if self("GET", "/version")[0] == 200:
                    break
            except OSError:
                pass
            if self.process.poll() is not None or time.time() > deadline:
                self.stop()
                raise RuntimeError("the core did not start: %s" % self.output()[-600:])
            time.sleep(0.1)

    def output(self):
        self.log.flush()
        return (self.root / "core.log").read_text(encoding="utf-8", errors="replace")

    def __call__(self, method, path, payload=None):
        """(status, body): the same shape settings_service / clash_profile expect of a controller."""
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        head = ["%s %s HTTP/1.0" % (method, path), "Host: localhost"]
        if body:
            head += ["Content-Type: application/json", "Content-Length: %d" % len(body)]
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(30)
        try:
            client.connect(self.socket)
            client.sendall(("\r\n".join(head) + "\r\n\r\n").encode("utf-8") + body)
            data = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            client.close()
        header, _, content = data.partition(b"\r\n\r\n")
        return int(header.split(b" ")[1]), content.decode("utf-8", "replace")

    def get(self, path):
        status, body = self("GET", path)
        assert status == 200, (path, status, body)
        return json.loads(body)

    def proxies(self):
        return self.get("/proxies")["proxies"]

    def rules(self):
        return [(rule["type"], rule["payload"], rule["proxy"]) for rule in self.get("/rules")["rules"]]

    def groups(self):
        return {name: (proxy["type"], proxy["all"]) for name, proxy in self.proxies().items()
                if "all" in proxy and name != "GLOBAL"}

    def load(self, text):
        return self("PUT", "/configs?force=true", {"path": "", "payload": text})

    def reset(self):
        """Back to the fixture: files in Clash Verge's folder and the config the core runs."""
        for path in FIXTURE.rglob("*"):
            if path.is_file():
                shutil.copy2(str(path), str(self.verge / path.relative_to(FIXTURE)))
        (self.verge / "clash-verge.yaml").write_text(self.original, encoding="utf-8")
        status, body = self.load(self.original)
        assert status == 204, body

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.log.close()
        shutil.rmtree(str(self.root), ignore_errors=True)
