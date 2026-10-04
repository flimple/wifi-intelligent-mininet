#!/usr/bin/env python3
from mn_wifi.net import Mininet_wifi
from mn_wifi.cli import CLI

def topology():
    net = Mininet_wifi()

    c0 = net.addController('c0')

    # FIX 1: Absolutely no spaces in the position strings
    # FIX 2: Added an underscore to the SSID to prevent Linux iwconfig errors
    ap1 = net.addAccessPoint("ap-1", ssid="Wifi_Name", mode="g", channel="1", position="100,100,0", range=150)
    sta1 = net.addStation("vcp_1", ip="10.0.0.1/8", position="200,200,0")

    net.setPropagationModel(model="logDistance", exp=4.5)

    net.configureNodes()

    net.plotGraph(max_x=300, max_y=300)

    net.build()

    c0.start()
    ap1.start([c0])

    CLI(net)

    net.stop()

if __name__ == '__main__':
    topology()