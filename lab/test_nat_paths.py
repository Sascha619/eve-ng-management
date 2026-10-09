#!/usr/bin/env python3
"""NAT-Paare aus NetBox im Lab Ende-zu-Ende prüfen.

Für jedes VIP -> Backend-Paar (Tag ilbs-pve-nat bzw. --tag):
  1. Client wählen: VIP in main -> it-fw mit der Gateway-Adresse des moa-pc-Netzes
     (der Router sieht dasselbe wie beim moa-pc); VIP im Testanlagen-VRF -> Probe
     im Standort-LAN.
  2. Paketzähler der managed dstnat-Regel(n) auf beiden Routern lesen.
  3. ping (3x) vom Client per RouterOS-API.
  4. Zähler erneut + conntrack (reply-src-address = tatsächliches Backend).

Drucker (N:1): Paare, deren Backend in einem Drucker-LAN liegt, laufen nicht über
die Tabelle, sondern als Matrix: jeder druckende Host (Probe "printer") -> Drucker
direkt und jede Anwahl-Adresse. Der Pfad geht über das Drucker-LAN des Ziels, in
dem rtr-ilbs-01 per ARP die Anwahl-Adressen fängt (printer_vlan über den Backbone).
Dazu der VRRP-Stand der TNRs auf den Drucker-LANs (alle hängen im selben L2).

Danach VRF-Szenarien von den VMs und dem Stub in den Testanlagen-VRFs: eigenes Gateway
(soll antworten), ein main-VIP (Hinweg leakt laut pve_nat-Design, Antwort
stirbt) und ein main-Host (soll unerreichbar sein).

Läuft mit dem Python des ansible-tam-venv (librouteros):
  ~/Repos/ansible-tam/.venv/bin/python lab/test_nat_paths.py [--tag ilbs-pve-nat-test] [--only VIP ...]
      [--skip-printer] [--skip-vrf]

Exit-Code 1 bei unerwarteten Ergebnissen.
"""

import argparse
import ipaddress
import json
import sys
import time
import urllib.error
import urllib.request

try:
    import librouteros
except ImportError:
    sys.exit("librouteros fehlt — mit ~/Repos/ansible-tam/.venv/bin/python starten.")

from create_ilbs_lab import CONFIG_DIR, NODES, ROS_PASSWORD, ROS_USER
from gen_ext_config import NETBOX_TOKEN, NETBOX_URL, nat_backends

ROUTERS = ("rtr-ilbs-01", "rtr-ilbs-02")
PREFIX = {"ilbs-pve-nat": "managed:ilbs-pve-nat", "ilbs-pve-nat-test": "managed:ilbs-pve-nat:test"}

_api = {}


def api(node: str):
    if node not in _api:
        _api[node] = librouteros.connect(NODES[node]["ip"], ROS_USER, ROS_PASSWORD, port=8728, timeout=20)
    return _api[node]


def ping(node: str, address: str, vrf: str = "main", src: str | None = None) -> dict:
    args = {"address": address, "count": "3", "vrf": vrf}
    if src:
        args["src-address"] = src
    res = list(api(node)("/ping", **args))
    last = res[-1] if res else {}
    ttls = [r["ttl"] for r in res if "ttl" in r]
    return {"received": int(last.get("received", 0)), "ttl": ttls[-1] if ttls else None}


def dstnat_counts(comment: str) -> dict[str, int]:
    """Paketzähler aller nat-Regeln mit genau diesem Comment, je Router."""
    out = {}
    for r in ROUTERS:
        rules = [x for x in api(r).path("ip", "firewall", "nat") if x.get("comment") == comment]
        out[r] = sum(int(x.get("packets", 0)) for x in rules if not x.get("disabled"))
    return out


def conntrack(dst: str, src: str) -> dict[str, str]:
    """reply-src-address der conntrack-Einträge src -> dst, je Router."""
    out = {}
    for r in ROUTERS:
        for c in api(r).path("ip", "firewall", "connection"):
            if c.get("dst-address", "").split(":")[0] == dst and c.get("src-address", "").split(":")[0] == src:
                out[r] = c.get("reply-src-address", "").split(":")[0]
    return out


def probe_vip(node: str, vrf: str, src: str, vip: str, comment: str) -> tuple[dict, dict, dict]:
    """Ping auf eine VIP: (Ping, dstnat-Delta je Router, conntrack-Backend je Router)."""
    before = dstnat_counts(comment)
    res = ping(node, vip, vrf, src)
    after = dstnat_counts(comment)
    delta = {r: after[r] - before[r] for r in ROUTERS if after[r] > before[r]}
    return res, delta, conntrack(vip, src)


