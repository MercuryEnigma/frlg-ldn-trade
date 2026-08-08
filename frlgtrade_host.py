#!/usr/bin/env python3
"""frlgtrade_host - FireRed/LeafGreen trade simulator HOST over LDN.

This first host checkpoint creates the complete Linux LDN interface set and supplies the periodic
802.11 beacons required by the patched mt7601u driver.  A Switch can discover the Direct Corner
group and join it.  Pia/RFU leader admission and the trade state machine are intentionally the next
milestone; party/trade arguments already match frlgtrade.py so this entry point can grow in place.

LIVE (needs the Switch, root, and the same dependencies as frlgtrade.py):
    sudo -E python3 frlgtrade_host.py --live dummy.pk3 trademon.pk3

On the Switch choose Direct Corner -> Join Group.  Ctrl-C stops beacon injection, destroys the LDN
network, and removes its interfaces.
"""

import argparse
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from frlgsim import ldntrace, mon as monmod  # noqa: E402
from frlgsim import transport as tmod  # noqa: E402
from frlgtrade import _Log, parse_slots  # noqa: E402
from host_spike import CAPTURED_TRADE_BEACON, _resolve_keys  # noqa: E402
from inject_ldn_beacons import build_beacon, read_interface_mac  # noqa: E402


class BeaconInjector:
    """Managed form of inject_ldn_beacons.py's transmit loop."""

    def __init__(self, monitor="ldn-mon", ap="ldn", channel=1, ssid_length=32,
                 dtim_period=3, log=print):
        self.monitor = monitor
        self.ap = ap
        self.channel = channel
        self.ssid_length = ssid_length
        self.dtim_period = dtim_period
        self.log = log
        self.sent = 0
        self.error = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread = None

    def start(self, timeout=5):
        self._thread = threading.Thread(target=self._run, name="ldn-beacon-injector", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            raise RuntimeError("802.11 beacon injector did not start")
        if self.error is not None:
            raise RuntimeError(f"802.11 beacon injector failed: {self.error}")
        return self

    def _run(self):
        tx = None
        try:
            bssid = read_interface_mac(self.ap)
            tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
            tx.bind((self.monitor, 0))
            self.log(f"[host] injecting periodic 802.11 beacons on {self.monitor}: "
                     f"bssid={bssid.hex(':')} channel={self.channel} interval=100 TU")
            self._started.set()
            sequence = 0
            deadline = time.monotonic()
            while not self._stop.is_set():
                tx.send(build_beacon(bssid, self.channel, sequence, self.ssid_length,
                                     self.dtim_period))
                sequence = (sequence + 1) & 0xFFF
                self.sent += 1
                deadline += 0.1024
                self._stop.wait(max(0.0, deadline - time.monotonic()))
        except BaseException as exc:
            self.error = exc
            self._started.set()
        finally:
            if tx is not None:
                tx.close()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.log(f"[host] stopped 802.11 beacon injection after {self.sent} beacon(s)")


def validate_trade_inputs(args, lg):
    """Validate the future trade inputs now, without instantiating the follower-only TradeEngine."""
    party = [monmod.Mon.from_file(path) for path in args.party]
    if args.trades > len(party):
        raise SystemExit(f"--trades {args.trades} needs at least {args.trades} party mons; "
                         f"supplied {len(party)}")
    offered_slots = parse_slots(args.slots, args.trades, len(party))
    if offered_slots is None:
        if args.trades == 6:
            offered_slots = list(range(6))
        else:
            offered_slots = list(range(args.slot, args.slot + args.trades))
            if offered_slots and offered_slots[-1] >= len(party):
                raise SystemExit("default offered slots exceed the supplied party; use --slots")
    for i, pokemon in enumerate(party):
        lg(f"  party slot {i}: {pokemon.describe()}")
    lg.info(f"Loaded {len(party)} party Pokémon; host trade engine pending "
            f"(planned offered slots: {offered_slots}).")
    return party, offered_slots


def run_live(args, lg):
    validate_trade_inputs(args, lg)
    keys_path = _resolve_keys(args.keys)
    if not os.path.exists(keys_path):
        raise SystemExit(f"prod.keys not found at {keys_path!r}; pass --keys with an absolute path")

    phy = args.phy
    if phy == "auto":
        phy = tmod.find_ap_phy(log=lg)
        if phy is None:
            raise SystemExit(f"no AP-capable phy found; present phys: "
                             f"{', '.join(tmod.list_phys()) or 'none'}")

    tracer = ldntrace.Tracer(args.capture, log=lg) if args.capture else None
    password = bytes.fromhex(args.password) if args.password else None
    comm_id = int(args.comm_id, 16) if args.comm_id else None
    host = tmod.HostTransport(
        app_data=CAPTURED_TRADE_BEACON,
        password=password,
        nickname=args.ot,
        keys_path=keys_path,
        local_comm_id=comm_id,
        scene_id=args.scene,
        max_participants=args.max_participants,
        phyname=phy,
        channel=args.channel,
        tracer=tracer,
        log=lg,
    )
    injector = None
    joined = False
    try:
        host.start(preflight=not args.skip_preflight)
        injector = BeaconInjector(channel=args.channel, log=lg).start()
        lg.info("Hosting Direct Corner. On the Switch choose Join Group.")
        lg("[host] LDN discovery is active; waiting for a console JoinEvent. "
           "Pia/RFU trade hosting is not implemented in this checkpoint.")
        while True:
            if injector.error is not None:
                raise RuntimeError(f"802.11 beacon injector stopped: {injector.error}")
            if host.participants and not joined:
                joined = True
                lg.info("Switch joined the Linux LDN host successfully.")
                lg("[host] checkpoint reached: LDN participant registered. Keeping the network up; "
                   "Pia/RFU leader support is the next implementation stage. Ctrl-C to stop.")
            time.sleep(0.1)
    except KeyboardInterrupt:
        lg("[host] interrupted; shutting down")
    finally:
        if injector is not None:
            injector.stop()
        host.stop()
        if tracer is not None:
            tracer.close()
            lg(f"[host] trace written: {args.capture} (counts: {tracer.counts})")
    return joined


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("party", nargs="+", metavar="MON",
                    help="1..6 party .pk3/.ek3 files; validated now and used by the future host trade engine")
    ap.add_argument("-o", "--out", default="received.pk3",
                    help="received-mon output path (reserved until host trading is implemented)")
    ap.add_argument("--out-size", type=int, choices=(80, 100), default=100)
    ap.add_argument("--out-format", choices=("pk3", "ek3"), default="pk3")
    ap.add_argument("--slot", type=int, default=1)
    ap.add_argument("--slots", default="")
    ap.add_argument("--trades", type=int, default=1, choices=range(1, 7), metavar="N")
    ap.add_argument("--self-id", type=int, default=0, choices=(0,),
                    help="wire mpId (host/leader is always 0)")
    ap.add_argument("--ot", default="EMU", help="sim trainer / LDN nickname")
    ap.add_argument("--version", choices=("firered", "leafgreen"), default="leafgreen")
    ap.add_argument("--anim-delay", type=int, default=None)
    ap.add_argument("--decline", action="store_true")
    ap.add_argument("--refuse-illegit", action="store_true")
    ap.add_argument("--trust-pia", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--compress", action="store_true")
    ap.add_argument("--connect-id", "--parent-pid", dest="connect_id", default="")
    ap.add_argument("--verbose", action="store_true")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="host for a real Switch")
    mode.add_argument("--replay", metavar="CAPTURE",
                      help="reserved for a future host-role replay harness")
    ap.add_argument("--password", default="", help="LDN passphrase hex; default built-in emulator value")
    ap.add_argument("--phy", default="auto", help="Wi-Fi phy; default auto-selects an AP-capable phy")
    ap.add_argument("--keys", default="~/.switch/prod.keys")
    ap.add_argument("--comm-id", help="LDN local_communication_id hex; default known FRLG value")
    ap.add_argument("--capture", metavar="FILE",
                    help="record host LDN advertisement/data actions to JSONL")
    ap.add_argument("--channel", type=int, default=1, choices=range(1, 15), metavar="1-14")
    ap.add_argument("--scene", type=int, default=None,
                    help="LDN scene; default known FRLG Direct Corner scene 22287")
    ap.add_argument("--max-participants", type=int, default=6, choices=range(2, 9), metavar="2-8")
    ap.add_argument("--skip-preflight", action="store_true")
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()
    if not 1 <= len(args.party) <= 6:
        ap.error(f"supply 1..6 party mons; got {len(args.party)}")
    if args.replay:
        ap.error("host-role --replay is not implemented yet; use --live")
    lg = _Log(args.verbose)
    joined = run_live(args, lg)
    return 0 if joined else 130


if __name__ == "__main__":
    sys.exit(main())
