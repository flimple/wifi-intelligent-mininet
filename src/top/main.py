#!/usr/bin/env python3
from mn_wifi.net import Mininet_wifi
from mn_wifi.cli import CLI
from libs.custom_gui import WifiGUI

def topology():
    net = Mininet_wifi()
    c0 = net.addController("c0", position="50,50,30")
    ap1 = net.addAccessPoint("ap", ssid="Wifi_Name", mode="g", channel="1", position="100,100,0", range=300, failMode="secure")
    sta1 = net.addStation("sta1", ip="10.0.0.1/8", position="200,200,0", range=100)

    net.configureNodes()
    net.addLink(sta1, ap1)

    net.build()
    c0.start()
    ap1.start([c0])

    gui = WifiGUI(net)
    gui.start()

    CLI(net)

    gui.stop()
    net.stop()

if __name__ == '__main__':
    topology()