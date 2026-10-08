#!/usr/bin/env python3
"""NAT-Paare aus NetBox im Lab Ende-zu-Ende prüfen.

Für jedes VIP -> Backend-Paar (Tag ilbs-pve-nat bzw. --tag):
  1. Client wählen: VIP in main -> it-fw mit der Gateway-Adresse des moa-pc-Netzes
     (der Router sieht dasselbe wie beim moa-pc); VIP im Testanlagen-VRF -> Probe
     im Standort-LAN, Drucker-Anwahladressen -> Drucker-Probe (Annahme).
  2. Paketzähler der managed dstnat-Regel(n) auf beiden Routern lesen.
  3. ping (3x) vom Client per RouterOS-API.
  4. Zähler erneut + conntrack (reply-src-address = tatsächliches Backend).

Danach VRF-Szenarien von den VMs und dem Stub in den Testanlagen-VRFs: eigenes Gateway
(soll antworten), ein main-VIP (Hinweg leakt laut pve_nat-Design, Antwort
stirbt) und ein main-Host (soll unerreichbar sein).

Läuft mit dem Python des ansible-tam-venv (librouteros):
  ~/Repos/ansible-tam/.venv/bin/python lab/test_nat_paths.py [--tag ilbs-pve-nat-test] [--only VIP ...]

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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", default="ilbs-pve-nat")
    ap.add_argument("--only", nargs="+", metavar="VIP", help="nur diese VIPs")
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

    def client_for(p):
        if p["vip_vrf"] == "main":
            return "it-fw", "main", main_client["src"], False
        for name, pr in probes.items():
            if pr.get("side") == p["vip_vrf"] and pr.get("assumption") and any(
                    ipaddress.IPv4Address(p["vip"]) in ipaddress.IPv4Network(n) for n in pr.get("reaches", [])):
                return pr["node"], pr["vrf"], pr["src"], True
        for name, pr in probes.items():
            if pr.get("side") == p["vip_vrf"] and not pr.get("assumption") and "gateway" not in pr:
                return pr["node"], pr["vrf"], pr["src"], False
        return None

    unexpected = 0
    print(f"{'VIP@VRF':<24} {'Backend@VRF':<22} {'Client':<12} {'Ping':<5} {'TTL':<4} {'dstnat':<8} {'Backend lt. conntrack':<22} Ergebnis")
    for p in sorted(pairs, key=lambda x: tuple(int(o) for o in x["vip"].split("."))):
        vip, backend = f"{p['vip']}@{p['vip_vrf']}", f"{p['backend']}@{p['vrf']}"
        client = client_for(p)
        if client is None:
            print(f"{vip:<24} {backend:<22} {'-':<12} kein Client im Lab für VRF {p['vip_vrf']}")
            continue
        node, vrf, src, assumption = client
        comment = f"{prefix} {p['vip']}@{p['vip_vrf']} dstnat"
        before = dstnat_counts(comment)
        res = ping(node, p["vip"], vrf, src)
        after = dstnat_counts(comment)
        ct = conntrack(p["vip"], src)
        delta = {r: after[r] - before[r] for r in ROUTERS if after[r] > before[r]}
        via = ",".join(short(r) for r in delta) or "-"
        seen = ",".join(sorted(set(ct.values()))) or "-"
        ok_backend = bool(ct) and all(v == p["backend"] for v in ct.values())

        if p["backend"] in topo["expect_unreachable"]:
            verdict, bad = ("erwartet unerreichbar" if not res["received"] else "antwortet trotz expect_unreachable"), False
        elif p["backend"] not in in_lab:
            verdict, bad = "Backend nicht im Lab (gen_ext_config + --reimport)", False
        elif res["received"] and ok_backend and delta:
            verdict, bad = "OK", False
        elif res["received"] and ok_backend:
            verdict, bad = "Antwort, aber managed dstnat nicht genutzt (Handregeln?)", True
        elif res["received"]:
            verdict, bad = "FALSCHES BACKEND", True
        elif delta:
            verdict, bad = "dstnat greift, keine Antwort", True
        else:
            verdict, bad = "dstnat greift nicht", True
        if assumption:
            verdict, bad = verdict + " [Annahme Drucker-Client]", False
        unexpected += bad
        print(f"{vip:<24} {backend:<22} {node:<12} {res['received']}/3   {str(res['ttl'] or '-'):<4} {via:<8} {seen:<22} {verdict}")

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
