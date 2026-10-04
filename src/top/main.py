#!/usr/bin/env python3
from mn_wifi.net import Mininet_wifi
from mn_wifi.cli import CLI
from libs.custom_gui import WifiGUI

import json
import os

TOPOLOGY_FILE="data/topology.json"

def default_topology_test(net : Mininet_wifi):
    ap1 = net.addAccessPoint('ap1', ssid='lab', mode='g', channel='1',
                             position='30,50,0', range=40)
    ap2 = net.addAccessPoint('ap2', ssid='lab', mode='g', channel='6',
                             position='90,50,0', range=40)
    sta1 = net.addStation('sta1', ip='10.0.0.1/8', position='20,40,0')
    sta2 = net.addStation('sta2', ip='10.0.0.2/8', position='40,60,0')
    sta3 = net.addStation('sta3', ip='10.0.0.3/8', position='95,55,0')
    h1 = net.addHost('h1', ip='10.0.0.10/8', position='120,50,0')
    c0 = net.addController('c0')

    net.setPropagationModel(model='logDistance', exp=3)
    configure = getattr(net, 'configureNodes', None) or net.configureWifiNodes
    configure()

    net.addLink(ap1, ap2)      # wired backbone between the APs (dots!)
    net.addLink(ap2, h1)       # a wired host behind ap2

    net.build()
    c0.start()
    ap1.start([c0])
    ap2.start([c0])


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
            ap_list = [net.addAccessPoint(ap["name"], ssid=params["ssid"], mode=params["mode"], channel=params["channel"], position=params["position"], range=params["range"], failMode=params.get("failMode") or "secure"), data["start"][ap["name"]]]
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