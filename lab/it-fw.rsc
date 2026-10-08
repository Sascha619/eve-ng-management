# Lab-only: simuliert die IT-Firewall (Uplink VLAN 1240) als Gegenstelle für
# rtr-ilbs-01/02. Kein Kundengerät, wird nicht von ansible-tam verwaltet.
#
#   ether1  Mgmt 10.0.2.105 (Bootstrap)
#   ether2  -> sw-ilbs-01 sfp-sfpplus5 (Bond it-uplink-mlag)
/interface ethernet
set [ find default-name=ether2 ] name=sw-ilbs-01
/interface bonding
add lacp-rate=1sec mode=802.3ad name=ilbs-uplink slaves=sw-ilbs-01
/interface vlan
add comment="IT TransferNetz" interface=ilbs-uplink name=VLAN-1240 vlan-id=1240
# Stellvertreter für Hosts in den IT-Netzen (Servernetz 172.18.0.0/18,
# Clientnetz 172.18.104.0/23) — Quelle/Ziel für NAT- und Routing-Tests.
/interface bridge
add name=lo-it
/ip address
add address=172.20.240.1/29 comment="Default IT Firewall" interface=VLAN-1240
add address=172.18.0.63/32 comment="IT-Server (PASCAL_TEST-Ziel)" interface=lo-it
add address=172.18.104.10/32 comment="IT-Client / Entwicklerrechner" interface=lo-it
/ip route
add comment="ILBS intern (VRRP VLAN-1240)" dst-address=172.18.128.0/17 gateway=172.20.240.6
add comment="ILBS Testanlagen (VRRP VLAN-1240)" dst-address=1.0.0.0/8 gateway=172.20.240.6
/system identity
set name=it-fw
