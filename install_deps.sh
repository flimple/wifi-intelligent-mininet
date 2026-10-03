#!/bin/bash
sudo apt update
# numpy meta data gen error fix
sudo apt install python3-dev gfortran build-essential
sudo apt install -y git python3-pip openvswitch-switch
# Check if the mininet directory exists, if not install it
if [ ! -d "src/top/libs/mininet-wifi" ]; then
    # Official mininet repo link
    git clone https://github.com/intrig-unicamp/mininet-wifi.git
    mv mininet-wifi src/top/libs/mininet-wifi
fi
# Check the compilation of mininet
sudo src/top/libs/mininet-wifi/util/install.sh -Wln