#!/usr/bin/env python3
"""Import RouterOS network state from EVE-NG nodes into NetBox."""

import requests
import subprocess
import re
import json
import sys

NETBOX_URL = "http://localhost:8000/api"
NETBOX_TOKEN = "0123456789abcdef0123456789abcdef01234567"
HEADERS = {
    "Authorization": f"Token {NETBOX_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

NODES = {
    "192.168.56.11": {"name": "IT", "role": "switch"},
    "192.168.56.12": {"name": "SW_ILBS_2", "role": "switch"},
    "192.168.56.13": {"name": "SW_ILBS_1", "role": "switch"},
    "192.168.56.14": {"name": "RTR_ILBS_2", "role": "router"},
    "192.168.56.15": {"name": "RTR_ILBS_1", "role": "router"},
}


def nb_get(path, params=None):
    r = requests.get(f"{NETBOX_URL}{path}", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def nb_post(path, data):
    r = requests.post(f"{NETBOX_URL}{path}", headers=HEADERS, json=data)
    if r.status_code == 201:
        return r.json()
    if r.status_code == 400:
        # Might already exist
        err = r.json()
        print(f"  WARN {path}: {err}")
        return None
    r.raise_for_status()


def nb_get_or_create(path, data, lookup_params=None):
    """Get existing object or create new one."""
    if lookup_params is None:
        lookup_params = {"name": data.get("name")}
    label = lookup_params.get("name", str(lookup_params))
    existing = nb_get(path, lookup_params)
    if existing.get("count", 0) > 0:
        obj = existing["results"][0]
        print(f"  EXISTS: {path} -> {label} (id={obj['id']})")
        return obj
    obj = nb_post(path, data)
    if obj:
        print(f"  CREATED: {path} -> {label} (id={obj['id']})")
    return obj


def ssh_command(ip, cmd):
    """Run a command on a RouterOS node via SSH."""
    result = subprocess.run(
        [
            "sshpass", "-p", "admin",
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
            f"admin@{ip}", cmd,
        ],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout.strip()


def parse_interfaces(output):
    """Parse RouterOS /interface print detail output."""
    interfaces = []
    current = {}
    for line in output.split("\n"):
        line = line.strip()
        # New interface entry starts with a number
        match = re.match(r'^\d+\s+[A-Z ]*\s+name="([^"]+)"', line)
        if match:
            if current:
                interfaces.append(current)
            current = {"name": match.group(1)}
            # Extract type
            type_match = re.search(r'type="([^"]+)"', line)
            if type_match:
                current["type"] = type_match.group(1)
            mac_match = re.search(r'mac-address=([0-9A-Fa-f:]+)', line)
            if mac_match and mac_match.group(1) != "00:00:00:00:00:00":
                current["mac"] = mac_match.group(1)
            mtu_match = re.search(r'(?<!\w)mtu=(\d+)', line)
            if mtu_match:
                current["mtu"] = int(mtu_match.group(1))
            if " X " in line[:8] or "X" in line[:8]:
                current["enabled"] = False
            else:
                current["enabled"] = True
        elif current:
            mac_match = re.search(r'mac-address=([0-9A-Fa-f:]+)', line)
            if mac_match and mac_match.group(1) != "00:00:00:00:00:00":
                current["mac"] = mac_match.group(1)
            mtu_match = re.search(r'(?<!\w)mtu=(\d+)', line)
            if mtu_match and "mtu" not in current:
                current["mtu"] = int(mtu_match.group(1))
    if current:
        interfaces.append(current)
    return interfaces


def parse_ip_addresses(output):
    """Parse RouterOS /ip address print output."""
    addresses = []
    for line in output.split("\n"):
        match = re.match(
            r'\s*\d+\s*[I ]*\s*([\d.]+/\d+)\s+([\d.]+)\s+(\S+)', line
        )
        if match:
            addresses.append({
                "address": match.group(1),
                "network": match.group(2),
                "interface": match.group(3),
            })
    return addresses


def ros_type_to_netbox(ros_type):
    """Map RouterOS interface type to NetBox interface type."""
    mapping = {
        "ether": "1000base-t",
        "vlan": "virtual",
        "bridge": "bridge",
        "bond": "lag",
        "loopback": "virtual",
        "vrrp": "virtual",
    }
    return mapping.get(ros_type, "other")


def main():
    print("=" * 60)
    print("NetBox Import - RouterOS EVE-NG Nodes")
    print("=" * 60)

    # Step 1: Create foundational objects
    print("\n--- Creating foundational objects ---")

    site = nb_get_or_create("/dcim/sites/", {
        "name": "EVE-NG Lab",
        "slug": "eve-ng-lab",
        "status": "active",
    })

    manufacturer = nb_get_or_create("/dcim/manufacturers/", {
        "name": "MikroTik",
        "slug": "mikrotik",
    })

    device_type = nb_get_or_create("/dcim/device-types/", {
        "manufacturer": manufacturer["id"],
        "model": "Cloud Hosted Router",
        "slug": "chr",
    }, lookup_params={"slug": "chr"})

    router_role = nb_get_or_create("/dcim/device-roles/", {
        "name": "Router",
        "slug": "router",
        "color": "2196f3",
    })

    switch_role = nb_get_or_create("/dcim/device-roles/", {
        "name": "Switch",
        "slug": "switch",
        "color": "4caf50",
    })

    platform = nb_get_or_create("/dcim/platforms/", {
        "name": "RouterOS",
        "slug": "routeros",
        "manufacturer": manufacturer["id"],
    })

    # Step 2: Collect data from all nodes
    print("\n--- Collecting data from RouterOS nodes ---")
    node_data = {}
    for ip, info in NODES.items():
        print(f"\n  Querying {info['name']} ({ip})...")
        iface_output = ssh_command(ip, "/interface print detail without-paging")
        ip_output = ssh_command(ip, "/ip address print without-paging")
        identity = ssh_command(ip, "/system identity print")

        hostname = info["name"]
        id_match = re.search(r"name:\s+(\S+)", identity)
        if id_match:
            hostname = id_match.group(1)

        node_data[ip] = {
            "hostname": hostname,
            "role": info["role"],
            "interfaces": parse_interfaces(iface_output),
            "addresses": parse_ip_addresses(ip_output),
            "mgmt_ip": ip,
        }
        print(f"    Found {len(node_data[ip]['interfaces'])} interfaces, "
              f"{len(node_data[ip]['addresses'])} IPs")

    # Step 3: Create devices and interfaces in NetBox
    print("\n--- Creating devices in NetBox ---")
    for ip, data in node_data.items():
        hostname = data["hostname"]
        role = router_role if data["role"] == "router" else switch_role

        print(f"\n  Device: {hostname}")
        device = nb_get_or_create("/dcim/devices/", {
            "name": hostname,
            "device_type": device_type["id"],
            "role": role["id"],
            "platform": platform["id"],
            "site": site["id"],
            "status": "active",
        })
        if not device:
            print(f"    ERROR: Could not create device {hostname}")
            continue

        device_id = device["id"]

        # Create interfaces
        print(f"    Creating interfaces...")
        iface_map = {}
        for iface in data["interfaces"]:
            nb_type = ros_type_to_netbox(iface.get("type", "ether"))
            iface_data = {
                "device": device_id,
                "name": iface["name"],
                "type": nb_type,
                "enabled": iface.get("enabled", True),
            }
            if "mac" in iface:
                iface_data["mac_address"] = iface["mac"]
            if "mtu" in iface and iface["mtu"] != 65536:
                iface_data["mtu"] = iface["mtu"]

            nb_iface = nb_get_or_create(
                "/dcim/interfaces/",
                iface_data,
                lookup_params={"device_id": device_id, "name": iface["name"]},
            )
            if nb_iface:
                iface_map[iface["name"]] = nb_iface["id"]

        # Create IP addresses
        print(f"    Creating IP addresses...")
        primary_ip4_id = None
        for addr in data["addresses"]:
            iface_id = iface_map.get(addr["interface"])
            if not iface_id:
                print(f"      SKIP: Interface {addr['interface']} not found")
                continue

            ip_data = {
                "address": addr["address"],
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": iface_id,
                "status": "active",
            }
            nb_ip = nb_get_or_create(
                "/ipam/ip-addresses/",
                ip_data,
                lookup_params={"address": addr["address"]},
            )

            # Set management IP as primary
            if addr["address"].startswith(data["mgmt_ip"]):
                if nb_ip:
                    primary_ip4_id = nb_ip["id"]

        # Set primary IP on device
        if primary_ip4_id:
            r = requests.patch(
                f"{NETBOX_URL}/dcim/devices/{device_id}/",
                headers=HEADERS,
                json={"primary_ip4": primary_ip4_id},
            )
            if r.status_code == 200:
                print(f"    Set primary IP: {data['mgmt_ip']}")
            else:
                print(f"    WARN: Could not set primary IP: {r.text[:200]}")

    print("\n" + "=" * 60)
    print("Import complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
