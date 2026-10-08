#!/usr/bin/env python3
"""Semaphore-Projekt KLEIN-115 für das ILBS-Lab einrichten (idempotent).

Legt über die Semaphore-API an, was noch fehlt (Abgleich per Name):
  - Projekt KLEIN-115
  - Key "None" (lokales Repo braucht keinen Schlüssel)
  - Repository ansible-tam (file:///opt/repos/ansible-tam, siehe docker-compose.yml)
  - Inventory "KLEIN-115 Lab (EVE-NG)" -> ansible/inventory/hosts_lab.yml
  - Environment "lab-defaults" (Variablen + Secrets)
  - Templates mit Surveys laut den ansible-tam-READMEs

Bestehende Templates werden mit --update auf den hier definierten Stand gebracht.

Aufruf:
  ./setup_semaphore.py [--url http://localhost:3010] [--branch staging] [--update]
"""

import argparse
import http.cookiejar
import json
import sys
import urllib.request

PROJECT = "KLEIN-115"
REPO_NAME = "ansible-tam"
REPO_URL = "file:///opt/repos/ansible-tam"
INVENTORY_NAME = "KLEIN-115 Lab (EVE-NG)"
INVENTORY_FILE = "ansible/inventory/hosts_lab.yml"
ENV_NAME = "lab-defaults"

# Semaphore läuft im Repo-Root; ansible.cfg (roles_path) liegt unter ansible/.
ENV_VARS = {
    "ANSIBLE_CONFIG": "ansible/ansible.cfg",
    "NETBOX_URL": "http://netbox:8080",
    "ROUTEROS_USER": "admin",
    # localhost zeigt im Container auf den Container -> Forwards auf dem Host.
    "ROUTEROS_API_HOST": "host.docker.internal",
    "EVE_JUMP_HOST": "host.docker.internal",
    "EVE_JUMP_PORT": "2222",
}
ENV_SECRETS = {
    "NETBOX_TOKEN": "0123456789abcdef0123456789abcdef01234567",
    "ROUTEROS_PASSWORD": "Mikrotik1!",
    "EVE_JUMP_PASSWORD": "eve",
}
# Extra-Vars: Plays mit connection: local würden sonst /usr/bin/python3 des
# Containers nehmen — librouteros/netaddr liegen aber im Ansible-venv.
ENV_EXTRA_VARS = {
    "ansible_python_interpreter": "{{ ansible_playbook_python }}",
}


def mode(var, title="Modus"):
    return {"name": var, "title": title, "type": "enum", "required": True,
            "values": [{"name": "Plan only", "value": "true"}, {"name": "Apply", "value": "false"}],
            "default_value": "true"}


def flag(var, title, off, on):
    return {"name": var, "title": title, "type": "enum", "required": True,
            "values": [{"name": off, "value": "false"}, {"name": on, "value": "true"}],
            "default_value": "false"}


def text(var, title, description="", required=False, type_="", default=""):
    return {"name": var, "title": title, "description": description, "type": type_,
            "required": required, "default_value": default}


TEMPLATES = [
    {
        "name": "ILBS VLAN — Self-Service VLAN Provisioning",
        "playbook": "ansible/playbooks/vlan.yml",
        "description": "VLAN + Prefix + IPs in NetBox, VLAN/VRRP auf Switches und Routern",
        "survey": [
            text("vlan_id", "VLAN-ID", "1200–4094", required=True, type_="int"),
            text("vlan_subnet", "Subnetz (CIDR)", "z.B. 172.18.250.0/27; bei absent leer lassen"),
            text("vlan_tenant", "Tenant-Slug", "leer = ilbs-virtualisierungscluster"),
            text("vlan_netbox_vrf", "VRF", "leer = Global/main", default="main"),
            {"name": "vlan_state", "title": "Aktion", "type": "enum", "required": True,
             "values": [{"name": "present", "value": "present"}, {"name": "absent", "value": "absent"}],
             "default_value": "present"},
            mode("vlan_dry_run"),
        ],
    },
    {
        "name": "ILBS PVE NAT — Rollout / Prune",
        "playbook": "ansible/playbooks/pve_nat.yml",
        "description": "NAT-Paare aus NetBox (Tag ilbs-pve-nat) auf rtr-ilbs-01/02",
        "survey": [
            mode("pve_nat_dry_run"),
            flag("pve_nat_prune", "Aktion", "Apply", "Apply + Prune"),
            flag("pve_nat_is_test", "Namespace", "Prod", "Test (:test)"),
            text("pve_nat_tag", "NetBox-Tag", "Test-Lauf: ilbs-pve-nat-test", required=True,
                 default="ilbs-pve-nat"),
        ],
    },
    {
        "name": "RouterOS VLANs — Reconciler",
        "playbook": "ansible/playbooks/routeros_vlans_apply.yml",
        "description": "NetBox-VLANs (1200–4094) auf die Geräte bringen",
        "survey": [mode("rvl_dry_run"), flag("rvl_prune", "Aktion", "Apply", "Apply + Prune")],
    },
    {
        "name": "RouterOS Static Routes — Reconciler",
        "playbook": "ansible/playbooks/routeros_static_routes_apply.yml",
        "description": "Statische Routen aus NetBox auf die Router",
        "survey": [mode("rsr_dry_run"), flag("rsr_prune", "Aktion", "Apply", "Apply + Prune")],
    },
    {
        "name": "RouterOS Backup",
        "playbook": "ansible/playbooks/routeros_backup.yml",
        "description": "Binär-Backup + Export auf allen Geräten",
        "survey": [],
    },
]


