# EVE-NG Network Management

Entwicklungs-Lab für [`ansible-tam`](https://github.com/cargobayUG/ansible-tam):
[EVE-NG](https://www.eve-ng.net/) (RouterOS-Emulation der Kundengeräte),
[NetBox](https://netbox.dev/) (Source of Truth) und
[Semaphore](https://semaphoreui.com/) (Ansible-UI).

Ziel: `ansible-tam` läuft **ohne Anpassungen** gegen dieses Lab — Inventory
`ansible/inventory/hosts_lab.yml`, die NetBox-Backup-Skripte und die Tests
erwarten genau die Adressen und Namen unten.

## Überblick

```
Host (Ubuntu, KVM/libvirt + Docker)
│
├─ Docker (Compose-Projekt eve-ng-management, Netz 10.250.0.0/24)
│   ├─ NetBox 4.6 + netbox-routing  http://localhost:8001   admin / admin
│   └─ Semaphore 2.19    http://localhost:3010   admin / admin
│
├─ eve-forwards (systemd-User-Unit, socat)
│   localhost:18728-18732 ──> 10.0.2.101-105:8728   RouterOS-API
│   localhost:2222        ──> 10.0.2.15:22          EVE-SSH (root / eve)
│   localhost:8080        ──> 10.0.2.15:80          EVE-Web-UI (admin / eve)
│
└─ libvirt-Netz eve-mgmt 10.0.2.0/24 (NAT, Bridge virbr-eve)
    └─ VM eve-ng 10.0.2.15 (EVE-NG CE 6.2, pnet0 = Cloud0)
        └─ Lab ilbs.unl, CHR 7.23.5, ether1 = oob-ilbs an Cloud0
             rtr-ilbs-01 10.0.2.101   rtr-ilbs-02 10.0.2.102
             sw-ilbs-01  10.0.2.103   sw-ilbs-02  10.0.2.104
             it-fw       10.0.2.105   (simuliert IT-Firewall, VLAN 1240)
             moa-pc      VPC, 172.18.104.10/23 (Entwicklerrechner im Clientnetz)
             Testanlagen 10.0.2.106-.120 (optional, siehe „Testanlagen und NAT-Tests“)
```

Topologie im Lab:

```
rtr-ilbs-01 ══ sw-ilbs-01 ══ LACP-PEER-LINK ══ sw-ilbs-02 ══ rtr-ilbs-02
                   └── it-fw ── moa-pc
```

RouterOS-Login auf allen Nodes: `admin` / `Mikrotik1!`

## Gerätekonfiguration

Die Lab-Geräte tragen den **Kundenstand** aus `ansible-tam/docs/export/*.txt`.
`lab/gen_lab_config.py` übersetzt die Exports für CHR:

- Nur physische Ports, auf die die Config verweist (Bond-Slave, Bridge-Port,
  ...), werden übernommen; sie heißen auf dem CHR wie beim Kunden
  (`ether2..N` per `name=` umbenannt). `ether1` = `oob-ilbs` = Mgmt.
- Entfernt: MLAG (CHR kann kein MLAG, Peer-Link = normaler Bond),
  Port-/Bond-MTU, `/interface ethernet switch`, `/system routerboard`,
  `/tool sniffer`.
- `oob-ilbs` trägt neben der Lab-Mgmt-IP (10.0.2.x) die Kunden-OoB-Adresse
  (172.18.118.221–224), damit der moa-pc die Geräte wie beim Kunden erreicht.
- Alles andere — VLANs, VRRP, VRFs, IPs, Bridge-VLANs, Interface-Listen,
  Mangle/NAT, Routen — bleibt textgleich.

Das Ergebnis (`lab/configs/`, nicht im Git — enthält Kundendaten) wird nach
jedem neuen Export neu erzeugt. `lab/it-fw.rsc` ist handgeschrieben.

## Ersteinrichtung

### 1. Pakete (sudo)

```bash
sudo apt install -y qemu-system-x86 qemu-utils libvirt-daemon-system virtinst virt-manager \
  docker.io docker-compose-v2 docker-buildx socat sshpass python3-venv python3-pip unzip
sudo usermod -aG docker,libvirt,kvm "$USER"    # danach neu anmelden
```

### 2. EVE-NG-VM

```bash
# ISO: https://www.eve-ng.net/index.php/download/ (Community 6.2.0-4, Ubuntu 22.04)
lab/create_eve_vm.sh ~/Downloads/lab/eve-ce-prod-6.2.0-4-full.iso
virt-manager    # Konsole der VM öffnen
```

In der Konsole (Tastatur bewusst English (US) lassen):

1. Ubuntu-Installer: English → Keyboard „Done“ → „Confirm destructive action“:
   Continue (betrifft nur die VM-Platte). Danach installiert cloud-init beim
   ersten Boot die EVE-Pakete (einige Minuten).
2. Login `root` / `eve` startet den EVE-Setup-Assistenten: Root-Passwort `eve`
   (zweimal), Hostname `eve-ng`, Domain beliebig, **dhcp** (→ 10.0.2.15), NTP
   leer, „direct connection“. Die VM startet neu.

Danach in der VM `expect` nachinstallieren (für den Konsolen-Bootstrap) und das
CHR-Image ablegen:

```bash
sshpass -p eve ssh root@10.0.2.15 'apt-get update && apt-get install -y expect'
unzip ~/Downloads/lab/chr-7.23.5.img.zip -d /tmp
qemu-img convert -O qcow2 /tmp/chr-7.23.5.img /tmp/hda.qcow2
sshpass -p eve ssh root@10.0.2.15 mkdir -p /opt/unetlab/addons/qemu/mikrotik-7.23.5
sshpass -p eve scp /tmp/hda.qcow2 root@10.0.2.15:/opt/unetlab/addons/qemu/mikrotik-7.23.5/
sshpass -p eve ssh root@10.0.2.15 /opt/unetlab/wrappers/unl_wrapper -a fixpermissions
```

### 3. Port-Forwards

```bash
mkdir -p ~/.config/systemd/user
ln -s ~/Repos/eve-ng-management/lab/systemd/eve-forwards.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now eve-forwards
loginctl enable-linger "$USER"
```

### 4. Lab aufbauen

```bash
lab/gen_lab_config.py       # Exports -> lab/configs/
lab/create_ilbs_lab.py      # Topologie, Start, Bootstrap, Config-Import
```

Das Skript ergänzt ein bestehendes Lab um fehlende Nodes und Links (neue
Einträge in `NODES`/`VPCS`/`LINKS`, Skript erneut starten); bestehende Nodes
behalten ihre Config.

LACP läuft über die EVE-Bridges (verifiziert: Partner-ID am Router-Bond, VRRP
rtr-01 Master / rtr-02 Backup). Falls das nach einem EVE-Update nicht mehr
gilt: Configs mit `--bond-mode balance-xor` erzeugen, Lab mit `--recreate` neu.

### 5. NetBox + Semaphore

```bash
docker compose up -d --build
lab/setup_semaphore.py      # Projekt KLEIN-115, Repo, Inventory, Env, Templates
```

NetBox läuft als eigenes Image (`Dockerfile.netbox`) in derselben Version wie
die Kunden-NetBox (4.6.5) mit dem Plugin **netbox-routing 0.4.3** — Quelle der
statischen Routen für `routeros_static_routes`.

**Daten** kommen aus der Kunden-NetBox, nicht als DB-Dump, sondern als
eingegrenzter API-Export (nur die ilbs-Geräte und was daran hängt):

1. `ansible-tam/ansible/scripts/dump_netbox_ilbs.py` auf dem Kunden-Rechner
   ausführen (nur lesend, nur Standardbibliothek, Python ≥ 3.8):
   `python3 dump_netbox_ilbs.py --url https://<kunden-netbox>` → eine Datei
   `netbox_ilbs_<zeitstempel>.json.gz`.
2. Datei nach `ansible-tam/backups/netbox-snapshots/` legen (gitignored —
   Kundendaten gehören nicht ins Git).
3. Ins Lab einspielen (schreibt nur auf localhost, `--purge` leert vorher alle
   importierten Objekttypen; Benutzer/Tokens bleiben):

   ```bash
   cd ../ansible-tam
   backups/netbox-snapshot.sh                                   # Rückweg
   python3 ansible/scripts/netbox_ilbs_import.py \
     backups/netbox-snapshots/netbox_ilbs_<zeitstempel>.json.gz --purge --dry-run
   python3 ansible/scripts/netbox_ilbs_import.py \
     backups/netbox-snapshots/netbox_ilbs_<zeitstempel>.json.gz --purge
   ```

IPs von VMs/Fremdgeräten (z.B. NAT-Backends) landen dabei ohne Zuordnung —
für die Reconciler ohne Belang. Ein vorhandener Snapshot lässt sich alternativ
mit `backups/netbox-restore.sh <datei>.sql` zurückspielen.

Semaphore klont `ansible-tam` per `file:///opt/repos/ansible-tam` (Bind-Mount
von `../ansible-tam`), Branch per `setup_semaphore.py --branch` — committete
Stände sind ohne Push testbar. Das Environment `lab-defaults` setzt
`ansible_python_interpreter={{ ansible_playbook_python }}`, sonst nimmt Ansible
für die `connection: local`-Plays das Container-Python ohne librouteros.

### 6. ansible-tam lokal

```bash
cd ~/Repos/ansible-tam
python3 -m venv .venv
.venv/bin/pip install ansible-core -r ansible/requirements.txt
.venv/bin/ansible-galaxy collection install -r collections/requirements.yml

source ~/Repos/eve-ng-management/lab/ansible-tam.env   # Interpreter, ROUTEROS_USER, NetBox
cd ansible && ansible-playbook -i inventory/hosts_lab.yml playbooks/pve_nat.yml
```

## Verkehr simulieren (moa-pc)

`moa-pc` steht für den Entwicklerrechner beim Kunden: ein EVE-VPC (nur `ping`
und `trace`) im Clientnetz IT hinter der it-fw. Die it-fw bildet beide
Kunden-Firewalls zugleich nach — Gateway Clientnetz (172.18.104.1) und
Projekte-Firewall mit Bein im OoB-Netz (172.18.118.1 auf `ether1`):

| Ziel | Weg |
|---|---|
| Geräte-OoB 172.18.118.221–224 | it-fw direkt (wie beim Kunden) |
| InBand-Mgmt 172.18.255.0/28, VLAN-Netze, PVE-Mgmt | it-fw → VRRP 172.20.240.6 (VLAN 1240) |
| Testanlagen über NAT-VIPs (172.18.251.x, 172.18.252.192/26) | it-fw → VRRP 172.20.240.6, dort pve_nat (siehe unten) |

Konsole: EVE-Web-UI → `ilbs.unl` → Klick auf `moa-pc`, z.B.
`ping 172.18.118.224`, `trace 172.18.255.1`. Die IP kommt aus der
Startup-Config des Nodes (`VPCS` in `create_ilbs_lab.py`).

Der Rückweg folgt den Routing-Tabellen der Geräte, die it-fw macht kein NAT.
Er hängt an der Default-Route von rtr-ilbs-01 (VRRP-Master; auch die Switches
antworten über 172.18.255.14). Die fehlt im Export vom 2026-10-05, ist beim
Kunden aber vorhanden (geprüft 2026-10-08) und im Lab nachgetragen. Nach einem
Neuaufbau aus diesem Export auf rtr-ilbs-01 wieder anlegen — oder neu
exportieren:

```
/ip route add distance=1 dst-address=0.0.0.0/0 gateway=172.20.240.1 routing-table=main
```

## Testanlagen und NAT-Tests

Hinter den ILBS-Routern lassen sich die Testanlagen nachbauen, damit die
`pve_nat`-Regeln und die VRFs Ende-zu-Ende testbar sind: Backbone (Aruba),
TNR-Paare je Standort (Cisco, VRRP), Stubs für Gegenstellen ohne Export und
die PVE-Knoten aus Netzsicht (Anschluss am Public-Bond, Mgmt-Adresse wie die
echten Knoten) — alles als kleine CHRs (256 MB, Mgmt 10.0.2.106–.120). Ein
PVE-Knoten hat eine VLAN-Bridge wie `vmbr0` mit VMs in den Testanlagen-VRFs:
je eine Probe-VRF (für das Testskript) und ein VPC zum Anklicken.

Namen, Adressen und Anschlusspunkte stehen in `lab/ext-lab.local.toml`
(gitignored, Kundendaten; Format: `lab/ext-lab.example.toml`). Daraus und aus
den Exports in `ansible-tam/docs/export` erzeugt `lab/gen_ext_config.py` die
Configs und `configs/ext_topology.json`:

- **Backbone und TNRs** übernehmen nur die L3-Sicht der Exports (VLANs, IPs,
  VRRP, statische Routen). Ein Site-Switch ersetzt Crosslink und Access-Ports.
  Routen mit Backup-Route bekommen `check-gateway=ping` — in EVE bleibt der
  Link eines gestoppten Nodes oben.
- **Zielhosts** sind die `nat_inside`-Adressen der pve_nat-VIPs aus der
  Lab-NetBox, platziert im passenden Standort-VLAN (je VLAN ein VRF mit
  Default-Route über die VRRP-Adresse) bzw. als /32 auf einem Stub.
  Ziele ohne passendes Netz meldet der Generator.
- **Drucker-LANs** (`printer` je Site, `printer_vlan` am Backbone): Das
  L2-VLAN ohne IP reicht der Backbone getaggt bis zum Router durch; jedes
  Drucker-LAN hängt per Access-Port daran (im Lab ein Backbone-Port je Site
  statt eines untagged Ports mit Verteil-Switch). So fängt der Router per ARP
  die Anwahl-Adressen nicht vorhandener Drucker (N:1-NAT auf einen Drucker).
- **Clients** sind Probe-VRFs auf den CHRs (per API automatisierbar,
  überstehen einen TNR-Ausfall), z.B. `/ping <ziel> vrf=<probe>`; für
  Handtests der moa-pc und die VM-VPCs.
- Jede generierte `.rsc` entfernt den DHCP-Client auf ether1 — nach einem
  Reset legt der CHR ihn wieder an, und dessen Default-Route über 10.0.2.1
  stünde per ECMP neben den Lab-Routen.

```bash
lab/gen_ext_config.py
lab/create_ilbs_lab.py          # ergänzt Nodes/Links, Bootstrap, Import
~/Repos/ansible-tam/.venv/bin/python lab/test_nat_paths.py
```

Im Canvas hängt jede Gruppe an einer eigenen Mgmt-Wolke (alle auf `pnet0`,
nur optisch getrennt) und steht in einem beschrifteten Rahmen;
`lab/create_ilbs_lab.py --relayout` setzt Positionen, Wolken und Rahmen neu
(Rahmen von Hand angepasst? `--relayout` überschreibt sie).

EVE CE verbindet laufende Nodes nicht mit neuen Links; `create_ilbs_lab.py`
hängt deren TAP-Interfaces direkt an die Link-Bridge (nach einem Neustart
übernimmt EVE das selbst).

`test_nat_paths.py` pingt jedes NAT-Paar vom passenden Client (VIP in main:
von der it-fw mit 172.18.104.1, wie der moa-pc) und prüft auf beiden Routern
den Zähler der managed dstnat-Regel und per conntrack das tatsächliche
Backend. Paare, deren Backend in einem Drucker-LAN liegt, prüft es als
Matrix: jeder druckende Host (`printer.client`) pingt den Drucker direkt und
jede Anwahl-Adresse; dazu der VRRP-Stand der TNRs auf den Drucker-LANs (sie
teilen sich ein L2). Danach VRF-Szenarien von den VMs und dem Stub im Testanlagen-VRF: eigenes Gateway
(muss antworten), ein main-VIP und ein main-Host (dürfen nicht antworten; ein
steigender dstnat-Zähler zeigt, dass der Hinweg trotzdem ins Ziel-VRF leakt).

**Test-Paare** (Namespace `ilbs-pve-nat-test`):

1. In der Lab-NetBox VIP mit Tag `ilbs-pve-nat-test` und `nat_inside` anlegen
   (den Tag legt `test_nat_paths.py --tag ilbs-pve-nat-test` bei Bedarf an).
2. `lab/gen_ext_config.py` und `lab/create_ilbs_lab.py --reimport <site-lan>`
   (setzt den Node zurück und spielt die neue Config ein).
3. `pve_nat.yml -e pve_nat_is_test=true -e pve_nat_tag=ilbs-pve-nat-test -e pve_nat_dry_run=false`
4. `lab/test_nat_paths.py --tag ilbs-pve-nat-test`
5. Aufräumen: Tag entfernen, Lauf mit `-e pve_nat_prune=true`.

## Starten / Stoppen

```bash
virsh -c qemu:///system start eve-ng          # startet automatisch (autostart)
# Lab-Nodes: EVE-Web-UI -> ilbs.unl -> Start all  (oder create_ilbs_lab.py erneut)
docker compose up -d
systemctl --user status eve-forwards
```

Herunterfahren in umgekehrter Reihenfolge; die CHRs behalten ihre Config.

## Prüfen

```bash
curl -s -H "Authorization: Token 0123456789abcdef0123456789abcdef01234567" \
  http://localhost:8001/api/status/ | python3 -m json.tool
for p in 18728 18729 18730 18731; do
  ~/Repos/ansible-tam/.venv/bin/python -c "import librouteros; a=librouteros.connect('localhost','admin','Mikrotik1!',port=$p); \
print($p, next(iter(a.path('system','identity')))['name'])"
done
```

## Ältere Inhalte

`ansible/`, `import_to_netbox.py` und `create_dhcp_relay_lab.py` stammen aus
dem ersten Lab (VirtualBox, Mgmt `192.168.56.0/24`, NetBox auf Port 8000) und
sind nicht an den neuen Aufbau angepasst.
