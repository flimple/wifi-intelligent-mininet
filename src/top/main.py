#!/usr/bin/env python3
from mn_wifi.net import Mininet_wifi
from mn_wifi.cli import CLI
from libs.custom_gui import WifiGUI

def topology():
    net = Mininet_wifi()
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

    gui = WifiGUI(net)
    gui.start()

    CLI(net)

    gui.stop()
    net.stop()

if __name__ == '__main__':
    topology()