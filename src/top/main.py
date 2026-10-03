#!/usr/bin/env python3
from mininet.log import setLogLevel, info
from mn_wifi.net import Mininet_wifi
from mn_wifi.cli import CLI


def topology():
    net = Mininet_wifi()

    ap1 = net.addAccessPoint("acces_point-1", ssid="Wifi Name", mode="g", channel="1")
    sta1 = net.addStation("device 1", ip="10.0.0.1/8")

    net.configureNodes()

    net.addLink(ap1, sta1)

    net.build()
    #ap1.start()

    CLI(net)

    net.stop()


if __name__ == '__main__':
    setLogLevel('info')   # show the info messages above
    topology()