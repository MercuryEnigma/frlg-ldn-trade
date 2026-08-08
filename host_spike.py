#!/usr/bin/env python3
"""FRLG HOST spike - the earliest hardware checkpoint for Linux acting as the LDN host.

This does NOT complete a trade or give a gift. It only stands up an LDN network in the host role
and waits, so we can test either Direct Corner trade discovery or Mystery Gift friend discovery
before building the game protocol on top:

  HW-0  Can this Wi-Fi card AP-host at all?  -> `start()` returns without error / prints "AP up".
        (If the card cannot do the AP + monitor interface combination on one radio, it fails here,
         and we learn the whole approach needs a different radio - the cheapest possible failure.)

  HW-A  Does the console list/connect to us? Select the desired mode with --flow. If the beacon
        needs tuning, iterate frlgsim/beacon.py (or pass --beacon-hex from a matching real host).

  (a step past HW-A) Does the console CONNECT?  -> a "*** CONSOLE JOINED ***" line appears.

Setup is the same as a live trade: run as root with the LDN radio free (NetworkManager not managing
the LDN vifs - see the project notes), and pick the Wi-Fi phy with --phy.

    sudo ./.venv/bin/python host_spike.py --flow trade --phy phy0 --ot EMU
    sudo ./.venv/bin/python host_spike.py --flow trade --beacon-hex <captured-trade-host-hex>
    sudo ./.venv/bin/python host_spike.py --flow mystery-gift --phy phy0 --ot EMU

Ctrl-C to stop (tears down the LDN vifs).
"""

import argparse
import os
import sys
import time

from frlgsim import beacon
from frlgsim.transport import HostTransport, find_ap_phy, list_phys

VERSIONS = {"firered": beacon.VERSION_FIRE_RED, "leafgreen": 5}

# Captured verbatim from a real FireRed Direct Corner host. The Switch-side glue's RFU record
# layout is not fully understood, so this is better ground truth for trade discovery than rewriting
# fields using the experimental Mystery Gift encoder.
CAPTURED_TRADE_BEACON = bytes.fromhex(
    "005c160058000000000000000000000000000000000101000000050143686173650000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000686c5a68656c76623476354358455a232323232368642323232323232323")


