#!/usr/bin/env python3
"""Kunden-Export (RouterOS /export) -> CHR-Lab-Konfiguration.

Liest die Exports aus ansible-tam/docs/export und erzeugt pro Gerät:

  configs/<device>.rsc         Konfiguration zum /import auf dem CHR
  configs/<device>.ports.json  Kunden-Portname -> CHR-Interface (etherN)

Prinzip: Alles, was die Playbooks anfassen (VLANs, VRRP, VRFs, IPs, Bridge-
VLANs, Interface-Listen, Mangle/NAT, Routen), bleibt 1:1 wie beim Kunden.
Angepasst wird nur, was es auf einem CHR nicht gibt:

- Physische Ports: Nur Ports, auf die außerhalb von /interface ethernet etwas
  verweist (Bond-Slave, Bridge-Port, Bridge-VLAN, ...), werden übernommen. Sie
  bekommen der Reihe nach ether2..N und per name= den Kundennamen — alle
  Verweise im Rest der Config bleiben dadurch textgleich gültig. ether1 ist
  immer das Mgmt-Interface (oob-ilbs).
- Hardware-Parameter (mtu/l2mtu/speed/... an Ports und Bonds,
  suppress-hw-offload an Routen) entfallen.
- MLAG (mlag-id, mlag-peer-port, mlag-priority) entfällt; der Peer-Link bleibt
  ein normaler Bond.
- /interface ethernet switch, /system routerboard settings, /tool sniffer
  entfallen; die oob-ilbs-Adresse setzt der Bootstrap (Lab-Mgmt-IP).
- Nach /ip vrf folgt ein :delay — die VRF-Routing-Tabellen entstehen
  asynchron, der Import liefe sonst in "input does not match any value of
  new-routing-mark".

Aufruf:
  ./gen_lab_config.py                      # alle Exports, Default-Pfade
  ./gen_lab_config.py --bond-mode balance-xor   # falls LACP im Lab nicht hochkommt
"""

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_EXPORT_DIR = Path(__file__).resolve().parents[2] / "ansible-tam" / "docs" / "export"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "configs"

MGMT_PORT = "ether1"
MGMT_NAME = "oob-ilbs"

DROP_SECTIONS = {
    "/interface ethernet switch",
    "/system routerboard settings",
    "/tool sniffer",
}

# Ethernet-Parameter, die auf dem CHR übernommen werden — der Rest ist Hardware.
KEEP_ETHERNET_PARAMS = ("name", "comment", "disabled")

# Physische Ports von CCR2004 / CRS326 in Frontpanel-Reihenfolge.
PORT_RE = re.compile(r"^(ether|sfp-sfpplus|sfp28-|qsfpplus)(\d+)(?:-(\d+))?$")

PARAM_RE = re.compile(r'([\w-]+)=("(?:[^"\\]|\\.)*"|\S*)')


def logical_lines(text: str) -> list[str]:
    """Export-Text -> Zeilen ohne CR, Fortsetzungen zusammengefügt, ohne Kommentare."""
    lines: list[str] = []
    buf = ""
    for raw in text.replace("\r", "").split("\n"):
        if buf:
            raw = raw.lstrip()
        if raw.endswith("\\"):
            buf += raw[:-1]
            continue
        line = buf + raw
        buf = ""
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        lines.append(line)
    return lines


