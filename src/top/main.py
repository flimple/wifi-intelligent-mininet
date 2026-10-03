#!/usr/bin/env python3
from mininet.log import setLogLevel, info
from mn_wifi.net import Mininet_wifi
from mn_wifi.cli import CLI


def topology():
    # 1. Create an empty wireless network
    net = Mininet_wifi()

    # 2. Add the nodes
    info("*** Adding station, access point and controller\n")
    sta1 = net.addStation('sta1', ip='10.0.0.1/8')
    ap1 = net.addAccessPoint('ap1', ssid='my-wifi', mode='g', channel='1')
    c0 = net.addController('c0')

    # 3. Set up the wireless interfaces
    info("*** Configuring Wi-Fi nodes\n")
    net.configureNodes()   # on older Mininet-WiFi versions: net.configureWifiNodes()

    # 4. Connect the station to the access point
    info("*** Associating sta1 with ap1\n")
    net.addLink(sta1, ap1)

    # 5. Start the network
    info("*** Starting network\n")
    net.build()
    c0.start()
    ap1.start([c0])

    # 6. Open the command line so you can play with it
    info("*** Running CLI (type 'exit' to quit)\n")
    CLI(net)

    # 7. Clean up when you leave the CLI
    info("*** Stopping network\n")
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')   # show the info messages above
    topology()