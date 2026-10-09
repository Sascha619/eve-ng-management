#!/usr/bin/env python3
"""Testanlagen hinter den ILBS-Routern -> CHR-Lab-Konfiguration.

Liest die lokale Spec (ext-lab.local.toml, Format: ext-lab.example.toml), die
Aruba-/Cisco-Exports der Testanlagen und die NAT-Ziele (nat_inside der
pve_nat-Tags) aus der Lab-NetBox. Erzeugt in configs/ (gitignored):

  <node>.rsc, <node>.ports.json   wie gen_lab_config.py
  ext_topology.json               Nodes, Links, Probes, Zielhosts — gelesen von
                                  create_ilbs_lab.py und test_nat_paths.py

Nachbau (nur L3, was für Routing und NAT zählt):

- Backbone (Aruba): Trunk -> Bond (802.3ad, ein Slave) zum ILBS-Switch, VLANs
  mit IP auf dem Trunk -> VLAN-Interfaces, geroutete VLANs mit genau einem
  untagged Port -> eigener Port zum Node aus dem VLAN-Namen. Keine Bridge,
  damit der Node nicht am STP der ILBS-Switches teilnimmt. Statische Routen wie
  im Export; Ziele mit Backup-Route (distance) bekommen check-gateway=ping
  (Lab-Abweichung: in EVE bleibt der Link eines gestoppten Nodes oben).
  Drucker-VLAN (printer_vlan, ohne IP): VLAN-Interface auf dem Trunk + Bridge
  mit je einem Port zum Drucker-LAN jeder Site (statt untagged-Port + Switch).
- TNR (Cisco): BDI1 -> Port "uplink", alle SVIs als VLAN-Interfaces auf Port
  "lan" mit VRRP (vrid = Gruppe, Priorität aus dem Export), Loopback0, Routen.
  Weggelassen: MTU, Tracking, QoS, NetFlow, Auth, Access-Ports.
- Site-LAN: ein Switch für beide TNRs (ersetzt Crosslink und Access-Ports,
  VLAN 511 läuft hier durch) mit den Zielhosts: je VLAN ein VRF mit den
  Host-Adressen und Default-Route über die VRRP-Adresse. Mit "printer":
  Access-Port im Drucker-LAN zum Backbone, druckender Host als Probe. Mit
  "vpcs": diese Zielhosts als VPC an einem Access-Port statt als VRF-Adresse.
- Stubs (Gegenstellen ohne Export): Uplink-Adressen + Default-Route zurück,
  Zielhosts als /32 auf einer Loopback-Bridge.
- PVE: Bond zum PVE-Port des Switches, Mgmt untagged; mit VMs eine VLAN-Bridge
  wie vmbr0: VMs als Probe-VRFs und als VPCs an Access-Ports.

Jede .rsc setzt ihre Mgmt-IP selbst (für Re-Import per reset-configuration).

Aufruf:
  ./gen_ext_config.py [--spec ext-lab.local.toml] [--netbox-url ...]
"""

import argparse
import ipaddress
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent
CONFIG_DIR = LAB_DIR / "configs"
NETBOX_URL = "http://localhost:8001"
NETBOX_TOKEN = "0123456789abcdef0123456789abcdef01234567"

ICONS = {
    "backbone": "Switch-2D-L3-Generic-S.svg",
    "tnr": "Router-2D-Gen-Grey-S.svg",
    "lan": "Switch-2D-L2-Generic-S.svg",
    "stub": "Router-2D-Gen-Dark-S.svg",
    "pve": "Server-2D-Hypervisor-S.svg",
}


def iface(addr: str, mask: str) -> ipaddress.IPv4Interface:
    return ipaddress.IPv4Interface(f"{addr}/{mask}")


def q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ── Parser ───────────────────────────────────────────────────────────

