#!/usr/bin/env python3
"""ILBS-Lab in EVE-NG aufbauen: Topologie, Bootstrap, Kunden-Config.

Schritte (jeweils idempotent, Bestehendes wird übersprungen):
  1. Lab ilbs.unl per EVE-REST-API anlegen (Nodes, Netze, Links)
  2. Nodes starten
  3. Bootstrap über die Telnet-Konsole (Expect auf der EVE-VM):
     Erst-Login + Passwort, Mgmt-IP auf ether1, API an
  4. Config hochladen und importieren (configs/<dev>.rsc bzw. it-fw.rsc)

Topologie:
  rtr-ilbs-01 ══ sw-ilbs-01 ══ LACP-PEER-LINK ══ sw-ilbs-02 ══ rtr-ilbs-02
                     └── it-fw (IT-Firewall-Simulation, VLAN 1240)
  ether1 aller Nodes -> Cloud0 (pnet0, 10.0.2.0/24)

Voraussetzung: gen_lab_config.py gelaufen, EVE-VM unter 10.0.2.15 erreichbar,
CHR-Image unter /opt/unetlab/addons/qemu/mikrotik-<version>/.

Aufruf:
  ./create_ilbs_lab.py              # alles
  ./create_ilbs_lab.py --recreate   # Lab vorher löschen (Nodes verlieren ihre Config!)
"""

import argparse
import http.cookiejar
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent
CONFIG_DIR = LAB_DIR / "configs"

EVE_HOST = "10.0.2.15"
EVE_URL = f"http://{EVE_HOST}"
EVE_USER, EVE_PASSWORD = "admin", "eve"
EVE_ROOT_PASSWORD = "eve"
LAB_NAME = "ilbs"
LAB_PATH = f"{LAB_NAME}.unl"

CHR_IMAGE = "mikrotik-7.23.5"
ROS_USER, ROS_PASSWORD = "admin", "Mikrotik1!"

# Gerät -> Mgmt-IP (wie ansible-tam/ansible/inventory/hosts_lab.yml), Position im EVE-Canvas
NODES = {
    "rtr-ilbs-01": {"ip": "10.0.2.101", "pos": (150, 150), "icon": "Router-2D-Gen-White-S.svg"},
    "rtr-ilbs-02": {"ip": "10.0.2.102", "pos": (750, 150), "icon": "Router-2D-Gen-White-S.svg"},
    "sw-ilbs-01": {"ip": "10.0.2.103", "pos": (300, 400), "icon": "Switch-2D-L3-Generic-S.svg"},
    "sw-ilbs-02": {"ip": "10.0.2.104", "pos": (600, 400), "icon": "Switch-2D-L3-Generic-S.svg"},
    "it-fw": {"ip": "10.0.2.105", "pos": (300, 650), "icon": "Firewall-2D-Generic-S.svg"},
}

# it-fw ist handgeschrieben (it-fw.rsc), daher Portmap hier statt aus .ports.json.
IT_FW_PORTS = {"oob": "ether1", "sw-ilbs-01": "ether2"}

# Links über Kunden-Portnamen; aufgelöst über configs/<dev>.ports.json.
LINKS = [
    (("rtr-ilbs-01", "sw-ilbs-01-sfp-01"), ("sw-ilbs-01", "rtr-ilbs-01-sfp1")),
    (("rtr-ilbs-01", "sw-ilbs-01-sfp-02"), ("sw-ilbs-01", "rtr-ilbs-01-sfp2")),
    (("rtr-ilbs-02", "sw-ilbs-02-sfp-01"), ("sw-ilbs-02", "rtr-ilbs-02-sfp1")),
    (("rtr-ilbs-02", "sw-ilbs-02-sfp-02"), ("sw-ilbs-02", "rtr-ilbs-02-sfp2")),
    (("sw-ilbs-01", "qsfpplus1-1"), ("sw-ilbs-02", "qsfpplus1-1")),
    (("sw-ilbs-01", "qsfpplus2-1"), ("sw-ilbs-02", "qsfpplus2-1")),
    (("sw-ilbs-01", "sfp-sfpplus5"), ("it-fw", "sw-ilbs-01")),
]

SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10"]


# ── EVE-REST-API ─────────────────────────────────────────────────────

class Eve:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call("POST", "/api/auth/login",
                  {"username": EVE_USER, "password": EVE_PASSWORD, "html5": "-1"})

    def call(self, method, path, data=None, ok=(200, 201)):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(EVE_URL + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=60) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in ok:
                return json.loads(e.read() or b"{}")
            raise RuntimeError(f"EVE {method} {path}: HTTP {e.code} {e.read()[:300]!r}") from None

    def lab(self, sub=""):
        return f"/api/labs/{LAB_PATH}{sub}"


