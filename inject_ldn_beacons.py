#!/usr/bin/env python3
"""Inject the ordinary hidden Wi-Fi beacons missing from an LDN AP.

Run this in a second terminal while host_spike.py is already hosting.  It does
not create the AP and it does not send the Nintendo LDN advertisement; it only
sends the standard 802.11 beacon that lets a console notice the AP's BSSID.
"""

import argparse
import socket
import struct
import time


RADIOTAP_HEADER = struct.pack("<BBHI", 0, 0, 8, 0)
BROADCAST = b"\xff" * 6

# These values mirror the AccessPoint beacon/probe settings in ldn/wlan.py.
SUPPORTED_RATES = bytes((0x82, 0x84, 0x8B, 0x96, 0x24, 0x30, 0x48, 0x6C))
RSN_PSK_CCMP = bytes.fromhex(
    "0100"          # RSN version 1
    "000fac04"      # group cipher: CCMP
    "0100"          # one pairwise cipher
    "000fac04"      # pairwise cipher: CCMP
    "0100"          # one authentication suite
    "000fac02"      # authentication: PSK
    "0c00"          # capabilities, matching ldn/wlan.py
)


def parse_mac(value: str) -> bytes:
    try:
        result = bytes.fromhex(value.replace(":", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid MAC address: {value}") from exc
    if len(result) != 6:
        raise argparse.ArgumentTypeError(f"invalid MAC address: {value}")
    return result


def read_interface_mac(interface: str) -> bytes:
    with open(f"/sys/class/net/{interface}/address", encoding="ascii") as stream:
        return parse_mac(stream.read().strip())


def element(element_id: int, data: bytes) -> bytes:
    if len(data) > 255:
        raise ValueError("information element is too long")
    return bytes((element_id, len(data))) + data


def build_beacon(
    bssid: bytes,
    channel: int,
    sequence: int,
    ssid_length: int,
    dtim_period: int,
) -> bytes:
    # 802.11 management header: beacon, broadcast destination, AP as SA/BSSID.
    header = struct.pack(
        "<HH6s6s6sH",
        0x0080,
        0,
        BROADCAST,
        bssid,
        bssid,
        (sequence & 0xFFF) << 4,
    )

    # Timestamp is microseconds, followed by 100-TU interval and the capability
    # flags used by ldn/wlan.py (ESS + privacy + short preamble/slot time).
    timestamp = (time.monotonic_ns() // 1_000) & 0xFFFFFFFFFFFFFFFF
    fixed = struct.pack("<QHH", timestamp, 100, 0x0511)

    # NL80211_HIDDEN_SSID_ZERO_CONTENTS means preserve the SSID's length but
    # replace its contents with zero bytes. LDN SSIDs are 32 bytes long.
    information_elements = b"".join((
        element(0, b"\x00" * ssid_length),             # hidden SSID
        element(1, SUPPORTED_RATES),                    # supported rates
        element(3, bytes((channel,))),                  # current channel
        element(5, bytes((0, dtim_period, 0, 0))),      # TIM / DTIM
        element(48, RSN_PSK_CCMP),                      # WPA2-PSK / CCMP
    ))
    return RADIOTAP_HEADER + header + fixed + information_elements


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inject hidden 802.11 beacons for an already-running LDN AP"
    )
    parser.add_argument("--monitor", default="ldn-mon",
                        help="monitor interface used for injection (default: ldn-mon)")
    parser.add_argument("--ap", default="ldn",
                        help="AP interface whose MAC becomes the BSSID (default: ldn)")
    parser.add_argument("--bssid", type=parse_mac,
                        help="override the BSSID instead of reading it from --ap")
    parser.add_argument("--channel", type=int, default=1,
                        choices=range(1, 15), metavar="1-14")
    parser.add_argument("--ssid-length", type=int, default=32,
                        choices=range(0, 33), metavar="0-32")
    parser.add_argument("--dtim-period", type=int, default=3,
                        choices=range(1, 256), metavar="1-255")
    args = parser.parse_args()

    bssid = args.bssid if args.bssid is not None else read_interface_mac(args.ap)
    bssid_text = ":".join(f"{part:02x}" for part in bssid)

    tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    tx.bind((args.monitor, 0))

    print(f"Injecting hidden beacons on {args.monitor}: BSSID={bssid_text}, "
          f"channel={args.channel}, interval=100 TU")
    print("Leave host_spike.py running. Now try Direct Corner -> Join Group.")
    print("Press Ctrl-C to stop this helper.")

    sequence = 0
    sent = 0
    deadline = time.monotonic()
    try:
        while True:
            tx.send(build_beacon(
                bssid, args.channel, sequence, args.ssid_length, args.dtim_period
            ))
            sequence = (sequence + 1) & 0xFFF
            sent += 1
            deadline += 0.1024  # 100 time units; one TU is 1.024 ms
            time.sleep(max(0.0, deadline - time.monotonic()))
    except KeyboardInterrupt:
        print(f"\nStopped after {sent} injected beacons.")
    finally:
        tx.close()


if __name__ == "__main__":
    main()