def parse_aruba(text: str) -> dict:
    dev = {"hostname": None, "trunks": {}, "vlans": {}, "routes": []}
    vlan = None
    for line in text.splitlines():
        s = line.strip()
        if m := re.match(r'hostname "(.+)"$', line):
            dev["hostname"] = m[1]
        elif m := re.match(r"trunk (\S+) (\S+) (\w+)$", line):
            dev["trunks"][m[2].lower()] = m[1]
        elif m := re.match(r"ip route (\S+) (\S+) (\S+)(?: distance (\d+))?$", line):
            dev["routes"].append({"dst": str(ipaddress.IPv4Network(f"{m[1]}/{m[2]}")),
                                  "gw": m[3], "distance": int(m[4] or 1)})
        elif m := re.match(r"vlan (\d+)$", line):
            vlan = dev["vlans"].setdefault(int(m[1]), {"name": "", "untagged": [], "tagged": [], "ip": None})
        elif vlan is not None and line.startswith("   "):
            if s == "exit":
                vlan = None
            elif m := re.match(r'name "(.*)"$', s):
                vlan["name"] = m[1]
            elif m := re.match(r"(untagged|tagged) (\S+)$", s):
                vlan[m[1]] = [p.lower() for p in m[2].split(",")]
            elif m := re.match(r"ip address (\S+) (\S+)$", s):
                vlan["ip"] = iface(m[1], m[2])
        else:
            vlan = None
    return dev


def parse_cisco(text: str) -> dict:
    dev = {"hostname": None, "ifaces": {}, "routes": []}
    cur = None
    for line in text.splitlines():
        if m := re.match(r"hostname (\S+)$", line):
            dev["hostname"] = m[1]
        elif m := re.match(r"interface (\S+)$", line):
            cur = dev["ifaces"].setdefault(m[1], {"description": "", "ip": None, "shutdown": False, "vrrp": {}})
        elif cur is not None and line.startswith(" "):
            s = line.strip()
            if s.startswith("description "):
                cur["description"] = s.removeprefix("description ")
            elif m := re.match(r"ip address (\S+) (\S+)$", s):
                cur["ip"] = iface(m[1], m[2])
            elif s == "shutdown":
                cur["shutdown"] = True
            elif m := re.match(r"vrrp (\d+) ip (\S+)$", s):
                cur["vrrp"].setdefault(int(m[1]), {"priority": 100})["ip"] = m[2]
            elif m := re.match(r"vrrp (\d+) priority (\d+)$", s):
                cur["vrrp"].setdefault(int(m[1]), {"priority": 100})["priority"] = int(m[2])
        else:
            cur = None
            # Nur globale Routen; "ip route vrf Mgmt-intf ..." ist Geräte-Mgmt.
            if m := re.match(r"ip route (\d\S+) (\S+) (\S+)(?: (\d+))?$", line):
                dev["routes"].append({"dst": str(ipaddress.IPv4Network(f"{m[1]}/{m[2]}")),
                                      "gw": m[3], "distance": int(m[4] or 1)})
    return dev


def cisco_svis(dev: dict) -> dict[int, dict]:
    """VlanN-SVIs mit IP, nicht shutdown."""
    return {int(name[4:]): i for name, i in dev["ifaces"].items()
            if name.startswith("Vlan") and i["ip"] and not i["shutdown"]}


def cisco_addresses(dev: dict) -> set[str]:
    addrs = set()
    for i in dev["ifaces"].values():
        if i["ip"]:
            addrs.add(str(i["ip"].ip))
        addrs.update(v["ip"] for v in i["vrrp"].values() if "ip" in v)
    return addrs


# ── NetBox ───────────────────────────────────────────────────────────

