#!/usr/bin/env python3
from mn_wifi.net import Mininet_wifi
from mn_wifi.cli import CLI


def topology():
    net = Mininet_wifi()

    ap1 = net.addAccessPoint("ap_1", ssid="wifiname", mode="g", channel="1", position="100,100,0", range=300, failmode="standalone")
    sta1 = net.addStation("vcp_1", ip="10.0.0.1/8", position="200,200,0", range=100)

    net.configureNodes()
    net.addLink(sta1, ap1)
    
    net.plotGraph(max_x=300, max_y=300)
    net.build()

    ap1.start([])

    CLI(net)

    net.stop()


if __name__ == '__main__':
    topology()