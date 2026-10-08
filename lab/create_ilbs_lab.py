#!/usr/bin/env python3
"""ILBS-Lab in EVE-NG aufbauen: Topologie, Bootstrap, Kunden-Config.

Schritte (jeweils idempotent, Bestehendes wird übersprungen):
  1. Lab ilbs.unl per EVE-REST-API anlegen bzw. um fehlende Nodes und Links
     ergänzen (neue Nodes in NODES/VPCS/LINKS eintragen, Skript erneut starten)
  2. Nodes starten
  3. Bootstrap über die Telnet-Konsole (Expect auf der EVE-VM):
     Erst-Login + Passwort, Mgmt-IP auf ether1, API an
  4. Config hochladen und importieren (configs/<dev>.rsc bzw. it-fw.rsc)

Testanlagen (Backbone, TNRs, Stubs, PVE) kommen aus configs/ext_topology.json
(gen_ext_config.py, enthält Kundendaten) dazu, falls vorhanden.

Topologie:
  rtr-ilbs-01 ══ sw-ilbs-01 ══ LACP-PEER-LINK ══ sw-ilbs-02 ══ rtr-ilbs-02
                     └── it-fw (IT-Firewall-Simulation, VLAN 1240)
                           └── moa-pc (VPC, Clientnetz IT 172.18.104.0/23)
  ether1 aller CHRs -> Cloud0 (pnet0, 10.0.2.0/24)

Voraussetzung: gen_lab_config.py gelaufen, EVE-VM unter 10.0.2.15 erreichbar,
CHR-Image unter /opt/unetlab/addons/qemu/mikrotik-<version>/.

Aufruf:
  ./create_ilbs_lab.py              # alles
  ./create_ilbs_lab.py --recreate   # Lab vorher löschen (Nodes verlieren ihre Config!)
  ./create_ilbs_lab.py --reimport <node>     # Testanlagen-Node neu konfigurieren
  ./create_ilbs_lab.py --relayout   # Positionen, Mgmt-Wolken und Rahmen neu setzen
"""

import argparse
import base64
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
    "rtr-ilbs-01": {"ip": "10.0.2.101", "pos": (500, 170), "icon": "Router-2D-Gen-White-S.svg"},
    "rtr-ilbs-02": {"ip": "10.0.2.102", "pos": (1140, 170), "icon": "Router-2D-Gen-White-S.svg"},
    "sw-ilbs-01": {"ip": "10.0.2.103", "pos": (500, 500), "icon": "Switch-2D-L3-Generic-S.svg"},
    "sw-ilbs-02": {"ip": "10.0.2.104", "pos": (1140, 500), "icon": "Switch-2D-L3-Generic-S.svg"},
    "it-fw": {"ip": "10.0.2.105", "pos": (1140, 760), "icon": "Firewall-2D-Generic-S.svg"},
}

# it-fw ist handgeschrieben (it-fw.rsc), daher Portmap hier statt aus .ports.json.
IT_FW_PORTS = {"oob": "ether1", "sw-ilbs-01": "ether2", "moa-pc": "ether3"}

# VPCs (EVE "Virtual PC", nur ping/trace): eine NIC eth0, Konfiguration als
# Startup-Config.
VPCS = {
    "moa-pc": {"pos": (1140, 940), "config": "set pcname moa-pc\nip 172.18.104.10/23 172.18.104.1\n"},
}

# Links über Kunden-Portnamen; aufgelöst über configs/<dev>.ports.json.
LINKS = [
    (("rtr-ilbs-01", "sw-ilbs-01-sfp-01"), ("sw-ilbs-01", "rtr-ilbs-01-sfp1")),
    (("rtr-ilbs-01", "sw-ilbs-01-sfp-02"), ("sw-ilbs-01", "rtr-ilbs-01-sfp2")),
    (("rtr-ilbs-02", "sw-ilbs-02-sfp-01"), ("sw-ilbs-02", "rtr-ilbs-02-sfp1")),
    (("rtr-ilbs-02", "sw-ilbs-02-sfp-02"), ("sw-ilbs-02", "rtr-ilbs-02-sfp2")),
    (("sw-ilbs-01", "qsfpplus1-1"), ("sw-ilbs-02", "qsfpplus1-1")),
    (("sw-ilbs-01", "qsfpplus2-1"), ("sw-ilbs-02", "qsfpplus2-1")),
    (("sw-ilbs-01", "sfp-sfpplus5"), ("it-fw", "sw-ilbs-01")),
    (("it-fw", "moa-pc"), ("moa-pc", "eth0")),
]

