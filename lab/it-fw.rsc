# Lab-only: simuliert die IT-Firewall (Uplink VLAN 1240) als Gegenstelle für
# rtr-ilbs-01/02. Kein Kundengerät, wird nicht von ansible-tam verwaltet.
#
#   ether1  Mgmt 10.0.2.105 (Bootstrap) + OoB-Netz der ILBS-Geräte
#           (172.18.118.0/24, beim Kunden direkt an der Projekte-Firewall)
#   ether2  -> sw-ilbs-01 sfp-sfpplus5 (Bond it-uplink-mlag)
#   ether3  -> moa-pc (VPC im Clientnetz IT, Stellvertreter für den
#           Entwicklerrechner)
/interface ethernet
set [ find default-name=ether2 ] name=sw-ilbs-01
set [ find default-name=ether3 ] name=moa-pc
/interface bonding
add lacp-rate=1sec mode=802.3ad name=ilbs-uplink slaves=sw-ilbs-01
/interface vlan
add comment="IT TransferNetz" interface=ilbs-uplink name=VLAN-1240 vlan-id=1240
# Stellvertreter für einen Host im IT-Servernetz (172.18.0.0/18) — Quelle/Ziel
# für NAT- und Routing-Tests.
/interface bridge
add name=lo-it
/ip address
add address=172.20.240.1/29 comment="Default IT Firewall" interface=VLAN-1240
add address=172.18.104.1/23 comment="Clientnetz IT (Gateway moa-pc)" interface=moa-pc
add address=172.18.118.1/24 comment="OoB ILBS" interface=ether1
add address=172.18.0.63/32 comment="IT-Server (PASCAL_TEST-Ziel)" interface=lo-it
/ip route
add comment="ILBS intern (VRRP VLAN-1240)" dst-address=172.18.128.0/17 gateway=172.20.240.6
add comment="ILBS Testanlagen (VRRP VLAN-1240)" dst-address=1.0.0.0/8 gateway=172.20.240.6
/system identity
set name=it-fw
