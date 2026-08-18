"""Application runtime for hosting one FRLG Mystery Gift distribution.

Everything below the activity - LDN, Pia, Reliable, RFU bring-up, the beacon
injector, the join/leave lifecycle - is the trade host's proven code, so this
subclasses :class:`frlgsim.host_app.HostApplication` and replaces only the three
trade-specific seams: what gets built, what gets logged at startup, and what
counts as progress.
"""

from dataclasses import dataclass

from . import host_session, ldntrace, wonder_card
from .host_app import HostApplication
from .host_beacon import build_wonder_card_app_data
from .host_mystery_gift import (
    MG_CLOSE, MG_DONE, MG_GIFT, MG_START, HostMysteryGiftEngine,
)
from .host_pia import HostPeerProtocol
from .linkplayer import HOST_NAME_PAD
from .mg_server import SERVER_RESULT_NAMES, SVR_MSG_CARD_SENT


@dataclass(frozen=True)
class MysteryGiftRunConfig:
    """Options for one Wonder Card distribution session."""

    item: int = wonder_card.DEFAULT_GIFT_ITEM
    flag_id: int = 1003
    card_title: str = wonder_card.DEFAULT_GIFT_TITLE
    card_subtitle: str = wonder_card.DEFAULT_GIFT_SUBTITLE
    trust_pia: bool = True
    password: bytes | None = None
    phy: str = "auto"
    keys_path: str = "~/.switch/prod.keys"
    local_comm_id: int | None = None
    capture_path: str | None = None
    channel: int = 1
    scene_id: int | None = None
    # Must match the trade host. This is an LDN/Pia-layer value, not the RFU
    # group size: build_net_conn_request emits a station array of
    # max_stations * 22 bytes and native FRLG always fills every configured slot,
    # so lowering it to the two players a gift actually involves changes the
    # length of the Net 0x11 the console has to parse. A live run with 2 got the
    # console onto the LDN network and then never drew a single Pia reply.
    max_participants: int = 6
    skip_preflight: bool = False
    # These are the live-proven Switch host settings. The CLI exposes matching
    # --no-* switches for targeted transport diagnostics.
    skip_encryption: bool = True
    native_nonce_sequence: bool = True
    session_response_first: bool = True

    def __post_init__(self):
        if type(self.item) is not int or not 0 < self.item <= 0xFFFF:
            raise ValueError("item must be a positive 16-bit item id")
        # Raises with the accepted range if flag_id is outside sReceivedGiftFlags.
        wonder_card.flag_for_flag_id(self.flag_id)
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


class MysteryGiftHostApplication(HostApplication):
    """Host one Wonder Card handout for a single console."""

    def __init__(self, config, profile, **kwargs):
        super().__init__(config, profile, **kwargs)
        self.card = None
        self.ram_script = None
        self._last_state = None
        self._result_logged = False

    def _build_payload(self):
        """Build the live payload from the shared Wonder Card defaults.

        ``build_berry_gift`` is the sole source of the card's body, Claydol
        icon, signature, and hidden ID number.  The CLI may still override the
        item, receipt flag, title, and subtitle.
        """
        return wonder_card.build_default_gift(
            item=self.config.item,
            flag_id=self.config.flag_id,
            title=self.config.card_title,
            subtitle=self.config.card_subtitle)

    def _build_components(self):
        phy, keys = self._resolve_phy_and_keys()
        link_player = self.profile.to_link_player()
        self.card, self.ram_script = self._build_payload()
        engine = HostMysteryGiftEngine(
            self.card, self.ram_script, link_player=link_player,
            trust_pia=self.config.trust_pia, log=self.log)
        self.session = host_session.HostSession(engine=engine, log=self.log)
        inactive, active = build_wonder_card_app_data(
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
        self._last_state = self.session.activity.state
        return link_player

    def _log_identity(self, link_player):
        wire = link_player.pack(name_pad=HOST_NAME_PAD)
        flag = wonder_card.flag_for_flag_id(self.config.flag_id)
        self.info(f"Host identity: OT={self.profile.name!r}, "
                  f"TID=0x{self.profile.tid:04x}, SID=0x{self.profile.sid:04x}")
        self.info("Host LinkPlayer display identity: "
                  f"name_bytes={wire[8:16].hex()} "
                  f"language={int.from_bytes(wire[26:28], 'little')}")
        self.info(f"RFU parent identity: raw={self.session.rfu.host_session_id.hex()} "
                  f"u16=0x{int.from_bytes(self.session.rfu.host_session_id, 'little'):04x}")
        self.info(f"Gift: {self.config.card_title!r} - Wonder Card flagId "
                  f"{self.config.flag_id} (receipt flag 0x{flag:03x}), "
                  f"item {self.config.item}, "
                  f"card {len(self.card)}B + RAM script {len(self.ram_script)}B")
        self.info("Advertising ACTIVITY_WONDER_CARD. On the Switch choose "
                  "Mystery Gift -> Wonder Cards -> Friend.")

    def _log_trade_progress(self):
        """Report Mystery Gift milestones (the base class calls this every tick)."""
        engine = self.session.activity
        state = engine.state
        if state != self._last_state:
            self._last_state = state
            message = {
                MG_START: "LinkPlayer exchange complete; waiting for the console's gift client.",
                MG_GIFT: "Sending the Wonder Card conversation.",
                MG_CLOSE: "Gift conversation finished; closing the RFU link.",
                MG_DONE: "Mystery Gift session complete.",
            }.get(state)
            if message:
                self.info(message)
        if engine.result is not None and not self._result_logged:
            self._result_logged = True
            self.info("Result: " + SERVER_RESULT_NAMES.get(
                engine.result, f"code {engine.result}"))

    def _save_received(self):
        """Nothing to save: the gift travels outward only."""

    def run(self):
        joined = super().run()
        engine = self.session.activity if self.session is not None else None
        if engine is not None and engine.result == SVR_MSG_CARD_SENT:
            print("Wonder Card delivered. On the Switch, talk to the delivery man "
                  "on the second floor of any Pokemon Center to receive the gift.")
        elif engine is not None and engine.result is not None:
            print("Session finished without delivering a card: "
                  + SERVER_RESULT_NAMES.get(engine.result, f"code {engine.result}"))
        return joined
