# EVE-NG Network Management

Integrierte Netzwerk-Management-Umgebung, die [EVE-NG](https://www.eve-ng.net/) (Netzwerk-Emulation), [NetBox](https://netbox.dev/) (Source of Truth) und [Ansible Semaphore](https://semaphoreui.com/) (Automation-UI) verbindet.

## Überblick

```
┌──────────────────────────────────────────────────────────────┐
│                        Host-System                           │
│                                                              │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐  │
│  │   NetBox     │   │  Semaphore   │   │   EVE-NG VM      │  │
│  │  (Docker)    │──▶│  (Docker)    │──▶│  (VirtualBox)    │  │
│  │  Port 8000   │   │  Port 3010   │   │  Port 8080       │  │
│  │              │   │              │   │                   │  │
│  │ Source of    │   │ Ansible UI   │   │ ┌──┐ ┌──┐ ┌──┐  │  │
│  │ Truth        │   │ + Runner     │   │ │R1│ │S1│ │S2│  │  │
│  └─────────────┘   └──────────────┘   │ └──┘ └──┘ └──┘  │  │
│                                        │    RouterOS       │  │
│                                        └──────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**Workflow:**
1. **NetBox** — Netzwerk dokumentieren (Devices, Interfaces, IPs, Kabel)
2. **Semaphore** — Ansible-Playbooks auslösen, die Konfiguration aus NetBox lesen
3. **EVE-NG** — RouterOS-Nodes werden automatisch konfiguriert

## Voraussetzungen

- Docker + Docker Compose
- VirtualBox mit EVE-NG VM (OVA)
- `sshpass` (für den NetBox-Import)
- Python 3 mit `requests` (für den NetBox-Import)

## Projektstruktur

```
eve-ng-management/
├── docker-compose.yml          # NetBox + Semaphore Services
├── netbox.env                  # NetBox Umgebungsvariablen
├── import_to_netbox.py         # Automatischer Import: RouterOS → NetBox
├── ansible/
│   ├── requirements.yml        # Ansible Collections (routeros, netbox)
│   ├── inventory/
│   │   └── netbox.yml          # NetBox Dynamic Inventory Plugin
│   └── playbooks/
│       ├── routeros_basic.yml           # System-Info + Interfaces abfragen
│       └── routeros_configure_ips.yml   # IPs aus NetBox auf Router anwenden
└── README.md
```

## Schnellstart

### 1. Services starten

```bash
cd ~/eve-ng-management
docker compose up -d
```

Warten bis alle Container healthy sind:

```bash
docker compose ps
```

### 2. Web-UIs aufrufen

| Service      | URL                        | Login              |
|-------------|----------------------------|-------------------|
| **NetBox**   | http://localhost:8000       | `admin` / `admin` |
| **Semaphore**| http://localhost:3010       | `admin` / `admin` |
| **EVE-NG**   | http://localhost:8080       | `admin` / `eve`   |

### 3. RouterOS-Nodes in NetBox importieren

Voraussetzung: EVE-NG VM läuft und die RouterOS-Nodes sind gestartet und haben Management-IPs im Netz `192.168.56.0/24` auf `ether8`.

```bash
python3 import_to_netbox.py
```

Das Skript:
- Verbindet sich per SSH zu allen 5 RouterOS-Nodes
- Liest Interfaces, IPs und Hostnamen aus
- Erstellt in NetBox: Site, Manufacturer, Device Type, Roles, Platform
- Importiert alle Devices mit Interfaces und IP-Adressen
- Setzt die Management-IP als Primary IP

## Netzwerk-Architektur

### EVE-NG VM (VirtualBox)

| Adapter | Bridge     | Netz                  | Zweck              |
|---------|------------|----------------------|-------------------|
| eth0    | pnet0      | NAT (10.0.2.x)       | Internet-Zugang    |
| eth1    | pnet1      | 192.168.56.0/24       | Management-Netz    |

### RouterOS-Nodes

| Node         | Rolle   | Management-IP     | Management-Interface |
|-------------|---------|------------------|---------------------|
| IT           | Switch  | 192.168.56.11    | ether8              |
| SW_ILBS_2    | Switch  | 192.168.56.12    | ether8              |
| SW_ILBS_1    | Switch  | 192.168.56.13    | ether8              |
| RTR_ILBS_2   | Router  | 192.168.56.14    | ether8              |
| RTR_ILBS_1   | Router  | 192.168.56.15    | ether8              |

**SSH-Zugang zu allen Nodes:** `admin` / `admin`

### Netzwerk-Konnektivität

```
Semaphore (Docker) ──▶ 192.168.56.x ──▶ EVE-NG pnet1 Bridge ──▶ RouterOS ether8
                   (Host-Only vboxnet0)
```

Semaphore erreicht die RouterOS-Nodes direkt über das Host-Only-Netzwerk (`vboxnet0` / `192.168.56.0/24`), das als Management-Netzwerk in EVE-NG konfiguriert ist (Cloud1 / pnet1).

## Docker-Services

### NetBox (Source of Truth)

| Container              | Funktion                        |
|-----------------------|--------------------------------|
| `netbox`               | Web-UI + REST API (Port 8000)   |
| `netbox-worker`        | Background Job Processing       |
| `netbox-housekeeping`  | Datenbank-Wartung               |
| `postgres`             | PostgreSQL für NetBox           |
| `redis`                | Redis (Queue)                   |
| `redis-cache`          | Redis (Cache)                   |

**API-Token:** `0123456789abcdef0123456789abcdef01234567`

### Semaphore (Ansible UI)

| Container       | Funktion                      |
|----------------|------------------------------|
| `semaphore`     | Web-UI + Ansible Runner (Port 3010) |
| `semaphore-db`  | PostgreSQL für Semaphore      |

Das Verzeichnis `./ansible` wird als `/opt/semaphore/playbooks` in den Semaphore-Container gemountet.

## Ansible-Playbooks

### Neues Playbook erstellen

1. **Datei erstellen** in `ansible/playbooks/`:

```yaml
---
- name: Mein Playbook
  hosts: all
  gather_facts: false
  connection: ansible.netcommon.network_cli
  vars:
    ansible_network_os: community.routeros.routeros
    ansible_user: admin
    ansible_password: admin

  tasks:
    - name: Beispiel-Task
      community.routeros.command:
        commands:
          - /system identity print
      register: result

    - name: Output anzeigen
      ansible.builtin.debug:
        var: result.stdout_lines
```

2. **Task Template in Semaphore anlegen:**
   - Semaphore UI → Project "EVE-NG Network" → Task Templates → + New Template
   - **Playbook Filename:** `playbooks/mein_playbook.yml` (relativ zum Repository-Root)
   - **Inventory:** EVE-NG RouterOS Nodes
   - **Repository:** Local Playbooks
   - **Environment:** EVE-NG Lab

3. **Ausführen:** Run ▶ klicken

### Vorhandene Playbooks

#### `routeros_basic.yml`
Liest von allen Nodes: System-Version, Hostname und Interface-Liste.

#### `routeros_configure_ips.yml`
Synchronisiert IP-Adressen von NetBox auf die RouterOS-Nodes:
1. Fragt die NetBox API ab: welche IPs sind welchem Device/Interface zugewiesen?
2. Vergleicht mit dem aktuellen Zustand auf dem Router
3. Fügt nur fehlende IPs hinzu (bestehende werden nicht angetastet)

**Typischer Workflow:**
1. In NetBox: Interface auswählen → IP-Adresse zuweisen
2. In Semaphore: "RouterOS Configure IPs" ausführen ▶
3. Die IP wird automatisch auf dem Router konfiguriert

### Ansible Collections

Definiert in `ansible/requirements.yml`:

| Collection              | Zweck                                 |
|------------------------|---------------------------------------|
| `community.routeros`    | RouterOS-Module (SSH/CLI-Kommandos)   |
| `netbox.netbox`         | NetBox Dynamic Inventory Plugin       |

## Semaphore-Konfiguration

Im Projekt "EVE-NG Network" sind folgende Objekte konfiguriert:

| Objekt         | Name                     | Details                              |
|---------------|--------------------------|--------------------------------------|
| **Credentials** | RouterOS SSH             | `admin` / `admin` (Login/Password)   |
| **Repository**  | Local Playbooks          | `/opt/semaphore/playbooks`           |
| **Inventory**   | EVE-NG RouterOS Nodes    | Statisch, alle 5 Nodes              |
| **Environment** | EVE-NG Lab               | Ansible network_cli Variablen        |

## EVE-NG Zugang

### SSH zur EVE-NG VM

```bash
ssh -p 2222 root@localhost    # Passwort: eve
```

### EVE-NG REST API

```bash
# Login
curl -s -c /tmp/eve-cookie -X POST -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"eve"}' \
  http://localhost:8080/api/auth/login