def split_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    for line in lines:
        if line.startswith("/"):
            sections.append((line.strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            raise ValueError(f"Zeile vor erstem Abschnitt: {line!r}")
    return sections


def params(line: str) -> dict[str, str]:
    return {k: v for k, v in PARAM_RE.findall(line)}


def drop_params(line: str, names: tuple[str, ...]) -> str:
    for name in names:
        line = re.sub(rf"\s{re.escape(name)}=(?:\"(?:[^\"\\]|\\.)*\"|\S*)", "", line)
    return line


def port_sort_key(port: str) -> tuple:
    m = PORT_RE.match(port)
    if not m:
        return (99, port)
    order = {"ether": 0, "sfp-sfpplus": 1, "sfp28-": 2, "qsfpplus": 3}[m.group(1)]
    return (order, int(m.group(2)), int(m.group(3) or 0))


def referenced_tokens(sections: list[tuple[str, list[str]]]) -> set[str]:
    """Alle Werte-Tokens außerhalb von /interface ethernet (Komma-Listen aufgelöst)."""
    tokens: set[str] = set()
    for header, body in sections:
        if header == "/interface ethernet":
            continue
        for line in body:
            for _, value in PARAM_RE.findall(line):
                tokens.update(value.strip('"').split(","))
    return tokens


def convert(text: str, bond_mode: str | None) -> tuple[str, dict[str, str]]:
    sections = split_sections(logical_lines(text))
    refs = referenced_tokens(sections)

    # Physische Ports: default-name -> Kundenname (umbenannt oder default-name).
    ports: dict[str, dict[str, str]] = {}
    for header, body in sections:
        if header != "/interface ethernet":
            continue
        for line in body:
            m = re.search(r"\[ find default-name=(\S+) \]", line)
            if not m:
                raise ValueError(f"Unerwartete Ethernet-Zeile: {line!r}")
            ports[m.group(1)] = params(line.replace(m.group(0), ""))
    # Ports im Werkszustand haben keine set-Zeile, werden aber evtl. referenziert.
    for token in refs:
        if token != MGMT_PORT and token not in ports and PORT_RE.match(token):
            ports[token] = {}

    needed = sorted(
        (p for p, prm in ports.items() if p != MGMT_PORT and prm.get("name", p) in refs),
        key=port_sort_key,
    )
    port_map = {MGMT_NAME: MGMT_PORT}
    eth_lines = [f"set [ find default-name={MGMT_PORT} ] name={MGMT_NAME}"]
    for idx, port in enumerate(needed, start=2):
        prm = ports[port]
        name = prm.get("name", port)
        chr_port = f"ether{idx}"
        port_map[name] = chr_port
        kept = " ".join(f"{k}={prm[k]}" for k in KEEP_ETHERNET_PARAMS if k in prm and k != "name")
        eth_lines.append(f"set [ find default-name={chr_port} ] name={name}" + (f" {kept}" if kept else ""))

    out: list[str] = []
    for header, body in sections:
        if header in DROP_SECTIONS:
            continue
        if header == "/interface ethernet":
            out += [header, *eth_lines]
            continue
        new_body = []
        for line in body:
            if header == "/interface bonding":
                line = drop_params(line, ("mlag-id", "mtu"))
                if bond_mode:
                    line = re.sub(r"\smode=\S+", f" mode={bond_mode}", line)
                    line = drop_params(line, ("lacp-rate",))
            elif header == "/interface bridge":
                line = drop_params(line, ("mlag-peer-port", "mlag-priority"))
            elif header == "/interface ovpn-server server":
                line = drop_params(line, ("mac-address",))
            elif header == "/ip address" and params(line).get("interface") == MGMT_NAME:
                continue
            elif header == "/ip route":
                line = drop_params(line, ("suppress-hw-offload",))  # nur CRS-Hardware
            new_body.append(line)
        out += [header, *new_body]
        if header == "/ip vrf":
            # Die Routing-Tabelle eines VRFs entsteht asynchron; ohne Pause
            # scheitern spätere Verweise (new-routing-mark=<vrf>) im Import.
            out.append(":delay 3s")

    return "\n".join(out) + "\n", port_map


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--bond-mode", help="Bond-Modus im Lab überschreiben (z.B. balance-xor)")
    args = ap.parse_args(argv)

    exports = sorted(args.export_dir.glob("*.txt"))
    if not exports:
        sys.exit(f"Keine Exports (*.txt) in {args.export_dir}")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    for export in exports:
        device = export.stem
        rsc, port_map = convert(export.read_text(), args.bond_mode)
        header = (
            f"# Lab-Konfiguration für {device}, generiert von gen_lab_config.py\n"
            f"# aus {export.name} — nicht von Hand ändern, neu generieren.\n"
        )
        (args.out_dir / f"{device}.rsc").write_text(header + rsc)
        (args.out_dir / f"{device}.ports.json").write_text(json.dumps(port_map, indent=2) + "\n")
        print(f"{device}: {len(port_map)} Ports -> {', '.join(f'{v}={k}' for k, v in port_map.items())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