class Semaphore:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call("POST", "/api/auth/login", {"auth": user, "password": password})

    def call(self, method, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.url + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path}: HTTP {e.code} {e.read()[:500]!r}") from None
        return json.loads(raw) if raw else None

    def ensure(self, list_path, name, payload, label):
        for obj in self.call("GET", list_path) or []:
            if obj["name"] == name:
                print(f"  {label} '{name}' vorhanden (id {obj['id']})")
                return obj
        obj = self.call("POST", list_path, payload)
        print(f"  {label} '{name}' angelegt (id {obj['id']})")
        return obj


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:3010")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--branch", default="staging", help="Branch von ansible-tam")
    ap.add_argument("--update", action="store_true", help="bestehende Templates überschreiben")
    args = ap.parse_args(argv)

    s = Semaphore(args.url, args.user, args.password)

    project = s.ensure("/api/projects", PROJECT, {"name": PROJECT}, "Projekt")
    pid = project["id"]
    base = f"/api/project/{pid}"

    key = s.ensure(f"{base}/keys", "None", {"name": "None", "type": "none", "project_id": pid}, "Key")
    repo = s.ensure(f"{base}/repositories", REPO_NAME, {
        "name": REPO_NAME, "project_id": pid, "git_url": REPO_URL,
        "git_branch": args.branch, "ssh_key_id": key["id"],
    }, "Repository")
    inventory = s.ensure(f"{base}/inventory", INVENTORY_NAME, {
        "name": INVENTORY_NAME, "project_id": pid, "type": "file", "inventory": INVENTORY_FILE,
        "ssh_key_id": key["id"], "repository_id": repo["id"],
    }, "Inventory")
    env_fields = {"name": ENV_NAME, "project_id": pid,
                  "json": json.dumps(ENV_EXTRA_VARS), "env": json.dumps(ENV_VARS)}
    env = s.ensure(f"{base}/environment", ENV_NAME, {
        **env_fields,
        "secrets": [{"type": "env", "name": k, "secret": v, "operation": "create"}
                    for k, v in ENV_SECRETS.items()],
    }, "Environment")
    if args.update:
        # Secrets bleiben unangetastet (ohne secrets-Liste kein Secret-Update).
        s.call("PUT", f"{base}/environment/{env['id']}", {**env_fields, "id": env["id"]})
        print(f"  Environment '{ENV_NAME}' aktualisiert (Variablen, Extra-Vars)")

    existing = {t["name"]: t for t in s.call("GET", f"{base}/templates") or []}
    for tpl in TEMPLATES:
        payload = {
            "project_id": pid, "name": tpl["name"], "playbook": tpl["playbook"],
            "description": tpl["description"], "app": "ansible",
            "inventory_id": inventory["id"], "repository_id": repo["id"],
            "environment_id": env["id"], "survey_vars": tpl["survey"],
        }
        if tpl["name"] in existing:
            if not args.update:
                print(f"  Template '{tpl['name']}' vorhanden — unverändert (--update zum Überschreiben)")
                continue
            tid = existing[tpl["name"]]["id"]
            s.call("PUT", f"{base}/templates/{tid}", {**payload, "id": tid})
            print(f"  Template '{tpl['name']}' aktualisiert (id {tid})")
        else:
            obj = s.call("POST", f"{base}/templates", payload)
            print(f"  Template '{tpl['name']}' angelegt (id {obj['id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