# Mgmt-Wolken: alle auf pnet0 (Cloud0), je Gruppe eine, damit die ether1-Links
# kurz bleiben. CHRs ohne Eintrag hängen an der ersten Wolke.
CLOUDS = {
    "Mgmt ILBS": {"pos": (820, 30), "nodes": ["rtr-ilbs-01", "rtr-ilbs-02", "sw-ilbs-01", "sw-ilbs-02"]},
    "Mgmt IT": {"pos": (1300, 760), "nodes": ["it-fw"]},
}

# Beschriftete Rahmen um Gruppen (EVE-Formen hinter den Nodes): (left, top, Breite, Höhe)
FRAMES = {
    "ILBS": (440, 120, 780, 470),
    "IT-Netz (simuliert)": (1080, 710, 330, 330),
}

# Testanlagen: Nodes, Links, Wolken, Rahmen aus gen_ext_config.py (nur generiert, gitignored).
EXT_TOPOLOGY = CONFIG_DIR / "ext_topology.json"
EXT_NODES: set[str] = set()
if EXT_TOPOLOGY.exists():
    _ext = json.loads(EXT_TOPOLOGY.read_text())
    NODES.update(_ext["nodes"])
    LINKS += [tuple(tuple(end) for end in link) for link in _ext["links"]]
    VPCS.update(_ext.get("vpcs", {}))
    CLOUDS.update(_ext.get("clouds", {}))
    FRAMES.update({k: tuple(v) for k, v in _ext.get("frames", {}).items()})
    EXT_NODES = set(_ext["nodes"])

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
    maps = {"it-fw": IT_FW_PORTS, **{vpc: {"eth0": "eth0"} for vpc in VPCS}}
    for dev in NODES:
        if dev == "it-fw":
            continue
        f = CONFIG_DIR / f"{dev}.ports.json"
        if not f.exists():
            sys.exit(f"{f} fehlt — erst ./gen_lab_config.py laufen lassen.")
        maps[dev] = json.loads(f.read_text())
    return maps


def eve_iface_id(port: str) -> int:
    # EVE nummeriert die NICs ab 0: e0 = ether1 (CHR) bzw. eth0 (VPC).
    if port.startswith("ether"):
        return int(port.removeprefix("ether")) - 1
    return int(port.removeprefix("eth"))


def ensure_clouds(eve: Eve, networks: dict) -> dict[str, int]:
    """Je Eintrag in CLOUDS ein pnet0-Netz (Abgleich per Name). Eine pnet0-Wolke
    mit fremdem Namen (z.B. aus älteren Labs) übernimmt die erste Wolke."""
    pnet = {n["name"]: int(nid) for nid, n in networks.items() if n["type"] == "pnet0"}
    clouds = {}
    for name, cfg in CLOUDS.items():
        if name in pnet:
            clouds[name] = pnet[name]
            continue
        spare = [nid for n, nid in pnet.items() if n not in CLOUDS and nid not in clouds.values()]
        if not clouds and spare:
            eve.call("PUT", eve.lab(f"/networks/{spare[0]}"), {"id": spare[0], "name": name})
            clouds[name] = spare[0]
        else:
            clouds[name] = int(eve.call("POST", eve.lab("/networks"), {
                "type": "pnet0", "name": name, "left": str(cfg["pos"][0]), "top": str(cfg["pos"][1]),
                "visibility": 1})["data"]["id"])
            print(f"  Wolke {name} angelegt")
    return clouds


def cloud_of(dev: str) -> str:
    return next((name for name, cfg in CLOUDS.items() if dev in cfg["nodes"]), next(iter(CLOUDS)))


