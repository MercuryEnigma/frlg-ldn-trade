"""Composition of the FRLG leader Reliable, RFU, and trade layers."""

from . import reliable, rfu, trade
from .host_trade import HostTradeEngine
from .rfu_leader import RFULeader, UNI


class HostSession:
    """Transport-independent, single-child leader stack below Pia framing.

    Everything up to and including the RFU link is shared by every activity; only
    the engine above it differs.  Pass ``engine`` to host something other than a
    trade (see :class:`frlgsim.host_mystery_gift.HostMysteryGiftEngine`); it must
    provide ``tick``/``feed_child_slot``/``disconnect_requested``/
    ``mark_disconnect_sent``/``done``.
    """

    def __init__(self, party=None, *, engine=None, trade_slot=0, offered_slots=None,
                 trades=1, link_player=None, anim_delay=None, trust_pia=True,
                 log=lambda *a: None, reliable_kwargs=None, rfu_kwargs=None):
        self.reliable = reliable.HostReliableSession(**(reliable_kwargs or {}))
        self.rfu = RFULeader(**(rfu_kwargs or {}))
        if engine is not None:
            self.activity = engine
        elif party is not None:
            self.activity = HostTradeEngine(
                party, trade_slot=trade_slot, offered_slots=offered_slots, trades=trades,
                link_player=link_player,
                anim_delay=(trade.DEFAULT_ANIM_FRAMES if anim_delay is None else anim_delay),
                trust_pia=trust_pia, log=log)
        else:
            raise ValueError("HostSession needs either a party or an activity engine")
        self.log = log
        self.peer_open_ack_sent = False
        self.connect_seq = None
        self.connect_ack_sent = False
        self.close_poll_sent = False
        self.disconnect_sent = False
        self.stopped = False
        self.send_window_full = False
        self.window_full_ticks = 0

    @property
    def inflight(self):
        return self.reliable.inflight

    @property
    def trade(self):
        """The activity engine under its original name, for the trade host and its tests."""
        return self.activity

    def receive_reliable(self, payload, now_ms):
        """Consume one child Reliable message and return RFU event names."""
        if self.stopped:
            return []
        events = []
        for delivery in self.reliable.receive(payload, now_ms):
            event = self.rfu.receive(delivery.payload)
            if event is not None:
                events.append(event)
            if event == "connect" and self.connect_seq is None:
                self.connect_seq = delivery.seq
            if event == "uni":
                self.activity.feed_child_slot(self.rfu.child_cmd)
        return events

    def note_rtt(self, rtt_ms):
        self.reliable.note_rtt(rtt_ms)

    def tick(self, now_ms):
        """Advance Reliable timers and at most one native-rate RFU slot."""
        if self.stopped:
            return []
        out = list(self.reliable.poll(now_ms))
        for emission in out:
            if emission.flagsA != reliable.FLAGSA_CTRL:
                continue
            self.peer_open_ack_sent = True
            ack_id, _ = reliable.parse_bulk_ack(emission.payload)
            if self.connect_seq is not None and ack_id is not None:
                target = (self.connect_seq + 1) & 0xFFFF
                # ACK ids advance in wrapping 16-bit sequence space. Native
                # opens A only after the cumulative ACK covers C itself.
                if ((ack_id - target) & 0xFFFF) < 0x8000:
                    self.connect_ack_sent = True

        if self.reliable.inflight >= self.reliable.link.max_inflight:
            # The activity engine stops advancing entirely while this holds, so
            # the peer sees no further parent polls and eventually times out.
            # Silent before: a stalled session looked identical to a quiet one.
            if not self.send_window_full:
                self.send_window_full = True
                getattr(self.log, "info", self.log)(
                    f"Reliable send window is full ({self.reliable.inflight} frames "
                    f"unacknowledged); pausing the activity until it drains.")
            self.window_full_ticks += 1
            return out
        if self.send_window_full:
            self.send_window_full = False
            getattr(self.log, "info", self.log)(
                f"Reliable send window drained after {self.window_full_ticks} ticks; "
                "resuming the activity.")
            self.window_full_ticks = 0

        # Queue D one VBlank after the final parent close-link UNI poll.
        if (self.close_poll_sent and self.activity.disconnect_requested
                and not self.disconnect_sent):
            inner = self.rfu.disconnect_frame()
            if inner is not None:
                out.append(self.reliable.send(inner, now_ms))
            self.disconnect_sent = True
            self.activity.mark_disconnect_sent()
            return out

        # Before UNI, RFU control traffic owns the tick. Once UNI begins the
        # trade FSM supplies row zero of the parent's echo table.
        parent_words = self.activity.tick() if self.rfu.state == UNI else None
        # Native leader ordering: cumulatively ACK C (not merely the preceding
        # INIT) before opening our own fff0 stream with RFU A. The framing
        # adapter sends this tick's control-ACK batch before its A batch.
        if not self.reliable.local_opened and not self.connect_ack_sent:
            return out
        inner = self.rfu.tick(parent_words)
        if inner is None:
            return out
        if self.reliable.local_opened:
            out.append(self.reliable.send(inner, now_ms))
        else:
            out.append(self.reliable.open(inner, now_ms))

        if parent_words is not None and (parent_words[0] & rfu.RFUCMD_MASK) == rfu.READY_CLOSE_LINK:
            self.close_poll_sent = True
        return out

    def on_ldn_leave(self):
        """Stop all protocol output immediately when the station disappears."""
        self.stopped = True
        self.rfu.on_ldn_leave()
