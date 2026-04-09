#!/usr/bin/env python3
"""DHCP-Relay Lab – VPCS Client DHCP-Test.

Triggers DHCP on the VPCS client and verifies the lease.
The EVE-NG lab must already be running.

Topology:
  [DHCP-Client (VPCS)] ---(10.0.1.0/24)--- [Relay-Router] ---(10.0.2.0/24)--- [DHCP-Server]

- DHCP-Server (RouterOS): 10.0.2.1/24, mgmt 192.168.56.21/24
- Relay-Router (RouterOS): 10.0.1.1/24 + 10.0.2.2/24, mgmt 192.168.56.22/24
- DHCP-Client (VPCS): gets IP via DHCP from 10.0.1.0/24
"""

import subprocess

EVE_SSH_CMD = [
    "sshpass", "-p", "eve",
    "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
    "-p", "2222", "root@localhost",
]


# ── Helpers ──────────────────────────────────────────────────────────

def eve_ssh(cmd, timeout=30):
    result = subprocess.run(
        EVE_SSH_CMD + [cmd], capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout.strip(), result.stderr.strip()


def get_vpcs_port():
    """Find telnet port of the VPCS node."""
    out, _ = eve_ssh("ss -tlnp | grep vpcs | grep -oP '0.0.0.0:\\K[0-9]+'")
    return out.strip() if out else None


# ── VPCS expect script ──────────────────────────────────────────────

VPCS_EXPECT = r'''#!/usr/bin/expect -f
set timeout 30
set port [lindex $argv 0]
set cmds [lrange $argv 1 end]

spawn telnet 127.0.0.1 $port
sleep 1
send "\r"
sleep 1

foreach cmd $cmds {
    send "$cmd\r"
    if {$cmd eq "ip dhcp"} {
        sleep 12
    } else {
        sleep 2
    }
}

send "show ip\r"
sleep 2
puts "\n=== VPCS done ==="
close
'''


# ── DHCP-Client Test ────────────────────────────────────────────────

def test_dhcp_client():
    """Trigger DHCP on the VPCS client and show result."""
    print("\n=== DHCP auf VPCS-Client auslösen ===")

    port = get_vpcs_port()
    if not port:
        print("  FEHLER: VPCS Telnet-Port nicht gefunden. Läuft das Lab?")
        return False
    print(f"  VPCS Port: {port}")

    eve_ssh(f"cat > /tmp/cfg_vpcs.exp << 'EXPEOF'\n{VPCS_EXPECT}\nEXPEOF")
    eve_ssh("chmod +x /tmp/cfg_vpcs.exp")

    out, _ = eve_ssh(
        f'/tmp/cfg_vpcs.exp {port} "ip dhcp" "show ip"',
        timeout=60,
    )
    print(f"  {out[-300:]}" if out else "  (keine Ausgabe)")

    eve_ssh("rm -f /tmp/cfg_vpcs.exp")
    return True


# ── Main ────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("DHCP-Relay Lab – VPCS Client Test")
    print("=" * 60)
    test_dhcp_client()


if __name__ == "__main__":
    main()
