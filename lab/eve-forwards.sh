#!/usr/bin/env bash
#
# Port-Forwards Host -> EVE-Mgmt-Netz (10.0.2.0/24), damit ansible-tam
# (hosts_lab.yml, Skripte) unverändert gegen localhost arbeitet.
#
# Gebunden an 127.0.0.1 und die Docker-Bridge (host.docker.internal für
# Semaphore) — bewusst nicht an 0.0.0.0, sonst wären Geräte und EVE im WLAN
# erreichbar. Läuft als systemd-User-Unit (systemd/eve-forwards.service).
#
set -euo pipefail

DOCKER_BRIDGE_IP="${DOCKER_BRIDGE_IP:-172.17.0.1}"

FORWARDS=(
  "18728 10.0.2.101:8728"   # rtr-ilbs-01 API
  "18729 10.0.2.102:8728"   # rtr-ilbs-02 API
  "18730 10.0.2.103:8728"   # sw-ilbs-01 API
  "18731 10.0.2.104:8728"   # sw-ilbs-02 API
  "18732 10.0.2.105:8728"   # it-fw API
  "2222 10.0.2.15:22"       # EVE-NG SSH (EVE_JUMP_HOST/PORT)
  "8080 10.0.2.15:80"       # EVE-NG Web-UI
)

BINDS=(127.0.0.1)
if ip -4 addr show 2>/dev/null | grep -q "inet ${DOCKER_BRIDGE_IP}/"; then
  BINDS+=("$DOCKER_BRIDGE_IP")
elif command -v docker >/dev/null; then
  # Docker installiert, Bridge noch nicht da (Boot) -> systemd startet neu.
  echo "Docker-Bridge ${DOCKER_BRIDGE_IP} noch nicht da — Neustart folgt" >&2
  exit 1
fi

trap 'kill 0' EXIT
for fwd in "${FORWARDS[@]}"; do
  read -r port target <<<"$fwd"
  for bind in "${BINDS[@]}"; do
    socat "TCP-LISTEN:${port},bind=${bind},reuseaddr,fork" "TCP:${target}" &
  done
done
wait -n
exit 1