def netbox_get(url: str, token: str, path: str):
    req = urllib.request.Request(url.rstrip("/") + path, headers={
        "Authorization": f"Token {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def nat_backends(url: str, token: str, tags: list[str]) -> list[dict]:
    """(backend, vrf) aller VIPs mit einem der Tags; vrf None = main."""
    pairs = []
    for tag in tags:
        try:
            vips = netbox_get(url, token, f"/api/ipam/ip-addresses/?tag={tag}&limit=0")["results"]
        except urllib.error.HTTPError as e:
            if e.code == 400:  # Tag existiert nicht
                continue
            raise
        inside_ids = [v["nat_inside"]["id"] for v in vips if v.get("nat_inside")]
        inside = {}
        for i in range(0, len(inside_ids), 50):
            ids = "&".join(f"id={x}" for x in inside_ids[i:i + 50])
            for ip in netbox_get(url, token, f"/api/ipam/ip-addresses/?{ids}&limit=0")["results"]:
                inside[ip["id"]] = ip
        for v in vips:
            if not v.get("nat_inside"):
                continue
            b = inside[v["nat_inside"]["id"]]
            pairs.append({"backend": b["address"].split("/")[0],
                          "vrf": (b["vrf"] or {}).get("name", "main"),
                          "vip": v["address"].split("/")[0], "tag": tag,
                          "vip_vrf": (v["vrf"] or {}).get("name", "main"),
                          "description": v["description"] or b["description"] or ""})
    return pairs


# ── RouterOS-Ausgabe ─────────────────────────────────────────────────

class Rsc:
    def __init__(self, node: str, mgmt: str, source: str):
        self.node = node
        self.lines = [
            f"# Lab-Konfiguration für {node}, generiert von gen_ext_config.py",
            f"# aus {source} — nicht von Hand ändern, neu generieren.",
            ":delay 5s",  # run-after-reset: Interfaces erst hochkommen lassen
            # Nach einem Reset legt der CHR wieder einen DHCP-Client an — dessen
            # Default-Route über 10.0.2.1 stünde per ECMP neben den Lab-Routen.
            "/ip dhcp-client remove [find]",
            f':if ([:len [/ip address find address="{mgmt}/24"]] = 0) do={{'
            f"/ip address add address={mgmt}/24 interface=ether1 comment=lab-mgmt}}",
            "/ip service set api disabled=no",
            "/ip service set ssh disabled=no",
        ]
        self.ports = {"mgmt": "ether1"}

    def port(self, name: str, comment: str = "") -> str:
        eth = f"ether{len(self.ports) + 1}"
        self.ports[name] = eth
        self.add("/interface ethernet",
                 f"set [ find default-name={eth} ] name={name}" + (f" comment={q(comment)}" if comment else ""))
        return name

    def add(self, section: str, *lines: str):
        self.lines += [section, *lines]

    def vrf(self, name: str, interfaces: list[str]):
        # Routing-Tabelle des VRFs entsteht asynchron (siehe gen_lab_config.py).
        self.add("/ip vrf", f"add name={name} interfaces={','.join(interfaces)}")
        self.lines.append(":delay 3s")

    def write(self):
        self.add("/system identity", f"set name={self.node}")
        (CONFIG_DIR / f"{self.node}.rsc").write_text("\n".join(self.lines) + "\n")
        (CONFIG_DIR / f"{self.node}.ports.json").write_text(json.dumps(self.ports, indent=2) + "\n")


def route_lines(routes: list[dict], suffix: str = "") -> list[str]:
    """Ziele mit mehreren Routen (Backup per distance) bekommen check-gateway."""
    multi = {r["dst"] for r in routes if sum(x["dst"] == r["dst"] for x in routes) > 1}
    out = []
    for r in routes:
        line = f"add dst-address={r['dst']} gateway={r['gw']}{suffix}"
        if r["distance"] != 1:
            line += f" distance={r['distance']}"
        if r["dst"] in multi:
            line += ' check-gateway=ping comment="Lab: check-gateway, damit die Backup-Route greift"'
        out.append(line)
    return out


def other_host(net_if: ipaddress.IPv4Interface) -> str:
    """Gegenstelle in einem /30 (bzw. erste andere Host-Adresse)."""
    return str(next(h for h in net_if.network.hosts() if h != net_if.ip))


# ── Aufbau ───────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", type=Path, default=LAB_DIR / "ext-lab.local.toml")
    ap.add_argument("--netbox-url", default=NETBOX_URL)
    ap.add_argument("--netbox-token", default=NETBOX_TOKEN)
    args = ap.parse_args(argv)

    if not args.spec.exists():
        sys.exit(f"{args.spec} fehlt — Vorlage: {LAB_DIR / 'ext-lab.example.toml'}")
    spec = tomllib.loads(args.spec.read_text())
    export_dir = (LAB_DIR / spec["export_dir"]).resolve()
    ram = spec.get("ram", "256")
    CONFIG_DIR.mkdir(exist_ok=True)

    nodes, links, probes = {}, [], {}
    vpcs: dict[str, dict] = {}        # VPC-Nodes (Site-LAN-Zielhosts, PVE-VMs)
    devices: dict[str, str] = {}      # Adressen der Lab-Geräte selbst -> Node
    hosts: dict[str, str] = {}        # platzierte Zielhosts -> Node
    stub_hosts: dict[str, list] = {}  # Stub-Node -> Adressen
    printer: dict[str, dict] = {}     # Site -> Drucker-LAN (Netz, VRRP, TNRs)

    def add_node(name, cfg, kind):
        nodes[name] = {"ip": cfg["mgmt"], "pos": cfg["pos"], "icon": ICONS[kind], "ram": ram}

    def attach(spec_attach: str) -> tuple[str, str]:
        dev, port = spec_attach.split(":", 1)
        return dev, port

    # Exports
    bb_spec = spec["backbone"]
    bb = parse_aruba((export_dir / bb_spec["export"]).read_text())
    sites = []
    for site in spec.get("sites", []):
        tnrs = [(t, parse_cisco((export_dir / t["export"]).read_text())) for t in site["tnrs"]]
        sites.append((site, tnrs))

    # NAT-Ziele aus NetBox (eindeutig je Adresse+VRF)
    pairs = nat_backends(args.netbox_url, args.netbox_token, spec["nat_tags"])
    backends = sorted({(p["backend"], p["vrf"]) for p in pairs})
    labels = {}
    for p in pairs:
        labels.setdefault(p["backend"], f"NAT-Ziel {p['vip']}" + (f" ({p['description']})" if p["description"] else ""))

    # ── Backbone ──
    rsc = Rsc(bb_spec["node"], bb_spec["mgmt"], bb_spec["export"])
    add_node(bb_spec["node"], bb_spec, "backbone")
    sw_dev, sw_port = attach(bb_spec["uplink"])
    trunk_port = rsc.port(sw_dev, f"Lab: statt {', '.join(bb['trunks'].values())}")
    trunk_name, trunk_members = next(iter(bb["trunks"].items()))
    rsc.add("/interface bonding",
            f"add lacp-rate=1sec mode=802.3ad name={trunk_name} slaves={trunk_port}"
            f" comment={q('Lab: ein Slave statt ' + trunk_members)}")
    links.append([[bb_spec["node"], trunk_port], [sw_dev, sw_port]])
    vlan_lines, addr_lines = [], []
    vlan_nodes = bb_spec.get("vlan_nodes", {})
    node_names = {t["node"] for s, tnrs in sites for t, _ in tnrs} | {s["node"] for s in spec.get("stubs", [])}
    stub_uplinks: dict[str, list] = {}
    for vid, v in sorted(bb["vlans"].items()):
        if v["ip"] is None:
            continue
        devices[str(v["ip"].ip)] = bb_spec["node"]
        if trunk_name in v["tagged"]:
            vlan_lines.append(f"add interface={trunk_name} name=vlan{vid} vlan-id={vid} comment={q(v['name'])}")
            addr_lines.append(f"add address={v['ip']} interface=vlan{vid}")
        elif len(v["untagged"]) == 1 and not v["tagged"]:
            peer = vlan_nodes.get(v["name"], v["name"])
            if peer not in node_names:
                print(f"  {bb_spec['node']}: VLAN {vid} {v['name']!r} — kein Node in der Spec, übersprungen")
                continue
            port = rsc.port(v["name"], f"VLAN {vid} untagged {v['untagged'][0]}")
            addr_lines.append(f"add address={v['ip']} interface={port}")
            if peer in {s["node"] for s in spec.get("stubs", [])}:
                stub_uplinks.setdefault(peer, []).append((port, v["ip"]))
            else:
                links.append([[bb_spec["node"], port], [peer, "uplink"]])
    # Drucker-VLAN: reines L2 vom Trunk zu den Drucker-LANs der Sites. Die Ports
    # erst nach der VLAN-Schleife anlegen, damit bestehende etherN-Links bleiben.
    pvlan = bb_spec.get("printer_vlan")
    printer_sites = [s for s, _ in sites if "printer" in s]
    if printer_sites and not pvlan:
        sys.exit("Sites mit printer brauchen backbone.printer_vlan")
    if pvlan:
        pv = bb["vlans"].get(pvlan, {"name": "", "untagged": []})
        vlan_lines.append(f"add interface={trunk_name} name=vlan{pvlan} vlan-id={pvlan} comment={q(pv['name'])}")
    rsc.add("/interface vlan", *vlan_lines)
    if pvlan:
        on = ",".join(pv["untagged"]) or "?"
        pports = [rsc.port(f"{s['name']}-printer", f"VLAN {pvlan} untagged {on} -> {s['lan']['node']}")
                  for s in printer_sites]
        rsc.add("/interface bridge", f"add name=printer protocol-mode=none"
                f" comment={q(f'VLAN {pvlan} L2 bis zum Router (Lab: je Drucker-LAN ein Port statt {on})')}")
        rsc.add("/interface bridge port", *[f"add bridge=printer interface={p}" for p in [f"vlan{pvlan}", *pports]])
        links += [[[bb_spec["node"], p], [s["lan"]["node"], bb_spec["node"]]] for s, p in zip(printer_sites, pports)]
    rsc.add("/ip address", *addr_lines)
    rsc.add("/ip route", *route_lines(bb["routes"]))
    rsc.write()

    # ── Sites: TNRs + LAN ──
    for site, tnrs in sites:
        lan = site["lan"]
        add_node(lan["node"], lan, "lan")
        svis_all = {}
        for t, dev in tnrs:
            add_node(t["node"], t, "tnr")
            for a in cisco_addresses(dev):
                devices[a] = t["node"]
            rsc = Rsc(t["node"], t["mgmt"], t["export"])
            bdi = dev["ifaces"]["BDI1"]
            rsc.port("uplink", f"BDI1 {bdi['description']}")
            rsc.port("lan", f"Lab: Site-Switch {lan['node']} statt Crosslink/Access-Ports")
            rsc.add("/interface bridge", 'add name=Loopback0 comment="Loopback0"')
            svis = cisco_svis(dev)
            svis_all.update({vid: i for vid, i in svis.items() if vid not in svis_all})
            rsc.add("/interface vlan", *[f"add interface=lan name=Vlan{vid} vlan-id={vid} comment={q(i['description'])}"
                                         for vid, i in sorted(svis.items())])
            vrrp = [f"add interface=Vlan{vid} name=vrrp{g} vrid={g} priority={v['priority']}"
                    for vid, i in sorted(svis.items()) for g, v in sorted(i["vrrp"].items())]
            if vrrp:
                rsc.add("/interface vrrp", *vrrp)
            addrs = [f"add address={bdi['ip']} interface=uplink"]
            lo = dev["ifaces"].get("Loopback0")
            if lo and lo["ip"]:
                addrs.append(f"add address={lo['ip']} interface=Loopback0")
            for vid, i in sorted(svis.items()):
                addrs.append(f"add address={i['ip']} interface=Vlan{vid}")
                addrs += [f"add address={v['ip']}/32 interface=vrrp{g}" for g, v in sorted(i["vrrp"].items())]
            rsc.add("/ip address", *addrs)
            rsc.add("/ip route", *route_lines(dev["routes"]))
            rsc.write()
            links.append([[lan["node"], t["node"]], [t["node"], "lan"]])

        # LAN-Switch mit Zielhosts
        rsc = Rsc(lan["node"], lan["mgmt"], ", ".join(t["export"] for t, _ in tnrs))
        for t, _ in tnrs:
            rsc.port(t["node"])

        def svi_of(addr: str) -> int | None:
            return next((v for v, i in svis_all.items() if ipaddress.IPv4Address(addr) in i["ip"].network), None)

        def gw_of(vid: int) -> str:
            i = svis_all[vid]
            return next((v["ip"] for v in i["vrrp"].values() if "ip" in v), str(i["ip"].ip))

        # Access-Ports (untagged) nach den TNR-Ports: Drucker-LAN zum Backbone, VPCs
        access: list[tuple[str, int]] = []
        pr = site.get("printer")
        if pr and pr["vlan"] not in svis_all:
            sys.exit(f"{site['name']}: printer.vlan {pr['vlan']} ist kein SVI der TNRs")
        if pr:
            access.append((rsc.port(bb_spec["node"], f"Drucker-LAN zu {bb_spec['node']} {site['name']}-printer"
                                                     f" ({bb_spec['node']}-VLAN {pvlan})"), pr["vlan"]))
        site_vpcs: dict[str, str] = {}  # Zielhost als VPC statt als VRF-Adresse: Adresse -> VPC
        for v in site.get("vpcs", []):
            vid = svi_of(v["address"])
            if vid is None:
                sys.exit(f"{site['name']}: VPC {v['name']} {v['address']} liegt in keinem SVI-Netz")
            access.append((rsc.port(v["name"], f"VPC {v['name']} (VLAN {vid})"), vid))
            vpcs[v["name"]] = {"pos": v["pos"], "config": f"set pcname {v['name']}\n"
                               f"ip {v['address']}/{svis_all[vid]['ip'].network.prefixlen} {gw_of(vid)}\n"}
            links.append([[lan["node"], v["name"]], [v["name"], "eth0"]])
            site_vpcs[v["address"]] = v["name"]
        rsc.add("/interface bridge", 'add name=site protocol-mode=none vlan-filtering=yes comment="Lab: Site-Switch"')
        rsc.add("/interface bridge port", *[f"add bridge=site interface={t['node']} frame-types=admit-only-vlan-tagged"
                                            for t, _ in tnrs],
                *[f"add bridge=site interface={p} pvid={vid} frame-types=admit-only-untagged-and-priority-tagged"
                  for p, vid in access])
        tagged = f"site,{','.join(t['node'] for t, _ in tnrs)}"
        untagged: dict[int, list[str]] = {}
        for p, vid in access:
            untagged.setdefault(vid, []).append(p)
        vids = ",".join(str(v) for v in sorted(svis_all) if v not in untagged)
        rsc.add("/interface bridge vlan", f"add bridge=site tagged={tagged} vlan-ids={vids}",
                *[f"add bridge=site tagged={tagged} untagged={','.join(ps)} vlan-ids={vid}"
                  for vid, ps in sorted(untagged.items())])
        site_hosts: dict[int, list] = {}
        for b, vrf in backends:
            if vrf != bb_spec["vrf"] or b in devices:
                continue
            if b in site_vpcs:
                hosts[b] = site_vpcs[b]
                continue
            for vid, i in svis_all.items():
                if ipaddress.IPv4Address(b) in i["ip"].network:
                    site_hosts.setdefault(vid, []).append(b)
                    hosts[b] = lan["node"]
        if pr:
            # Druckender Host: meist schon Zielhost, sonst als Probe-Adresse dazu.
            cvid = svi_of(pr["client"])
            if cvid is None:
                sys.exit(f"{site['name']}: printer.client {pr['client']} liegt in keinem SVI-Netz")
            if pr["client"] in site_vpcs:
                sys.exit(f"{site['name']}: printer.client {pr['client']} ist ein VPC — das Testskript braucht ein VRF")
            if pr["client"] not in site_hosts.setdefault(cvid, []):
                site_hosts[cvid].append(pr["client"])
            probes[f"{site['name']}-printer-client"] = {"node": lan["node"], "vrf": f"v{cvid}", "side": bb_spec["vrf"],
                                                        "src": pr["client"], "printer": True}
            psvi = svis_all[pr["vlan"]]
            printer[site["name"]] = {"net": str(psvi["ip"].network), "vrrp": sorted(psvi["vrrp"]),
                                     "tnrs": [t["node"] for t, _ in tnrs]}
        probe = site.get("probe")
        if probe:
            site_hosts.setdefault(probe["vlan"], [])
        if not site_hosts:
            print(f"  {lan['node']}: keine Zielhosts")
        vlans, addrs, routes = [], [], []
        for vid, addr_list in sorted(site_hosts.items()):
            i = svis_all[vid]
            gw = gw_of(vid)
            vlans.append(f"add interface=site name=vlan{vid} vlan-id={vid} comment={q(i['description'])}")
            for a in addr_list:
                addrs.append(f"add address={a}/{i['ip'].network.prefixlen} interface=vlan{vid}"
                             f" comment={q(labels.get(a, 'Lab-Probe Drucker-Client'))}")
            if probe and probe["vlan"] == vid:
                addrs.append(f"add address={probe['address']}/{i['ip'].network.prefixlen} interface=vlan{vid}"
                             ' comment="Lab-Probe Client"')
                probes[f"{site['name']}-client"] = {"node": lan["node"], "vrf": f"v{vid}", "side": bb_spec["vrf"],
                                                    "src": probe["address"]}
            routes.append(f"add dst-address=0.0.0.0/0 gateway={gw}@v{vid} routing-table=v{vid}")
        rsc.add("/interface vlan", *vlans)
        for vid in sorted(site_hosts):
            # VRF und Interface brauchen verschiedene Namen (VRF ist selbst ein Device).
            rsc.vrf(f"v{vid}", [f"vlan{vid}"])
        rsc.add("/ip address", *addrs)
        rsc.add("/ip route", *routes)
        rsc.write()

    # ── Stubs ──
    for st in spec.get("stubs", []):
        add_node(st["node"], st, "stub")
        rsc = Rsc(st["node"], st["mgmt"], "ext-lab.local.toml")
        addrs, routes = [], []
        if "attach" in st:
            port = rsc.port("uplink")
            dev, sp = attach(st["attach"])
            links.append([[st["node"], port], [dev, sp]])
            addrs.append(f"add address={st['address']} interface={port}")
            routes.append(f"add dst-address=0.0.0.0/0 gateway={st['gateway']}")
            devices[st["address"].split("/")[0]] = st["node"]
            probes[st["node"]] = {"node": st["node"], "vrf": "main", "side": st["vrf"],
                                  "src": st["address"].split("/")[0], "gateway": st["gateway"]}
        for n, (bb_port, bb_ip) in enumerate(stub_uplinks.get(st["node"], []), start=1):
            port = rsc.port(f"uplink{n}", f"zu {bb_spec['node']} {bb_port}")
            links.append([[st["node"], port], [bb_spec["node"], bb_port]])
            mine = other_host(bb_ip)
            devices[mine] = st["node"]
            addrs.append(f"add address={mine}/{bb_ip.network.prefixlen} interface={port}")
            routes.append(f"add dst-address=0.0.0.0/0 gateway={bb_ip.ip}"
                          + ("" if n == 1 else f" distance={n * 10 - 10}")
                          + (" check-gateway=ping" if len(stub_uplinks[st["node"]]) > 1 else ""))
        rsc.add("/interface bridge", 'add name=lo-hosts comment="Zielhosts"')
        nets = [ipaddress.IPv4Network(n) for n in st.get("hosts_in", [])]
        for b, vrf in backends:
            if vrf == st["vrf"] and b not in devices and any(ipaddress.IPv4Address(b) in n for n in nets):
                addrs.append(f"add address={b}/32 interface=lo-hosts comment={q(labels[b])}")
                hosts[b] = st["node"]
        rsc.add("/ip address", *addrs)
        rsc.add("/ip route", *routes)
        rsc.write()

    # ── PVE ──
    pve_mgmt = []
    for pve in spec.get("pve", []):
        add_node(pve["node"], pve, "pve")
        rsc = Rsc(pve["node"], pve["mgmt"], "ext-lab.local.toml")
        dev, sp = attach(pve["attach"])
        port = rsc.port(sp.rsplit("-", 1)[-1], f"zu {dev} {sp}")
        links.append([[pve["node"], port], [dev, sp]])
        rsc.add("/interface bonding", f"add lacp-rate=1sec mode=802.3ad name=bond0 slaves={port}")
        vms = pve.get("vms", [])
        mgmt_if = "bond0"
        if vms:
            # Wie vmbr0 (VLAN-aware) auf dem PVE: Mgmt untagged auf dem Bond,
            # VM-VLANs getaggt, VPCs als VMs an Access-Ports. Kein STP — ein
            # PVE nimmt nicht am STP der ILBS-Switches teil.
            mgmt_if = "vmbr0"
            vpc_ports = []
            for vm in vms:
                if "vpc" in vm:
                    # tap-<vm> wie die VM-NICs auf dem PVE; nicht <vm> — so heißt schon das VRF.
                    vpc_ports.append((vm, rsc.port(f"tap-{vm['name']}", f"VPC {vm['name']} (VLAN {vm['vlan']})")))
            rsc.add("/interface bridge", 'add name=vmbr0 protocol-mode=none vlan-filtering=yes comment="wie PVE vmbr0"')
            rsc.add("/interface bridge port", "add bridge=vmbr0 interface=bond0",
                    *[f"add bridge=vmbr0 interface={p} pvid={vm['vlan']} frame-types=admit-only-untagged-and-priority-tagged"
                      for vm, p in vpc_ports])
            rsc.add("/interface bridge vlan", *[
                f"add bridge=vmbr0 tagged=vmbr0,bond0 vlan-ids={vm['vlan']}"
                + "".join(f" untagged={p}" for v, p in vpc_ports if v is vm) for vm in vms])
            rsc.add("/interface vlan", *[f"add interface=vmbr0 name=vlan{vm['vlan']} vlan-id={vm['vlan']} comment={vm['name']}"
                                         for vm in vms])
            for vm in vms:
                rsc.vrf(vm["name"], [f"vlan{vm['vlan']}"])
            for vm, p in vpc_ports:
                ip, gw = vm["vpc"], vm["gateway"]
                vpcs[vm["name"]] = {"pos": vm["vpc_pos"], "config": f"set pcname {vm['name']}\nip {ip} {gw}\n"}
                links.append([[pve["node"], p], [vm["name"], "eth0"]])
                devices[ip.split("/")[0]] = vm["name"]
        rsc.add("/ip address",
                f"add address={pve['address']} interface={mgmt_if} comment=\"PVE-Mgmt (untagged)\"",
                *[f"add address={vm['address']} interface=vlan{vm['vlan']} comment=\"Lab-Probe {vm['name']}\""
                  for vm in vms])
        rsc.add("/ip route", f"add dst-address=0.0.0.0/0 gateway={pve['gateway']}",
                *[f"add dst-address=0.0.0.0/0 gateway={vm['gateway']}@{vm['name']} routing-table={vm['name']}"
                  for vm in vms])
        rsc.write()
        devices[pve["address"].split("/")[0]] = pve["node"]
        pve_mgmt.append(pve["address"].split("/")[0])
        for vm in vms:
            probes[vm["name"]] = {"node": pve["node"], "vrf": vm["name"], "src": vm["address"].split("/")[0],
                                  "gateway": vm["gateway"], "vm_vlan": vm["vlan"]}

    # ── Prüfungen + Topologie ──
    tests = spec.get("tests", {})
    # Drucker-Clients dürfen Zielhosts sein: Sie nutzen deren Adresse, legen keine zweite an.
    clash = [p["src"] for p in probes.values() if not p.get("printer") and any(p["src"] == b for b, _ in backends)]
    if clash:
        sys.exit(f"Probe-Adressen sind NAT-Ziele: {clash} — andere Adressen in der Spec wählen.")
    known = set(tests.get("known_hosts", []))
    unplaced = sorted(f"{b}@{vrf}" for b, vrf in backends if b not in hosts and b not in devices and b not in known)
    topo = {
        "nodes": nodes, "links": links, "probes": probes,
        "hosts": hosts, "devices": {b: devices[b] for b, _ in backends if b in devices},
        "known_hosts": sorted(known), "expect_unreachable": tests.get("expect_unreachable", []),
        "main_client": tests.get("main_client"),
        "pve_mgmt": pve_mgmt, "vpcs": vpcs,
        "printer": {"backbone": bb_spec["node"], "sites": printer} if printer else {},
        "clouds": spec.get("clouds", {}), "frames": spec.get("frames", {}),
    }
    (CONFIG_DIR / "ext_topology.json").write_text(json.dumps(topo, indent=2) + "\n")

    print(f"{len(nodes)} Nodes, {len(links)} Links, {len(hosts)} Zielhosts, "
          f"{len(topo['devices'])} Ziele sind Geräteadressen, {len(probes)} Probes")
    for node in nodes:
        print(f"  {node}: {', '.join(f'{v}={k}' for k, v in json.loads((CONFIG_DIR / f'{node}.ports.json').read_text()).items())}")
    if unplaced:
        print("Nicht platzierbare NAT-Ziele (kein passendes Netz im Lab):")
        for u in unplaced:
            note = " — erwartet (expect_unreachable)" if u.split("@")[0] in topo["expect_unreachable"] else ""
            print(f"  {u}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