def port_maps() -> dict[str, dict[str, str]]:
    maps = {"it-fw": IT_FW_PORTS}
    for dev in NODES:
        if dev == "it-fw":
            continue
        f = CONFIG_DIR / f"{dev}.ports.json"
        if not f.exists():
            sys.exit(f"{f} fehlt — erst ./gen_lab_config.py laufen lassen.")
        maps[dev] = json.loads(f.read_text())
    return maps


def eve_iface_id(chr_port: str) -> int:
    # EVE nummeriert die NICs ab 0: e0 = ether1.
    return int(chr_port.removeprefix("ether")) - 1


def create_lab(eve: Eve, maps, recreate: bool) -> dict[str, int]:
    labs = eve.call("GET", "/api/folders/")["data"]["labs"]
    exists = any(lab["file"] == LAB_PATH for lab in labs)
    if exists and recreate:
        print(f"Lösche bestehendes Lab {LAB_PATH}")
        eve.call("DELETE", eve.lab())
        exists = False
    if exists:
        nodes = eve.call("GET", eve.lab("/nodes"))["data"]
        print(f"Lab {LAB_PATH} existiert — Topologie unverändert")
        return {n["name"]: int(nid) for nid, n in nodes.items()}

    print(f"Lege Lab {LAB_PATH} an")
    eve.call("POST", "/api/labs", {"path": "/", "name": LAB_NAME, "version": "1",
                                   "author": "", "description": "ILBS-Lab für ansible-tam"})

    node_ids = {}
    for dev, cfg in NODES.items():
        ethernets = max(int(p.removeprefix("ether")) for p in maps[dev].values())
        res = eve.call("POST", eve.lab("/nodes"), {
            "type": "qemu", "template": "mikrotik", "image": CHR_IMAGE, "name": dev,
            "icon": cfg["icon"], "ethernet": str(ethernets), "ram": "1024", "cpu": "1",
            "console": "telnet", "left": str(cfg["pos"][0]), "top": str(cfg["pos"][1]),
        })
        node_ids[dev] = int(res["data"]["id"])
        print(f"  Node {dev}: id {node_ids[dev]}, {ethernets} NICs")

    cloud = eve.call("POST", eve.lab("/networks"), {
        "type": "pnet0", "name": "Mgmt 10.0.2.0/24", "left": "450", "top": "20", "visibility": 1,
    })["data"]["id"]
    for dev in NODES:
        eve.call("PUT", eve.lab(f"/nodes/{node_ids[dev]}/interfaces"), {"0": cloud})

    for (a_dev, a_port), (b_dev, b_port) in LINKS:
        # Erst sichtbar anlegen: EVE verwirft unsichtbare Netze ohne Verbindung
        # sofort. Nach dem Verbinden als Punkt-zu-Punkt-Link ausblenden.
        net = eve.call("POST", eve.lab("/networks"), {
            "type": "bridge", "name": f"{a_dev}:{a_port}--{b_dev}:{b_port}",
            "left": "0", "top": "0", "visibility": 1,
        })["data"]["id"]
        for dev, port in ((a_dev, a_port), (b_dev, b_port)):
            iface = eve_iface_id(maps[dev][port])
            eve.call("PUT", eve.lab(f"/nodes/{node_ids[dev]}/interfaces"), {str(iface): net})
        eve.call("PUT", eve.lab(f"/networks/{net}"), {"visibility": 0})
        print(f"  Link {a_dev}:{a_port} -- {b_dev}:{b_port}")
    return node_ids


def start_nodes(eve: Eve, node_ids):
    nodes = eve.call("GET", eve.lab("/nodes"))["data"]
    for dev, nid in node_ids.items():
        if nodes[str(nid)]["status"] == 2:
            continue
        eve.call("GET", eve.lab(f"/nodes/{nid}/start"))
        print(f"  {dev} gestartet")


# ── Bootstrap über die Konsole ──────────────────────────────────────

