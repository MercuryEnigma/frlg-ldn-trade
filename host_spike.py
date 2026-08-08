#!/usr/bin/env python3
"""Mystery Gift HOST spike - the earliest hardware checkpoint for the distributor work.

This does NOT trade or give a gift. It only stands up an LDN network in the distributor/host role
and waits, so we can answer two questions at the console BEFORE building the MG transport on top:

  HW-0  Can this Wi-Fi card AP-host at all?  -> `start()` returns without error / prints "AP up".
        (If the card cannot do the AP + monitor interface combination on one radio, it fails here,
         and we learn the whole approach needs a different radio - the cheapest possible failure.)

  HW-A  Does the console list us as a Mystery Gift friend?  -> on the console:
        Mystery Gift -> Receive Gift -> Wireless/Friend; watch for our entry. If the beacon needs
        tuning, iterate frlgsim/beacon.py (or pass --beacon-hex from a captured real host beacon).

  (a step past HW-A) Does the console CONNECT?  -> a "*** CONSOLE JOINED ***" line appears.

Setup is the same as a live trade: run as root with the LDN radio free (NetworkManager not managing
the LDN vifs - see the project notes), and pick the Wi-Fi phy with --phy.

    sudo ./.venv/bin/python host_spike.py --phy phy0 --ot EMU
    sudo ./.venv/bin/python host_spike.py --beacon-hex <captured-host-application_data-hex>

Ctrl-C to stop (tears down the LDN vifs).
"""

import argparse
import os
import sys
import time

from frlgsim import beacon
from frlgsim.transport import HostTransport

VERSIONS = {"firered": beacon.VERSION_FIRE_RED, "leafgreen": 5}


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
    ap.add_argument("--phy", default="phy0", help="wifi phy to host on (default phy0)")
    ap.add_argument("--keys", default="~/.switch/prod.keys", help="Switch prod.keys path")
    ap.add_argument("--ot", default="EMU", help="host in-game name shown in the beacon")
    ap.add_argument("--tid", default="0x2288", help="host trainer id (hex), beacon field")
    ap.add_argument("--version", choices=list(VERSIONS), default="firered")
    ap.add_argument("--channel", type=int, default=None, help="fix the Wi-Fi channel (default: auto)")
    ap.add_argument("--max-participants", type=int, default=2)
    ap.add_argument("--password", default="", help="LDN passphrase hex; default = emulator passphrase")
    ap.add_argument("--beacon-hex", default="", help="use this raw application_data verbatim "
                    "(e.g. a captured real host beacon) instead of the synthesized first-cut beacon")
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

    if args.beacon_hex:
        app_data = bytes.fromhex(args.beacon_hex.replace(" ", ""))
        src = "captured/--beacon-hex"
    elif args.no_beacon:
        app_data = b""
        src = "EMPTY (HW-0 only)"
    else:
        app_data = beacon.build_beacon(
            trainer_id=int(args.tid, 16), name=args.ot, rfu_session_id=beacon.RFU_SERIAL_GAME,
            activity=beacon.ACTIVITY_WONDER_CARD, has_card=True, version=VERSIONS[args.version])
        src = "synthesized first-cut (frlgsim.beacon)"

    password = bytes.fromhex(args.password) if args.password else None
    keys_path = _resolve_keys(args.keys)
    if not os.path.exists(keys_path):
        print(f"[spike] prod.keys not found at {keys_path!r} (from --keys {args.keys!r}).")
        print("[spike] Pass an absolute path, e.g. --keys /home/<you>/.switch/prod.keys "
              "(under sudo, ~ is /root).")
        return 2
    print(f"[spike] keys: {keys_path}")
    print(f"[spike] beacon: {src} ({len(app_data)} B){': ' + app_data.hex() if app_data else ''}")

    tracer = None
    if args.trace:
        from frlgsim.ldntrace import Tracer
        tracer = Tracer(args.trace, log=print)
        print(f"[spike] tracing hosting bytes/actions -> {args.trace}")

    t = HostTransport(app_data=app_data, password=password, nickname=args.ot, keys_path=keys_path,
                      max_participants=args.max_participants, phyname=args.phy, channel=args.channel,
                      tracer=tracer, log=print)
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
    print("[spike] Now on the console: Mystery Gift -> Receive Gift -> Wireless/Friend, and watch")
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
