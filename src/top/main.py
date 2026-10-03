#!/usr/bin/env python3
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt

from mn_wifi.net import Mininet_wifi
from mn_wifi.cli import CLI


def topology():
    net = Mininet_wifi()

    ap1 = net.addAccessPoint("acces_point-1", ssid="Wifi Name", mode="g", channel="1")
    sta1 = net.addStation("device_1", ip="10.0.0.1/8")

    net.configureNodes()

    net.addLink(ap1, sta1)

    net.build()

    net.plotGraph(max_x=300, max_y=300)
    CLI(net)

    net.stop()


if __name__ == '__main__':
    topology()