def apply_layout(eve: Eve, node_ids: dict[str, int], clouds: dict[str, int]):
    """Nodes und Wolken auf ihre Positionen setzen, Rahmen neu anlegen."""
    for dev, nid in node_ids.items():
        left, top = (NODES.get(dev) or VPCS[dev])["pos"]
        eve.call("PUT", eve.lab(f"/nodes/{nid}"), {"id": nid, "left": str(left), "top": str(top)})
    for name, nid in clouds.items():
        left, top = CLOUDS[name]["pos"]
        eve.call("PUT", eve.lab(f"/networks/{nid}"), {"id": nid, "left": str(left), "top": str(top)})
    for oid, obj in (eve.call("GET", eve.lab("/textobjects"))["data"] or {}).items():
        if obj["name"].startswith(("lab-frame:", "lab-label:")):
            eve.call("DELETE", eve.lab(f"/textobjects/{oid}"))
    for label, (left, top, width, height) in FRAMES.items():
        # Format wie die EVE-Oberfläche (themes/default/js/actions.js); IDs setzt sie beim Laden.
        # z-index 0: hinter den Nodes, die Rahmen fangen keine Klicks ab.
        frame = (f'<div id="customShape0" class="customShape context-menu" data-path="0" '
                 f'style="display:inline;z-index:0;position:absolute;left:{left}px;top:{top}px;" '
                 f'width="{width}px" height="{height}px"><svg width="{width}" height="{height}">'
                 f'<rect width="{width}" height="{height}" fill="none" stroke-width="2" stroke="#8a9bb0" '
                 f'stroke-dasharray="10,6"/></svg></div>')
        text = (f'<div id="customText0" class="customShape customText context-menu" data-path="0" '
                f'style="display:inline;position:absolute;left:{left + 8}px;top:{top + 4}px; cursor:move; ;z-index:1001;">'
                f'<p align="left" style="vertical-align:top;color:#5b6b80;font-size:14px;font-weight: bold;">'
                f'{label}</p></div>')
        for kind, name, html in (("square", f"lab-frame:{label}", frame), ("text", f"lab-label:{label}", text)):
            eve.call("POST", eve.lab("/textobjects"), {
                "name": name, "type": kind, "data": base64.b64encode(html.encode()).decode()})
    print(f"  {len(node_ids)} Nodes, {len(clouds)} Wolken positioniert, {len(FRAMES)} Rahmen")