def _resolve_keys(path):
    """Resolve the prod.keys path, handling the sudo `~`-is-/root trap: under sudo the default
    `~/.switch/...` expands to /root, but the keys live in the INVOKING user's home. If the expanded
    path is missing and SUDO_USER is set, retry against that user's home. Returns the best path
    (existing if found, else the plain expansion so the loader raises a clear error)."""
    expanded = os.path.expanduser(path)
    if os.path.exists(expanded):
        return expanded
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and path.startswith("~"):
        try:
            import pwd
            home = pwd.getpwnam(sudo_user).pw_dir
            cand = os.path.join(home, path[2:]) if path.startswith("~/") else path.replace("~", home, 1)
            if os.path.exists(cand):
                return cand
        except (KeyError, ImportError):
            pass
    return expanded


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phy", default="auto", help="wifi phy to host on; 'auto' (default) picks the "
                    "first AP-capable phy (the adapter renumbers on reload/replug, e.g. phy3)")
    ap.add_argument("--keys", default="~/.switch/prod.keys", help="Switch prod.keys path")
    ap.add_argument("--ot", default="EMU", help="host in-game name shown in the beacon")
    ap.add_argument("--tid", default="0x2288", help="host trainer id (hex), beacon field")
    ap.add_argument("--version", choices=list(VERSIONS), default="firered")
    ap.add_argument("--flow", choices=("trade", "mystery-gift"), default="mystery-gift",
                    help="console flow to advertise (default: mystery-gift)")
    ap.add_argument("--channel", type=int, default=None, help="fix the Wi-Fi channel (default: auto)")
    ap.add_argument("--comm-id", default="", help="LDN local_communication_id (hex); default = the "
                    "captured FRLG-NSO id 0x01006fa0233f8000")
    ap.add_argument("--scene", type=int, default=None, help="LDN scene id; default = 22287 (the "
                    "captured trade scene; the MG-friend scene may differ)")
    ap.add_argument("--max-participants", type=int, default=None,
                    help="LDN participant limit (default: 6 for trade, 2 for mystery-gift)")
    ap.add_argument("--password", default="", help="LDN passphrase hex; default = emulator passphrase")
    ap.add_argument("--beacon-hex", default="", help="use this raw application_data verbatim "
                    "instead of synthesizing one; the capture must match --flow")
    ap.add_argument("--no-beacon", action="store_true", help="host with an EMPTY beacon (HW-0 only: "
                    "just prove the card can AP-host; the console will not list us)")
    ap.add_argument("--debug", action="store_true", help="enable the ldn library's own DEBUG logging "
                    "(auth parse failures, ignored frames, ...)")
    ap.add_argument("--trace", metavar="FILE", default="",
                    help="byte/action JSONL trace of the hosting path (advertisement bytes, auth "
                    "request/response hex, join events, data frames, UDP datagrams)")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="skip the iw-phy AP-mode preflight check (escape hatch)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        for name in ("ldn", "ldn.wlan"):
            logging.getLogger(name).setLevel(logging.DEBUG)

    if os.geteuid() != 0:
        ap.error("must run as root (LDN needs the raw radio); re-run with sudo")

    if args.phy == "auto":
        phy = find_ap_phy(log=print)
        if phy is None:
            print("[spike] no AP-capable phy found (need a driver that lists '* AP' in "
                  f"`iw phy <phy> info`). Present phys: {', '.join(list_phys()) or 'none'}")
            return 1
        args.phy = phy

    if args.beacon_hex:
        app_data = bytes.fromhex(args.beacon_hex.replace(" ", ""))
        src = "captured/--beacon-hex"
    elif args.no_beacon:
        app_data = b""
        src = "EMPTY (HW-0 only)"
    elif args.flow == "trade":
        app_data = CAPTURED_TRADE_BEACON
        src = "captured known-good trade host"
    else:
        app_data = beacon.build_beacon(
            trainer_id=int(args.tid, 16), name=args.ot, rfu_session_id=beacon.RFU_SERIAL_GAME,
            activity=beacon.ACTIVITY_WONDER_CARD, has_card=True,
            version=VERSIONS[args.version], nickname=args.ot)
        src = "synthesized first-cut mystery-gift (frlgsim.beacon)"

    max_participants = args.max_participants
    if max_participants is None:
        max_participants = 6 if args.flow == "trade" else 2

    password = bytes.fromhex(args.password) if args.password else None
    keys_path = _resolve_keys(args.keys)
    if not os.path.exists(keys_path):
        print(f"[spike] prod.keys not found at {keys_path!r} (from --keys {args.keys!r}).")
        print("[spike] Pass an absolute path, e.g. --keys /home/<you>/.switch/prod.keys "
              "(under sudo, ~ is /root).")
        return 2
    print(f"[spike] keys: {keys_path}")
    print(f"[spike] flow: {args.flow}")
    print(f"[spike] max participants: {max_participants}")
    print(f"[spike] beacon: {src} ({len(app_data)} B){': ' + app_data.hex() if app_data else ''}")

    tracer = None
    if args.trace:
        from frlgsim.ldntrace import Tracer
        tracer = Tracer(args.trace, log=print)
        print(f"[spike] tracing hosting bytes/actions -> {args.trace}")

    t = HostTransport(app_data=app_data, password=password, nickname=args.ot, keys_path=keys_path,
                      max_participants=max_participants, phyname=args.phy, channel=args.channel,
                      local_comm_id=int(args.comm_id, 16) if args.comm_id else None,
                      scene_id=args.scene, tracer=tracer, log=print)
    try:
        t.start(preflight=not args.skip_preflight)
    except Exception as e:
        msg = str(e)
        # The preflight's verdict is already the clear, single-line answer - print it plainly.
        if "cannot host" in msg:
            print(f"\n[spike] HW-0 FAILED (preflight): {msg}")
            if tracer:
                tracer.close()
            return 1
        # Distinguish setup errors (keys/deps) from a genuine radio/AP-mode failure so we don't
        # mislabel a config problem as "your card can't host".
        if "prod.keys" in msg or ("No such file" in msg and "keys" in msg.lower()):
            print(f"\n[spike] Could not load Switch keys (NOT a hardware failure):\n{e}")
            print("[spike] Pass --keys <absolute path to prod.keys>.")
            return 2
        if "missing dep" in msg or "ModuleNotFoundError" in msg or "ImportError" in msg:
            print(f"\n[spike] Missing a dependency (NOT a hardware failure):\n{e}")
            return 2
        radio = any(s in msg for s in ("nl80211", "EBUSY", "interface", "combination",
                                       "NL80211", "monitor", "AP", "not supported", "ENOTSUP"))
        print(f"\n[spike] could not host on {args.phy}:\n{e}")
        if radio:
            print("[spike] This looks like an interface/AP-mode error — the card may not support the "
                  "AP+monitor combination LDN hosting needs; the distributor approach would then need "
                  "a different radio. (HW-0 FAILED.)")
        else:
            print("[spike] Failure doesn't look radio-specific — check the traceback above before "
                  "concluding anything about AP-mode support.")
        return 1

    print("\n[spike] HW-0 PASSED: the card is hosting an LDN network.")
    if args.flow == "trade":
        print("[spike] Now on the console: Direct Corner -> Join Group (Linux is the Leader/host), "
              "select our entry, and watch")
    else:
        print("[spike] Now on the console: Mystery Gift -> Wonder Cards -> Friend (NOT Wireless "
              "Communication = the 0x7F7D distributor path we can't do), and watch")
    print("        this terminal. A '*** CONSOLE JOINED ***' line means we got past HW-A. Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[spike] stopping, tearing down LDN vifs...")
    finally:
        t.stop()
        if tracer:
            tracer.close()
            print(f"[spike] trace written: {args.trace} (counts: {tracer.counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
