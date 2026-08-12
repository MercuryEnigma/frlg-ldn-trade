"""Application runtime for hosting one complete FRLG trade session.

``HostApplication`` owns OS resources and scheduling.  Protocol bytes and peer
state live in :mod:`frlgsim.host_pia`; Reliable/RFU/trade state lives in
:class:`frlgsim.host_session.HostSession`.
"""

from dataclasses import dataclass
import os
import time

from . import host_session, host_trade, ldntrace, mon as monmod, transport
from .linkplayer import HOST_NAME_PAD
from .host_beacon import BeaconInjector, build_trade_app_data
from .host_pia import HostPeerProtocol
from .host_support import resolve_keys


HOST_CONTROL_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class HostRunConfig:
    party_paths: tuple
    output_path: str = "received.pk3"
    output_size: int = 100
    output_format: str = "pk3"
    trade_slot: int = 1
    offered_slots: tuple = ()
    trades: int = 1
    anim_delay: int | None = None
    trust_pia: bool = True
    password: bytes | None = None
    phy: str = "auto"
    keys_path: str = "~/.switch/prod.keys"
    local_comm_id: int | None = None
    capture_path: str | None = None
    channel: int = 1
    scene_id: int | None = None
    max_participants: int = 6
    skip_preflight: bool = False
    skip_encryption: bool = False
    native_nonce_sequence: bool = False
    session_response_first: bool = False

    def __post_init__(self):
        if not 1 <= len(self.party_paths) <= 6:
            raise ValueError("party must contain 1..6 Pokémon files")
        if not 1 <= self.trades <= 6:
            raise ValueError("trades must be 1..6")
        if self.trades > len(self.party_paths):
            raise ValueError("trades cannot exceed the configured party size")
        if self.output_size not in (80, 100):
            raise ValueError("output_size must be 80 or 100")
        if self.output_format not in ("pk3", "ek3"):
            raise ValueError("output_format must be pk3 or ek3")
        if len(self.offered_slots) != self.trades:
            raise ValueError("offered_slots must contain one slot per trade")
        if len(set(self.offered_slots)) != len(self.offered_slots):
            raise ValueError("offered_slots must be distinct")
        if any(type(slot) is not int or not 0 <= slot < len(self.party_paths)
               for slot in self.offered_slots):
            raise ValueError("offered_slots must reference the configured party")
        if type(self.trade_slot) is not int or not 0 <= self.trade_slot < len(self.party_paths):
            raise ValueError("trade_slot must reference the configured party")
        if self.anim_delay is not None \
                and (type(self.anim_delay) is not int or self.anim_delay < 0):
            raise ValueError("anim_delay must be a non-negative integer")
        if self.password is not None and not isinstance(self.password, bytes):
            raise ValueError("password must be bytes or None")
        if type(self.channel) is not int or not 1 <= self.channel <= 14:
            raise ValueError("channel must be 1..14")
        if type(self.max_participants) is not int or not 2 <= self.max_participants <= 8:
            raise ValueError("max_participants must be 2..8")
        if self.local_comm_id is not None and (
                type(self.local_comm_id) is not int
                or not 0 <= self.local_comm_id <= 0xFFFFFFFFFFFFFFFF):
            raise ValueError("local_comm_id must fit in 64 bits")
        if self.scene_id is not None and (
                type(self.scene_id) is not int or not 0 <= self.scene_id <= 0xFFFF):
            raise ValueError("scene_id must fit in 16 bits")