def create_lab(eve: Eve, maps, recreate: bool) -> tuple[dict[str, int], list, dict[str, int], bool]:
    labs = eve.call("GET", "/api/folders/")["data"]["labs"]
    exists = any(lab["file"] == LAB_PATH for lab in labs)
    if exists and recreate:
        print(f"Lösche bestehendes Lab {LAB_PATH}")
        eve.call("DELETE", eve.lab())
        exists = False
    if exists:
        print(f"Lab {LAB_PATH} existiert — ergänze fehlende Nodes und Links")
        present = {n["name"]: n for n in (eve.call("GET", eve.lab("/nodes"))["data"] or {}).values()}
        networks = eve.call("GET", eve.lab("/networks"))["data"] or {}
    else:
        print(f"Lege Lab {LAB_PATH} an")
        eve.call("POST", "/api/labs", {"path": "/", "name": LAB_NAME, "version": "1",
                                       "author": "", "description": "ILBS-Lab für ansible-tam"})
        present, networks = {}, {}
    running = {dev for dev, n in present.items() if n["status"] == 2}

    node_ids = {}
    for dev, cfg in NODES.items():
        ethernets = max(int(p.removeprefix("ether")) for p in maps[dev].values())
        if dev in present:
            node = present[dev]
            node_ids[dev] = int(node["id"])
            if int(node["ethernet"]) < ethernets:
                # Die NIC-Zahl lässt EVE nur am gestoppten Node ändern; die
                # CHR-Config bleibt erhalten, start_nodes startet ihn wieder.
                eve.call("GET", eve.lab(f"/nodes/{node['id']}/stop"))
                running.discard(dev)
                eve.call("PUT", eve.lab(f"/nodes/{node['id']}"),
                         {"id": node["id"], "ethernet": str(ethernets)})
                print(f"  Node {dev}: {node['ethernet']} -> {ethernets} NICs")
            continue
        res = eve.call("POST", eve.lab("/nodes"), {
            "type": "qemu", "template": "mikrotik", "image": CHR_IMAGE, "name": dev,
            "icon": cfg["icon"], "ethernet": str(ethernets), "ram": cfg.get("ram", "1024"), "cpu": "1",
            "console": "telnet", "left": str(cfg["pos"][0]), "top": str(cfg["pos"][1]),
        })
        node_ids[dev] = int(res["data"]["id"])
        print(f"  Node {dev}: id {node_ids[dev]}, {ethernets} NICs")

    for vpc, cfg in VPCS.items():
        if vpc in present:
            node_ids[vpc] = int(present[vpc]["id"])
            continue
        res = eve.call("POST", eve.lab("/nodes"), {
            "type": "vpcs", "template": "vpcs", "name": vpc, "icon": "PC-2D-Desktop-Generic-S.svg",
            "left": str(cfg["pos"][0]), "top": str(cfg["pos"][1]),
        })
        nid = node_ids[vpc] = int(res["data"]["id"])
        # Erst die Daten, dann das Flag (ohne Daten setzt EVE es auf 0 zurück).
        # EVE schreibt die Startup-Config nur beim ersten Start bzw. nach Wipe.
        eve.call("PUT", eve.lab(f"/configs/{nid}"), {"id": nid, "data": cfg["config"]})
        eve.call("PUT", eve.lab(f"/nodes/{nid}"), {"id": nid, "config": "1"})
        print(f"  VPC {vpc}: id {nid}")

    clouds = ensure_clouds(eve, networks)
    # Aktuelle Belegung: network_id 0 = NIC frei.
    used = {dev: [int(i["network_id"]) for i in
                  eve.call("GET", eve.lab(f"/nodes/{nid}/interfaces"))["data"]["ethernet"]]
            for dev, nid in node_ids.items()}
    for dev in NODES:
        # ether1 an die Wolke der Gruppe; Umhängen zwischen Wolken ist rein optisch
        # (alle auf pnet0), laufende Nodes bleiben verbunden.
        target = clouds[cloud_of(dev)]
        if used[dev][0] != target and (not used[dev][0] or used[dev][0] in clouds.values()):
            eve.call("PUT", eve.lab(f"/nodes/{node_ids[dev]}/interfaces"), {"0": target})

    hot = []  # (node_id, iface, net) laufender Nodes — siehe attach_hot_links
    for (a_dev, a_port), (b_dev, b_port) in LINKS:
        a_if, b_if = eve_iface_id(maps[a_dev][a_port]), eve_iface_id(maps[b_dev][b_port])
        if used[a_dev][a_if] or used[b_dev][b_if]:
            continue
        # Erst sichtbar anlegen: EVE verwirft unsichtbare Netze ohne Verbindung
        # sofort. Nach dem Verbinden als Punkt-zu-Punkt-Link ausblenden.
        net = eve.call("POST", eve.lab("/networks"), {
            "type": "bridge", "name": f"{a_dev}:{a_port}--{b_dev}:{b_port}",
            "left": "0", "top": "0", "visibility": 1,
        })["data"]["id"]
        for dev, iface in ((a_dev, a_if), (b_dev, b_if)):
            eve.call("PUT", eve.lab(f"/nodes/{node_ids[dev]}/interfaces"), {str(iface): net})
            if dev in running:
                hot.append((node_ids[dev], iface, net))
        eve.call("PUT", eve.lab(f"/networks/{net}"), {"visibility": 0})
        print(f"  Link {a_dev}:{a_port} -- {b_dev}:{b_port}")
    return node_ids, hot, clouds, not exists


def attach_hot_links(hot):
    """EVE CE hängt laufende Nodes nicht an neue Links (keine Hot-Links): den
    TAP direkt an die Link-Bridge hängen, mit EVEs Bridge-Einstellungen (LACP
    braucht group_fwd_mask). Nach einem Node-Neustart macht EVE das selbst."""
    for nid, iface, net in hot:
        br, tap = f"vnet0_{net}", f"vunl0_{nid}_{iface}"
        res = eve_ssh(
            f"ip link show {br} >/dev/null 2>&1 || {{ ip link add {br} type bridge"
            f" && echo 65535 > /sys/class/net/{br}/bridge/group_fwd_mask"
            f" && ip link set {br} mtu 9000 up; }}; ip link set {tap} master {br}")
        if res.returncode:
            sys.exit(f"Hot-Link {tap} -> {br} fehlgeschlagen: {res.stderr.strip()}")
        print(f"  {tap} -> {br} (laufender Node, direkt verbunden)")


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