def judge(res: dict, delta: dict, ct: dict, backend: str) -> tuple[str, bool]:
    """(Urteil, unerwartet) für ein Paar, dessen Backend im Lab steht."""
    ok_backend = bool(ct) and all(v == backend for v in ct.values())
    if res["received"] and ok_backend and delta:
        return "OK", False
    if res["received"] and ok_backend:
        return "Antwort, aber managed dstnat nicht genutzt (Handregeln?)", True
    if res["received"]:
        return "FALSCHES BACKEND", True
    if delta:
        return "dstnat greift, keine Antwort", True
    return "dstnat greift nicht", True


def in_nets(addr: str, nets: list) -> bool:
    return any(ipaddress.IPv4Address(addr) in n for n in nets)


def ensure_tag(url: str, token: str, tag: str):
    req = urllib.request.Request(f"{url}/api/extras/tags/?slug={tag}", headers={"Authorization": f"Token {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        if json.loads(resp.read())["count"]:
            return
    if not url.startswith(("http://localhost", "http://127.0.0.1")):
        sys.exit(f"Tag {tag} fehlt in {url} — lege ihn nur in der Lab-NetBox automatisch an.")
    req = urllib.request.Request(f"{url}/api/extras/tags/", method="POST",
                                 data=json.dumps({"name": tag, "slug": tag}).encode(),
                                 headers={"Authorization": f"Token {token}", "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30)
    print(f"Tag {tag} in der Lab-NetBox angelegt")


def short(r: str) -> str:
    return "r" + r[-1]


VRRP_MAC = "00:00:5E:00:01:{:02X}"


def printer_matrix(printer: dict, pairs: list[dict], probes: dict, prefix: str) -> int:
    """Druckende Hosts -> Drucker direkt und alle Anwahl-Adressen; VRRP der Drucker-LANs."""
    unexpected = 0
    clients = [pr for pr in probes.values() if pr.get("printer")]
    targets = [(b, None) for b in sorted({p["backend"] for p in pairs})]
    targets += [(p["vip"], p) for p in sorted(pairs, key=lambda x: tuple(int(o) for o in x["vip"].split(".")))]
    print("\nDrucker (N:1 über das Drucker-LAN des Ziels, rtr-01 fängt die Anwahl-Adressen)")
    print(f"  {'Client':<26} {'Ziel':<16} {'Ping':<5} {'TTL':<4} {'dstnat':<8} {'Backend lt. conntrack':<22} Ergebnis")
    for c in clients:
        for target, p in targets:
            if p is None:
                res, via, seen = ping(c["node"], target, c["vrf"], c["src"]), "-", "-"
                text, bad = ("OK (direkt, ohne NAT)", False) if res["received"] else ("Drucker direkt unerreichbar", True)
            else:
                res, delta, ct = probe_vip(c["node"], c["vrf"], c["src"], target, f"{prefix} {target}@{p['vip_vrf']} dstnat")
                via = ",".join(short(r) for r in delta) or "-"
                seen = ",".join(sorted(set(ct.values()))) or "-"
                text, bad = judge(res, delta, ct, p["backend"])
            unexpected += bad
            client = f"{c['src']} ({c['node']})"
            print(f"  {client:<26} {target:<16} {res['received']}/3   {str(res['ttl'] or '-'):<4} {via:<8} {seen:<22} {text}")

    # Alle Drucker-LANs hängen im selben L2: gleiche VRRP-Gruppe = gleiche virtuelle MAC.
    print("\n  VRRP auf den Drucker-LANs")
    groups: dict[int, list[str]] = {}
    for site, s in printer["sites"].items():
        for g in s["vrrp"]:
            groups.setdefault(g, []).append(site)
            masters = []
            for tnr in s["tnrs"]:
                for v in api(tnr).path("interface", "vrrp"):
                    if int(v.get("vrid", 0)) == g and v.get("master"):
                        masters.append(tnr)
            state = {0: "KEIN MASTER", 1: "OK"}.get(len(masters), "MEHRERE MASTER")
            unexpected += not masters
            print(f"    {site:<6} {s['net']:<18} Gruppe {g:<3} Master: {','.join(masters) or '-':<24} {state}")
    for g, sites in groups.items():
        if len(sites) > 1:
            mac = VRRP_MAC.format(g)
            seen = [f"{h.get('on-interface') or h.get('interface')}" for h in
                    api(printer["backbone"]).path("interface", "bridge", "host")
                    if h.get("mac-address", "").upper() == mac and h.get("bridge") == "printer"]
            print(f"    Befund: Gruppe {g} in {', '.join(sites)} im selben L2 -> virtuelle MAC {mac} mehrfach;"
                  f" {printer['backbone']} lernt sie an: {', '.join(seen) or '-'}")
    return unexpected


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", default="ilbs-pve-nat")
    ap.add_argument("--only", nargs="+", metavar="VIP", help="nur diese VIPs")
    ap.add_argument("--skip-printer", action="store_true", help="Drucker-Matrix überspringen")
    ap.add_argument("--skip-vrf", action="store_true", help="VRF-Szenarien überspringen")
    ap.add_argument("--netbox-url", default=NETBOX_URL)
    ap.add_argument("--netbox-token", default=NETBOX_TOKEN)
    args = ap.parse_args(argv)

    topo = json.loads((CONFIG_DIR / "ext_topology.json").read_text())
    probes, main_client = topo["probes"], topo["main_client"]
    in_lab = set(topo["hosts"]) | set(topo["devices"]) | set(topo["known_hosts"])
    if args.tag != "ilbs-pve-nat":
        ensure_tag(args.netbox_url, args.netbox_token, args.tag)
    pairs = nat_backends(args.netbox_url, args.netbox_token, [args.tag])
    if args.only:
        pairs = [p for p in pairs if p["vip"] in args.only]
    prefix = PREFIX.get(args.tag, f"managed:{args.tag}")

    printer = topo.get("printer") or {}
    printer_nets = [ipaddress.IPv4Network(s["net"]) for s in printer.get("sites", {}).values()]
    printer_pairs = [p for p in pairs if in_nets(p["backend"], printer_nets)]

    def client_for(p):
        if p["vip_vrf"] == "main":
            return "it-fw", "main", main_client["src"]
        for name, pr in probes.items():
            if pr.get("side") == p["vip_vrf"] and not pr.get("printer") and "gateway" not in pr:
                return pr["node"], pr["vrf"], pr["src"]
        return None

    unexpected = 0
    print(f"{'VIP@VRF':<24} {'Backend@VRF':<22} {'Client':<12} {'Ping':<5} {'TTL':<4} {'dstnat':<8} {'Backend lt. conntrack':<22} Ergebnis")
    for p in sorted(pairs, key=lambda x: tuple(int(o) for o in x["vip"].split("."))):
        if p in printer_pairs:
            continue
        vip, backend = f"{p['vip']}@{p['vip_vrf']}", f"{p['backend']}@{p['vrf']}"
        client = client_for(p)
        if client is None:
            print(f"{vip:<24} {backend:<22} {'-':<12} kein Client im Lab für VRF {p['vip_vrf']}")
            continue
        node, vrf, src = client
        res, delta, ct = probe_vip(node, vrf, src, p["vip"], f"{prefix} {p['vip']}@{p['vip_vrf']} dstnat")
        via = ",".join(short(r) for r in delta) or "-"
        seen = ",".join(sorted(set(ct.values()))) or "-"

        if p["backend"] in topo["expect_unreachable"]:
            text, bad = ("erwartet unerreichbar" if not res["received"] else "antwortet trotz expect_unreachable"), False
        elif p["backend"] not in in_lab:
            text, bad = "Backend nicht im Lab (gen_ext_config + --reimport)", False
        else:
            text, bad = judge(res, delta, ct, p["backend"])
        unexpected += bad
        print(f"{vip:<24} {backend:<22} {node:<12} {res['received']}/3   {str(res['ttl'] or '-'):<4} {via:<8} {seen:<22} {text}")

    if printer_pairs and not args.skip_printer:
        unexpected += printer_matrix(printer, printer_pairs, probes, prefix)

    if not args.skip_vrf:
        main_vip = next((p for p in sorted(pairs, key=lambda x: x["vip"])
                         if p["vip_vrf"] == "main" and p["backend"] in topo["hosts"]), None)
        print("\nVRF-Szenarien")
        for name, pr in probes.items():
            if "gateway" not in pr:
                continue
            checks = [("eigenes Gateway", pr["gateway"], True)]
            if main_vip:
                checks.append((f"main-VIP {main_vip['vip']}", main_vip["vip"], False))
            checks.append(("main-Host 172.18.0.63", "172.18.0.63", False))
            for label, target, want in checks:
                comment = f"{prefix} {target}@main dstnat"
                before = dstnat_counts(comment) if "VIP" in label else {}
                res = ping(pr["node"], target, pr["vrf"], pr["src"])
                after = dstnat_counts(comment) if "VIP" in label else {}
                leak = [short(r) for r in after if after[r] > before[r]]
                got = res["received"] > 0
                note = f" — Hinweg leakt per dstnat über {','.join(leak)}" if leak else ""
                verdict = ("OK" if got == want else "UNERWARTET") + note
                unexpected += got != want
                print(f"  {name:<11} ({pr['vrf']}, {pr['src']}) -> {label:<28} {res['received']}/3  {verdict}")
        for mgmt in topo.get("pve_mgmt") or []:
            res = ping("it-fw", mgmt, "main", main_client["src"])
            print(f"  {'moa-pc-Netz':<11} (main, {main_client['src']}) -> PVE-Mgmt {mgmt:<19} "
                  f"{res['received']}/3  {'OK' if res['received'] else 'UNERWARTET'}")
            unexpected += not res["received"]

    print(f"\n{unexpected} unerwartete Ergebnisse")
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
