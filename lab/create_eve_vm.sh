#!/usr/bin/env bash
#
# Legt das libvirt-Netz eve-mgmt und die EVE-NG-VM an (qemu:///system).
# Braucht kein sudo, nur Mitgliedschaft in der Gruppe libvirt.
#
# Verwendung:
#   ./create_eve_vm.sh ~/Downloads/lab/eve-ce-prod-6.2.0-4-full.iso
#
# Danach die Installation in der VM-Konsole abschließen (virt-manager):
# Erstkonfig-Assistent -> IP per DHCP (bekommt 10.0.2.15), Root-Passwort "eve".
#
set -euo pipefail

ISO="${1:?Pfad zum EVE-NG-ISO angeben}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export LIBVIRT_DEFAULT_URI=qemu:///system

VM_NAME="eve-ng"
VM_MAC="52:54:00:e0:00:15"   # DHCP-Reservierung in libvirt/eve-mgmt.xml
VM_RAM_MB=32768
VM_VCPUS=8
VM_DISK_GB=200
POOL="default"
ISO_VOL="$(basename "$ISO")"

# --- Netz ---------------------------------------------------------------
if ! virsh net-info eve-mgmt >/dev/null 2>&1; then
  virsh net-define "$SCRIPT_DIR/libvirt/eve-mgmt.xml"
  virsh net-autostart eve-mgmt
fi
# Kein "| grep -q": grep beendet sich früh, virsh bekommt SIGPIPE -> pipefail.
[[ "$(virsh net-info eve-mgmt)" =~ Active:[[:space:]]+yes ]] || virsh net-start eve-mgmt

# --- Storage-Pool -------------------------------------------------------
if ! virsh pool-info "$POOL" >/dev/null 2>&1; then
  virsh pool-define-as "$POOL" dir --target /var/lib/libvirt/images
  virsh pool-build "$POOL"
  virsh pool-autostart "$POOL"
fi
[[ "$(virsh pool-info "$POOL")" =~ State:[[:space:]]+running ]] || virsh pool-start "$POOL"

# ISO in den Pool hochladen — qemu darf nicht in ~ lesen.
if ! virsh vol-info --pool "$POOL" "$ISO_VOL" >/dev/null 2>&1; then
  virsh vol-create-as "$POOL" "$ISO_VOL" "$(stat -c %s "$ISO")" --format raw
  virsh vol-upload --pool "$POOL" "$ISO_VOL" "$ISO"
fi

# --- VM -----------------------------------------------------------------
if virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
  echo "VM '$VM_NAME' existiert bereits — nichts zu tun."
  exit 0
fi

# host-passthrough: Nested-Virtualisierung für die CHR-Nodes in EVE.
# virtio-scsi statt virtio-blk: der EVE-Installer erwartet /dev/sda.
# hd,cdrom: leere Platte bootet nicht -> ISO; nach der Installation startet
# die Platte, statt dass der Installer erneut läuft.
virt-install \
  --name "$VM_NAME" \
  --memory "$VM_RAM_MB" \
  --vcpus "$VM_VCPUS" \
  --cpu host-passthrough \
  --osinfo linux2020 \
  --controller type=scsi,model=virtio-scsi \
  --disk "pool=$POOL,size=$VM_DISK_GB,format=qcow2,bus=scsi,discard=unmap" \
  --disk "vol=$POOL/$ISO_VOL,device=cdrom,bus=sata" \
  --network "network=eve-mgmt,mac=$VM_MAC,model=virtio" \
  --graphics spice \
  --boot hd,cdrom \
  --autostart \
  --noautoconsole

echo
echo "VM '$VM_NAME' gestartet. Installation in virt-manager abschließen."
