#!/usr/bin/env python3
"""Start the routed HTTP/2 and HTTP/3 lab and open the Mininet CLI."""

from mininet.cli import CLI
from mininet.log import info, setLogLevel
from mininet.net import Mininet


def create_network():
    setLogLevel("info")
    net = Mininet(controller=None, build=False)

    try:
        h1 = net.addHost("h1", ip=None)
        r1 = net.addHost("r1", ip=None)
        r2 = net.addHost("r2", ip=None)
        h2 = net.addHost("h2", ip=None)

        net.addLink(h1, r1, intfName1="h1-eth0", intfName2="r1-eth0")
        net.addLink(r1, r2, intfName1="r1-eth1", intfName2="r2-eth0")
        net.addLink(r2, h2, intfName1="r2-eth1", intfName2="h2-eth0")

        net.build()
        net.start()

        h1.setIP("10.0.1.10/24", intf="h1-eth0")
        r1.setIP("10.0.1.1/24", intf="r1-eth0")
        r1.setIP("10.0.12.1/30", intf="r1-eth1")
        r2.setIP("10.0.12.2/30", intf="r2-eth0")
        r2.setIP("10.0.2.1/24", intf="r2-eth1")
        h2.setIP("10.0.2.10/24", intf="h2-eth0")

        for router in (r1, r2):
            router.cmd("sysctl -w net.ipv4.ip_forward=1")

        h1.cmd("ip route replace default via 10.0.1.1")
        r1.cmd("ip route replace 10.0.2.0/24 via 10.0.12.2")
        r2.cmd("ip route replace 10.0.1.0/24 via 10.0.12.1")
        h2.cmd("ip route replace default via 10.0.2.1")

        return net
    except BaseException:
        net.stop()
        raise


def main():
    net = create_network()
    try:
        info("\n*** Topology: h1 -> r1 -> r2 -> h2\n")
        info("*** Check connectivity: h1 ping -c 3 10.0.2.10\n")
        info("*** Check routing: h1 traceroute -n 10.0.2.10\n")
        info("*** Enter exit to stop the network.\n\n")
        CLI(net)
    except KeyboardInterrupt:
        info("\n*** Stopping the network\n")
    finally:
        net.stop()


if __name__ == "__main__":
    main()