# Lab-Nodes auflisten
curl -s -b /tmp/eve-cookie http://localhost:8080/api/labs/test.unl/nodes
```

### SSH zu RouterOS-Nodes (vom Host)

```bash
ssh admin@192.168.56.11    # IT
ssh admin@192.168.56.12    # SW_ILBS_2
ssh admin@192.168.56.13    # SW_ILBS_1
ssh admin@192.168.56.14    # RTR_ILBS_2
ssh admin@192.168.56.15    # RTR_ILBS_1
```

## Troubleshooting

### Semaphore-Task schlägt fehl

- **"Unable to connect to port 22"** — RouterOS-Node ist nicht erreichbar. Prüfe ob die Node in EVE-NG läuft und die Management-IP konfiguriert ist.
- **"paramiko is not installed"** — Im Semaphore-Container ausführen:
  ```bash
  docker exec eve-ng-management-semaphore-1 sh -c \
    "/opt/semaphore/apps/ansible/11.1.0/venv/bin/pip install paramiko"
  ```

### EVE-NG VM nicht erreichbar

```bash
# VM-Status prüfen
VBoxManage list runningvms

# VM starten
VBoxManage startvm "EVE-NG" --type headless

# Port-Forwarding prüfen
VBoxManage showvminfo "EVE-NG" | grep "NIC"
```

### NetBox API testen

```bash
curl -s -H "Authorization: Token 0123456789abcdef0123456789abcdef01234567" \
  http://localhost:8000/api/dcim/devices/ | python3 -m json.tool
```

### Konnektivität prüfen (aus Semaphore-Container)

```bash
# Ping zu RouterOS-Node
docker exec eve-ng-management-semaphore-1 ping -c 1 192.168.56.11

# SSH-Test
docker exec eve-ng-management-semaphore-1 \
  sshpass -p 'admin' ssh -o StrictHostKeyChecking=no admin@192.168.56.11 \
  '/system identity print'

# NetBox API aus Semaphore
docker exec eve-ng-management-semaphore-1 \
  curl -s -H "Authorization: Token 0123456789abcdef0123456789abcdef01234567" \
  http://netbox:8080/api/status/
```