BOOTSTRAP_EXPECT = r'''#!/usr/bin/expect -f
# argv: port password mgmt_ip
set timeout 15
set port [lindex $argv 0]
set pw [lindex $argv 1]
set ip [lindex $argv 2]
spawn telnet 127.0.0.1 $port
set deadline [expr {[clock seconds] + 300}]
set loggedin 0
while {!$loggedin} {
  if {[clock seconds] > $deadline} { puts "\nTIMEOUT beim Login"; exit 2 }
  send "\r"
  expect {
    -re {Login: ?$} { send "admin\r"; exp_continue }
    -re {Password: ?$} { send "\r"; exp_continue }
    -re {software license\? \[Y/n\]} { send "n\r"; exp_continue }
    -re {new password> ?$} { send "$pw\r"; exp_continue }
    -re {repeat new password> ?$} { send "$pw\r"; exp_continue }
    -re {Login failed} { puts "\nLogin fehlgeschlagen (schon gebootstrappt?)"; exit 3 }
    -re {\] > ?$} { set loggedin 1 }
    timeout { }
  }
}
foreach cmd [list \
  "/ip dhcp-client remove \[find\]" \
  "/ip address add address=$ip/24 interface=ether1 comment=lab-mgmt" \
  "/ip service set api disabled=no" \
  "/ip service set ssh disabled=no" ] {
  send "$cmd\r"
  expect -re {\] > ?$}
}
send "/quit\r"
expect eof
puts "\nBOOTSTRAP OK"
'''


def eve_ssh(cmd: str, timeout=120, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sshpass", "-p", EVE_ROOT_PASSWORD, "ssh", *SSH_OPTS, f"root@{EVE_HOST}", cmd],
        input=stdin, capture_output=True, text=True, timeout=timeout)


def ros_ssh(ip: str, cmd: str, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sshpass", "-p", ROS_PASSWORD, "ssh", *SSH_OPTS, f"{ROS_USER}@{ip}", cmd],
        capture_output=True, text=True, timeout=timeout)


def reachable(ip: str) -> bool:
    try:
        return ros_ssh(ip, "/system identity print", timeout=20).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def bootstrap(eve: Eve, node_ids):
    eve_ssh("cat > /tmp/ilbs_bootstrap.exp && chmod +x /tmp/ilbs_bootstrap.exp", stdin=BOOTSTRAP_EXPECT)
    nodes = eve.call("GET", eve.lab("/nodes"))["data"]
    for dev, nid in node_ids.items():
        ip = NODES[dev]["ip"]
        if reachable(ip):
            print(f"  {dev}: per SSH erreichbar — Bootstrap übersprungen")
            continue
        port = nodes[str(nid)]["url"].rsplit(":", 1)[1]
        print(f"  {dev}: Bootstrap über Konsole (Port {port}) ...", flush=True)
        res = eve_ssh(f"/tmp/ilbs_bootstrap.exp {port} '{ROS_PASSWORD}' {ip}", timeout=420)
        if "BOOTSTRAP OK" not in res.stdout:
            sys.exit(f"Bootstrap {dev} fehlgeschlagen:\n{res.stdout[-1500:]}\n{res.stderr}")


# ── Config einspielen ───────────────────────────────────────────────

def import_config(dev: str):
    ip = NODES[dev]["ip"]
    ident = ros_ssh(ip, ":put [/system identity get name]").stdout.strip()
    if ident == dev:
        print(f"  {dev}: Identity gesetzt — Config schon importiert, übersprungen")
        return
    rsc = LAB_DIR / "it-fw.rsc" if dev == "it-fw" else CONFIG_DIR / f"{dev}.rsc"
    remote = "lab-import.rsc"
    res = subprocess.run(
        ["sshpass", "-p", ROS_PASSWORD, "scp", "-O", *SSH_OPTS, str(rsc), f"{ROS_USER}@{ip}:{remote}"],
        capture_output=True, text=True, timeout=60)
    if res.returncode:
        sys.exit(f"Upload {dev} fehlgeschlagen: {res.stderr}")
    print(f"  {dev}: importiere {rsc.name} ...", flush=True)
    # Ohne verbose: Erfolg = Meldung + rc 0, Fehler = "Script Error ... line N" + rc 1.
    res = ros_ssh(ip, f"/import file-name={remote}", timeout=300)
    if res.returncode or "executed successfully" not in res.stdout:
        sys.exit(f"Import {dev} fehlgeschlagen:\n{res.stdout.strip()}\n{res.stderr.strip()}")
    ros_ssh(ip, f"/file remove {remote}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recreate", action="store_true", help="Lab vorher löschen")
    ap.add_argument("--skip-import", action="store_true", help="nur Topologie + Bootstrap")
    args = ap.parse_args(argv)

    maps = port_maps()
    eve = Eve()
    node_ids = create_lab(eve, maps, args.recreate)
    print("Starte Nodes")
    start_nodes(eve, node_ids)
    print("Bootstrap")
    bootstrap(eve, node_ids)
    if not args.skip_import:
        print("Config-Import")
        for dev in NODES:
            import_config(dev)
    print("Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
