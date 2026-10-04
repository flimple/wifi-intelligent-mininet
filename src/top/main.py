#!/usr/bin/env python3
from mn_wifi.net import Mininet_wifi
from mn_wifi.cli import CLI
from libs.custom_gui import WifiGUI

import json
import os

TOPOLOGY_FILE="data/topology.json"

def default_topology_test(net : Mininet_wifi):
    c0 = net.addController("Routeur0", position="0,0,30")
    ap1 = net.addAccessPoint("AccessPoint1", ssid="Wifi_Name", mode="g", channel="1", position="10,10,0", range=60, failMode="secure")
    ap2 = net.addAccessPoint("AccessPoint2", ssid="Wifi_Name2", mode="g", channel="6", position="25,10,0", range=15, failMode="secure")
    ap3 = net.addAccessPoint("AccessPoint3", ssid="Wifi_Name3_5G", mode="a", channel="36", position="30,20,0", range=15, failMode="secure")
    sta1 = net.addStation("Device1", ip="10.0.0.1/8", position="5,5,0", range=10)
    sta2 = net.addStation("Device2", ip="10.0.0.2/8", position="20,20,0", range=10)

    net.configureNodes()

    net.addLink(ap3, sta2)

    net.build()
    c0.start()
    ap1.start([c0])
    ap2.start([c0])
    ap3.start([c0])

def json_topology(net : Mininet_wifi, json_file):
    acpts, stas=[], []
    ctrllrs = {}
    with open(json_file, "r") as f:
        data = json.load(f)
        stations = list(data["stations"])
        aps = list(data["accessPoints"])
        ctrls = list(data["controllers"])
        for sta in stations:
            params = sta["params"]
            stas.append(net.addStation(sta["name"], range=params["range"], ip=params["ip"], position=params["position"]))
        for ap in aps:
            params = ap["params"]
            ap_list = [net.addAccessPoint(ap["name"], ssid=params["ssid"], mode=params["mode"], channel=params["channel"], position=params["position"], range=params["range"], failMode="secured"if "failMode" not in params else params["failMode"]), data["start"][ap["name"]]]
            acpts.append(ap_list)
        for ctrl in ctrls:
            params = ctrl["params"]
            ctrllrs[ctrl["name"]] = net.addController(ctrl["name"], position=params["position"])
    net.configureNodes()

    net.build()
    for _, ctrlr in ctrllrs.items():
        ctrlr.start()
    for ap in acpts:
        ap[0].start([ctrllrs[f] for f in ap[1]])

def topology():
    net = Mininet_wifi()

    if os.path.exists(TOPOLOGY_FILE):
        json_topology(net, TOPOLOGY_FILE)
    else:
        default_topology_test(net)   

    gui = WifiGUI(net)
    if os.path.exists(TOPOLOGY_FILE):
        gui.load_json(TOPOLOGY_FILE)
    gui.start()

    CLI(net)

    gui.stop()
    net.stop()

if __name__ == '__main__':
    topology()