def reachable(ip: str, wait: int = 0) -> bool:
    deadline = time.monotonic() + wait
    while True:
        try:
            if ros_ssh(ip, "/system identity print", timeout=20).returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() > deadline:
            return False
        time.sleep(10)


def bootstrap(eve: Eve, node_ids):
    eve_ssh("cat > /tmp/ilbs_bootstrap.exp && chmod +x /tmp/ilbs_bootstrap.exp", stdin=BOOTSTRAP_EXPECT)
    nodes = eve.call("GET", eve.lab("/nodes"))["data"]
    for dev in NODES:
        nid, ip = node_ids[dev], NODES[dev]["ip"]
        if reachable(ip):
            print(f"  {dev}: per SSH erreichbar — Bootstrap übersprungen")
            continue
        port = nodes[str(nid)]["url"].rsplit(":", 1)[1]
        print(f"  {dev}: Bootstrap über Konsole (Port {port}) ...", flush=True)
        res = eve_ssh(f"/tmp/ilbs_bootstrap.exp {port} '{ROS_PASSWORD}' {ip}", timeout=420)
        if "BOOTSTRAP OK" in res.stdout:
            continue
        # Login ohne Passwort scheitert: schon gebootstrappt, nur (neu) gestartet.
        if res.returncode == 3 and reachable(ip, wait=180):
            print(f"  {dev}: schon gebootstrappt, nach dem Start per SSH erreichbar")
            continue
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


def reimport_config(dev: str):
    """Testanlagen-Node auf die aktuelle .rsc zurücksetzen: Reset ohne Defaults,
    die .rsc läuft danach als run-after-reset (setzt ihre Mgmt-IP selbst)."""
    if dev not in EXT_NODES:
        sys.exit(f"--reimport nur für Testanlagen-Nodes ({', '.join(sorted(EXT_NODES))}), nicht {dev}")
    ip = NODES[dev]["ip"]
    remote = "lab-import.rsc"
    res = subprocess.run(
        ["sshpass", "-p", ROS_PASSWORD, "scp", "-O", *SSH_OPTS, str(CONFIG_DIR / f"{dev}.rsc"),
         f"{ROS_USER}@{ip}:{remote}"], capture_output=True, text=True, timeout=60)
    if res.returncode:
        sys.exit(f"Upload {dev} fehlgeschlagen: {res.stderr}")
    print(f"  {dev}: Reset + {dev}.rsc ...", flush=True)
    # :execute läuft als Skript — ohne die interaktive Rückfrage des Resets.
    ros_ssh(ip, f":execute {{/system reset-configuration no-defaults=yes keep-users=yes"
                f" skip-backup=yes run-after-reset={remote}}}")
    time.sleep(30)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if reachable(ip, wait=60) and ros_ssh(ip, ":put [/system identity get name]").stdout.strip() == dev:
            print(f"  {dev}: neu konfiguriert")
            return
        time.sleep(10)
    sys.exit(f"{dev}: nach dem Reset nicht mit Identity {dev} erreichbar")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recreate", action="store_true", help="Lab vorher löschen")
    ap.add_argument("--skip-import", action="store_true", help="nur Topologie + Bootstrap")
    ap.add_argument("--reimport", nargs="+", metavar="NODE", help="Testanlagen-Nodes neu konfigurieren")
    ap.add_argument("--relayout", action="store_true", help="Positionen, Wolken und Rahmen neu setzen")
    args = ap.parse_args(argv)

    if args.reimport:
        for dev in args.reimport:
            reimport_config(dev)
        return 0
    maps = port_maps()
    eve = Eve()
    node_ids, hot, clouds, new = create_lab(eve, maps, args.recreate)
    if new or args.relayout:
        print("Layout")
        apply_layout(eve, node_ids, clouds)
    print("Starte Nodes")
    start_nodes(eve, node_ids)
    attach_hot_links(hot)
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