class HostApplication:
    """Wire together the host components and own their deterministic cleanup."""

    def __init__(self, config, profile, *, log=print,
                 transport_factory=transport.HostTransport,
                 injector_factory=BeaconInjector):
        self.config = config
        self.profile = profile
        self.log = log
        self.info = getattr(log, "info", log)
        self.transport_factory = transport_factory
        self.injector_factory = injector_factory
        self.network = None
        self.injector = None
        self.tracer = None
        self.session = None
        self.peer = None
        self._saved_commits = 0
        self._last_trade_state = None
        self._absence_logged = False

    def _load_party(self):
        party = [monmod.Mon.from_file(path) for path in self.config.party_paths]
        if self.config.trades > len(party):
            raise SystemExit(f"--trades {self.config.trades} needs at least "
                             f"{self.config.trades} party mons; supplied {len(party)}")
        for i, pokemon in enumerate(party):
            self.log(f"  party slot {i}: {pokemon.describe()}")
        self.info(f"Loaded {len(party)} party Pokémon "
                  f"(planned offered slots: {list(self.config.offered_slots)}).")
        return party

    def _resolve_phy_and_keys(self):
        phy = self.config.phy
        if phy == "auto":
            phy = transport.find_ap_phy(log=self.log)
            if phy is None:
                raise SystemExit("no AP-capable phy found; present phys: "
                                 f"{', '.join(transport.list_phys()) or 'none'}")
        keys = resolve_keys(self.config.keys_path)
        if not os.path.exists(keys):
            raise SystemExit(f"prod.keys not found at {keys!r}; pass --keys with an absolute path")
        return phy, keys

    def _build_components(self):
        party = self._load_party()
        phy, keys = self._resolve_phy_and_keys()
        link_player = self.profile.to_link_player()
        self.session = host_session.HostSession(
            party, trade_slot=self.config.trade_slot,
            offered_slots=list(self.config.offered_slots), trades=self.config.trades,
            link_player=link_player, anim_delay=self.config.anim_delay,
            trust_pia=self.config.trust_pia, log=self.log)
        inactive, active = build_trade_app_data(
            self.profile, self.session.rfu.host_session_id)
        self.tracer = (ldntrace.Tracer(self.config.capture_path, log=self.log)
                       if self.config.capture_path else None)
        self.network = self.transport_factory(
            app_data=inactive, password=self.config.password,
            nickname=self.profile.discovery_name, keys_path=keys,
            local_comm_id=self.config.local_comm_id,
            scene_id=self.config.scene_id,
            max_participants=self.config.max_participants,
            phyname=phy, channel=self.config.channel,
            skip_encryption=self.config.skip_encryption,
            tracer=self.tracer, log=self.log)
        self.peer = HostPeerProtocol(
            self.network, self.profile, self.session, active,
            native_nonce_sequence=self.config.native_nonce_sequence,
            session_response_first=self.config.session_response_first,
            log=self.log)
        self._last_trade_state = self.session.trade.state
        return link_player

    def _log_identity(self, link_player):
        wire = link_player.pack(name_pad=HOST_NAME_PAD)
        self.info(f"Host identity: OT={self.profile.name!r}, "
                  f"TID=0x{self.profile.tid:04x}, SID=0x{self.profile.sid:04x}")
        self.info("Host LinkPlayer display identity: "
                  f"name_bytes={wire[8:16].hex()} "
                  f"language={int.from_bytes(wire[26:28], 'little')}")
        self.info(f"RFU parent identity: raw={self.session.rfu.host_session_id.hex()} "
                  f"u16=0x{int.from_bytes(self.session.rfu.host_session_id, 'little'):04x} "
                  "(shared by discovery beacon and A response)")
        self.info("RFU block delivery: " + (
            "Pia-backed send-once mode (recommended for this LDN bridge)."
            if self.config.trust_pia else
            "raw-RFU retransmit mode (diagnostic; may flood the Pia bridge)."))
        self.info("Pia nonce mode: " + (
            "native session-wide counter" if self.config.native_nonce_sequence
            else "independent random values"))

    def _send_pending(self, datagrams):
        for outbound in datagrams:
            self.network.send(outbound.data, outbound.destination)

    def _log_protocol_events(self, events):
        if "connect" in events:
            self.info("Switch requested the RFU link; preparing the leader A response.")
        if "child_ni_complete" in events:
            self.info("Received the Switch RFU identity; sending join-status NI.")

    def _log_trade_progress(self):
        state = self.session.trade.state
        if state != self._last_trade_state:
            self._last_trade_state = state
            message = {
                host_trade.H_ENTRY_CARD: "LinkPlayer exchange complete; exchanging trainer cards.",
                host_trade.H_ENTRY_SEAT: "Trainer cards exchanged; walking the Linux leader into the left chair.",
                host_trade.H_PARTY: "Trade-room entry complete; exchanging party data.",
                host_trade.H_SELECT: "Party exchange complete; trade selection is active.",
            }.get(state)
            if message:
                self.info(message)
        if self.session.trade.commits > self._saved_commits:
            self._saved_commits = self.session.trade.commits
            self._save_received()

    def _save_received(self):
        mons = self.session.trade.received_mons
        if not mons:
            return
        if self.config.trades == 1:
            targets = [(mons[-1], self.config.output_path)]
        else:
            stem, _ = os.path.splitext(self.config.output_path)
            targets = []
            for index, pokemon in enumerate(mons, 1):
                species = "".join(c for c in pokemon.species_name if c.isalnum()) \
                    or f"sp{pokemon.species}"
                targets.append((pokemon, f"{stem}_trade{index}_{species}.{self.config.output_format}"))
        for pokemon, path in targets:
            saver = pokemon.save_ek3 if self.config.output_format == "ek3" else pokemon.save_pk3
            saver(path, size=self.config.output_size)
        pokemon, path = targets[-1]
        self.info(f"Received: {pokemon.species_name} (#{pokemon.species})")
        print(f"Saved -> {path} ({self.config.output_format}, {self.config.output_size}B)")

    def run(self):
        joined_once = False
        rfu_ni_logged = False
        try:
            link_player = self._build_components()
            self._log_identity(link_player)
            self.network.start(preflight=not self.config.skip_preflight)
            self.injector = self.injector_factory(
                channel=self.config.channel, log=self.log)
            self.injector.start()
            self.info("Hosting Direct Corner. On the Switch choose Join Group.")
            while True:
                if self.injector.error is not None:
                    raise RuntimeError(f"802.11 beacon injector stopped: {self.injector.error}")
                if self.network.participants and not joined_once:
                    joined_once = True
                    self.peer.on_participant_joined()
                    self.info("Switch joined the Linux LDN host successfully.")
                if joined_once and not self.network.participants:
                    if self.session.trade.close_confirmed and not self.session.trade.done:
                        if not self._absence_logged:
                            self._absence_logged = True
                            self.info("The console left LDN after confirming room exit; "
                                      "finishing the 15-second host grace period.")
                    else:
                        self.session.on_ldn_leave()
                        self.info("The console left the LDN network; stopping host peer traffic.")
                        break

                for datagram, src_ip in self.network.recv():
                    events = self.peer.receive(datagram, src_ip)
                    self._log_protocol_events(events)
                    self._send_pending(self.peer.drain())

                now = time.monotonic()
                self._send_pending(self.peer.tick(now))
                self._log_trade_progress()
                if self.session.rfu.ni_complete and not rfu_ni_logged:
                    rfu_ni_logged = True
                    self.info("RFU NI handshake complete; parent UNI and trade-room startup are active.")
                if self.session.trade.done and not self.network.participants:
                    self.info("Room-exit grace period complete; host peer traffic stopped cleanly.")
                    break
                timeout = self.peer.next_deadline(now, HOST_CONTROL_POLL_SECONDS)
                self.network.wait_readable(timeout)
        except KeyboardInterrupt:
            self.log("[host] interrupted; shutting down")
        finally:
            if self.injector is not None:
                self.injector.stop()
            if self.network is not None:
                self.network.stop()
            if self.tracer is not None:
                self.tracer.close()
                self.log(f"[host] trace written: {self.config.capture_path} "
                         f"(counts: {self.tracer.counts})")
        return joined_once
