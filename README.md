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
│   ├─ NetBox 4.5 + netbox-routing  http://localhost:8001   admin / admin
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
```

Topologie im Lab:

```
rtr-ilbs-01 ══ sw-ilbs-01 ══ LACP-PEER-LINK ══ sw-ilbs-02 ══ rtr-ilbs-02
                   └── it-fw
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
  `/tool sniffer`, die Kunden-Adresse auf `oob-ilbs`.
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

LACP läuft über die EVE-Bridges (verifiziert: Partner-ID am Router-Bond, VRRP
rtr-01 Master / rtr-02 Backup). Falls das nach einem EVE-Update nicht mehr
gilt: Configs mit `--bond-mode balance-xor` erzeugen, Lab mit `--recreate` neu.

### 5. NetBox + Semaphore

```bash
docker compose up -d --build
# NetBox-Dump einspielen (Skripte aus ansible-tam):
mkdir -p ../ansible-tam/backups/netbox-snapshots
gunzip -c ../ansible-tam/backups/netbox_pre-main-to-null_2026-06-09T121502Z.sql.gz \
  > ../ansible-tam/backups/netbox-snapshots/20260609_121502.sql
../ansible-tam/backups/netbox-restore.sh 20260609_121502.sql   # migriert beim Start

lab/setup_semaphore.py      # Projekt KLEIN-115, Repo, Inventory, Env, Templates
```

NetBox läuft als eigenes Image (`Dockerfile.netbox`) mit dem Plugin
**netbox-routing 0.4.3** — Quelle der statischen Routen für
`routeros_static_routes`. Der Dump vom 09.06. ist älter als das Plugin, die
Routen müssen neu nach NetBox (`ansible-tam/ansible/scripts/routeros_static_routes_to_netbox.py`).

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
