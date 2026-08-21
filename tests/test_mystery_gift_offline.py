"""Offline tests for the Mystery Gift distributor work (Milestone-1 foundations + payload).

Everything here is hardware-independent: it checks the byte-exact protocol pieces built so far
against the FRLG decomp facts, with no Switch, no LDN, no root. The hardware-iterated integration
layers (Pia host FSM, NI handshake, ldn.create_network, the engine) are NOT covered here because
they can only be validated against a real console.

Run standalone (no pytest needed):   python tests/test_mystery_gift_offline.py
Or under pytest if installed:         pytest tests/test_mystery_gift_offline.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frlgsim import gbaframe, rfu, linkplayer, wonder_card, ni, beacon, transport
from frlgsim import mystery_gift as mg
from frlgsim import save_inject as si


# --- CRC16 (MysteryGiftLink header checksum) -------------------------------------------------
def test_crc16_bitwise_equals_game_table():
    """crc16() must equal the game's table-driven CalcCRC16WithTable [util.c:250] for any data."""
    for sample in (b"", b"\x00", b"GameFreak inc.", bytes(range(256)), b"\xff" * 333,
                   os.urandom(500)):
        assert mg.crc16(sample) == mg._crc16_tabledriven(sample)


def test_crc16_regression_anchors():
    """Fixed values captured from the verified routine (init 0x1121, poly 0x8408, final ~crc)."""
    assert mg.crc16(b"") == 0xEEDE
    assert mg.crc16(b"\x00") == 0xCF65
    assert mg.crc16(b"GameFreak inc.") == 0xC20F
    assert mg.crc16(b"123456789") == 0xBE75


def test_mg_link_constants():
    assert mg.MG_LINKID_CLIENT_SCRIPT == 16 and mg.MG_LINKID_GAME_DATA == 17
    assert mg.MG_LINKID_CARD == 22 and mg.MG_LINKID_RAM_SCRIPT == 25 and mg.MG_LINKID_READY_END == 20
    assert mg.MG_LINK_BUFFER_SIZE == 0x400 and mg.MG_LINK_HEADER_SIZE == 6 and mg.MG_LINK_MAX_CHUNK == 252


# --- Wonder Card + delivery RAM script (the Lansat Berry payload) -----------------------------
def test_wonder_card_size_and_validation_fields():
    """332 bytes, and the fields the console's ValidateWonderCard [mystery_gift.c:191] checks."""
    card = wonder_card.build_wonder_card(flag_id=1003, title="LANSAT BERRY")
    assert len(card) == 332
    assert int.from_bytes(card[0:2], "little") == 1003          # flagId != 0
    assert (card[8] & 0x3) == mg.CARD_TYPE_GIFT                  # type < 3
    assert ((card[8] >> 2) & 0xF) < mg.NUM_WONDER_BGS           # bgType < 8
    assert ((card[8] >> 6) & 0x3) in (0, 1, 2)                  # sendType valid
    assert card[9] <= mg.MAX_STAMP_CARD_STAMPS                  # maxStamps <= 7


def test_wonder_card_rejects_invalid_like_the_console():
    """Builder rejects the same out-of-range fields SaveWonderCard would reject."""
    for kwargs in ({"flag_id": 0}, {"bg_type": 8}, {"card_type": 3},
                   {"send_type": 3}, {"max_stamps": 8}):
        try:
            wonder_card.build_wonder_card(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {kwargs}")


def test_delivery_ram_script_is_byte_exact():
    """lock; faceplayer; giveitem LANSAT_BERRY(173),1; setflag 0x2AA; release; endram."""
    script = wonder_card.build_delivery_ram_script(item=173, flag_id=1003)
    assert script == bytes([
        0x6A,                    # lock
        0x5A,                    # faceplayer
        0x1A, 0x00, 0x80, 0xAD, 0x00,   # setorcopyvar VAR_0x8000, 173
        0x1A, 0x01, 0x80, 0x01, 0x00,   # setorcopyvar VAR_0x8001, 1
        0x09, 0x00,              # callstd STD_OBTAIN_ITEM
        0x29, 0xAA, 0x02,        # setflag FLAG_WONDER_CARD_UNUSED_1 (0x2AA)
        0x6C,                    # release
        0x0D,                    # endram
    ])


def test_flag_id_maps_to_receipt_flag():
    assert wonder_card.flag_for_flag_id(1000) == 0x2A7   # FLAG_RECEIVED_AURORA_TICKET
    assert wonder_card.flag_for_flag_id(1003) == 0x2AA   # FLAG_WONDER_CARD_UNUSED_1
    for bad in (999, 1020):
        try:
            wonder_card.flag_for_flag_id(bad)
        except ValueError:
            continue
        raise AssertionError(f"flagId {bad} should be rejected")


def test_berry_gift_bundle():
    """The two-berry gift: giveitem LANSAT(173) then LIECHI(168), one setflag, one endram."""
    card, script = wonder_card.build_berry_gift()
    assert len(card) == 332 and len(script) == 31
    assert mg.crc16(card) == 0x1B48   # anchor for the assembled card
    assert script == bytes([
        0x6A, 0x5A,                      # lock; faceplayer
        0x1A, 0x00, 0x80, 0xAD, 0x00,    # setorcopyvar VAR_0x8000, LANSAT(173)
        0x1A, 0x01, 0x80, 0x01, 0x00,    # setorcopyvar VAR_0x8001, 1
        0x09, 0x00,                      # callstd STD_OBTAIN_ITEM
        0x1A, 0x00, 0x80, 0xA8, 0x00,    # setorcopyvar VAR_0x8000, LIECHI(168)
        0x1A, 0x01, 0x80, 0x01, 0x00,    # setorcopyvar VAR_0x8001, 1
        0x09, 0x00,                      # callstd STD_OBTAIN_ITEM
        0x29, 0xAA, 0x02,                # setflag 0x2AA (one receipt flag for the whole gift)
        0x6C, 0x0D,                      # release; endram
    ])


def test_altering_cave_script_cycles_all_nine_encounter_tables():
    """Increment 0..8 modulo 9, show the embedded message, and retain the RAM script."""
    script = wonder_card.build_altering_cave_script()
    assert script[:37] == bytes([
        0xB8, 0x00, 0x00, 0x00, 0x00,        # setvaddress 0
        0x17, 0x24, 0x40, 0x01, 0x00,        # addvar VAR_ALTERING_CAVE_WILD_SET, 1
        0x21, 0x24, 0x40, 0x09, 0x00,        # compare variable, 9 tables
        0xBB, 0x00, 0x1A, 0x00, 0x00, 0x00,  # if value < 9, skip reset to offset 26
        0x16, 0x24, 0x40, 0x00, 0x00,        # otherwise setvar variable, 0
        0x6A, 0x5A,                          # lock; faceplayer
        0xBD, 0x25, 0x00, 0x00, 0x00,        # vmessage embedded text at offset 37
        0x66, 0x6D, 0x6C, 0x02,              # waitmessage; waitbuttonpress; release; end
    ])
    assert script[-1] == 0xFF                 # message terminator
    assert script[36] == 0x02                 # persistent `end`, not one-shot `endram`
    assert [(value + 1) % wonder_card.NUM_ALTERING_CAVE_TABLES for value in range(9)] \
        == [1, 2, 3, 4, 5, 6, 7, 8, 0]


def test_altering_cave_gift_bundle():
    card, script = wonder_card.build_altering_cave_gift()
    assert len(card) == 332
    assert int.from_bytes(card[0:2], "little") == 1003
    assert int.from_bytes(card[2:4], "little") == 41       # Zubat card icon
    assert script == wonder_card.build_altering_cave_script()
    assert len(script) <= 995


# --- Parent-side 0x54 framing (sim as leader/parent) -----------------------------------------
def test_parent_uni_echo_table_roundtrips_through_parse_in():
    """wrap_t_parent(parent_uni_slot(70B table)) must parse back into 5 mpId rows via parse_in."""
    row0 = rfu.serialize(rfu.send_player_ids_words())            # parent's own broadcast
    row1 = rfu.serialize([0x8800, 3, 0x81, 0, 0, 0, 0])         # child's reflected block-init
    table = rfu.pack_recv_cmds([row0, row1])
    assert len(table) == 70

    frame = gbaframe.wrap_t_parent(rfu.parent_uni_slot(table, bm_slot=1), ts=0x1234)
    rec = gbaframe.parse_in(frame)
    assert rec["type"] == "T" and rec["ts"] == 0x1234
    assert rec["llsf_state"] == rfu.LCOM_UNI
    slots = dict(rec["slots"])
    assert len(rec["slots"]) == 5
    assert slots[0] == row0 and slots[1] == row1 and slots[2] == b"\x00" * 14
    assert rfu.parse_slot(slots[0])["op"] == rfu.SEND_PLAYER_IDS
    assert rfu.parse_slot(slots[1])["op"] == rfu.SEND_BLOCK_INIT


def test_parent_accept_and_disconnect_frames():
    acc = gbaframe.parse_in(gbaframe.build_accept(b"\xAB\xCD", b"\x79\x67"))
    assert acc["type"] == "A" and acc["connect_id"] == b"\x79\x67"
    dis = gbaframe.parse_in(gbaframe.build_disconnect(b"\x79\x67"))
    assert dis["type"] == 0x44


# --- Parent link-opcode payloads --------------------------------------------------------------
def test_send_player_ids_payload():
    """0x7700: w1=playerCount=2, linkPlayerIdx[0]=1 (child -> mpId 1), rest 0 [link_rfu_2.c:1298]."""
    b = rfu.serialize(rfu.send_player_ids_words())
    assert b[0:2] == b"\x00\x77" and b[2:4] == b"\x02\x00"
    assert b[4:6] == b"\x01\x00" and b[6:8] == b"\x00\x00"


def test_send_block_req_none_payload():
    r = rfu.parse_slot(rfu.serialize(rfu.send_block_req_words()))
    assert r["op"] == rfu.SEND_BLOCK_REQ and r["reqtype"] == rfu.BLOCK_REQ_SIZE_NONE


def test_link_player_block_has_both_magics():
    lp = linkplayer.LinkPlayer(name="EMU", version=linkplayer.VERSION_FIRE_RED, player_id=0)
    blk = linkplayer.build_block(lp)
    assert len(blk) == 60
    parsed, ok = linkplayer.parse_block(blk)
    assert ok and parsed.name == "EMU" and parsed.version == linkplayer.VERSION_FIRE_RED


# --- Parent NI handshake (sender = join status; receiver = ack + reassemble child game data) --
def test_ni_send_sequence_matches_verified_child_sender():
    """The shared _ni_send_sequence must reproduce the byte-verified child NISender frame-for-frame
    (this is what lets ParentNISender reuse it with confidence)."""
    src = ni.build_game_data(5, 0x2288, "EMU")
    sender = ni.NISender(src)
    got = []
    while not sender.done:
        slot = sender.next_slot()
        if slot is None:
            break
        got.append(slot)
    seq = ni._ni_send_sequence(src, data_type=1, payload_size=12)
    rebuilt = [rfu.child_ni_llsf(st, n, ph, 0, sz) + pay for (st, n, ph, sz, pay) in seq]
    assert got == rebuilt
    assert len(got) == 6                              # NI_START + 3xNI + NI_END + NULL


def test_parent_ni_sender_join_status_frames():
    """PARENT NI sender for the 1-byte join status: two NI_STARTs (payloadSize 5 < 7-byte header),
    one NI (the status), NI_END, NULL — byte-exact in 3-byte PARENT LLSF."""
    sender = ni.ParentNISender(status=ni.RFU_STATUS_JOIN_GROUP_OK)
    got = []
    while not sender.done:
        got.append(sender.next_slot())
    hdr = ni._ni_header(0, 5, 1)                      # dataType 0, payloadSize 5, dataSize 1
    expected = [
        rfu.parent_ni_llsf(rfu.LCOM_NI_START, 1, 0, 0, 5, 1) + hdr[0:5],
        rfu.parent_ni_llsf(rfu.LCOM_NI_START, 2, 0, 0, 2, 1) + hdr[5:7],
        rfu.parent_ni_llsf(rfu.LCOM_NI, 1, 0, 0, 1, 1) + bytes([5]),
        rfu.parent_ni_llsf(rfu.LCOM_NI_END, 0, 0, 0, 0, 1),
        rfu.parent_ni_llsf(rfu.LCOM_NULL, 1, 0, 0, 0, 1),
    ]
    assert got == expected


def test_child_acks_of_parent_ni_match_reference_capture():
    """Round-trip: wrap each ParentNISender frame in a HOST 'T', parse_in it, feed the child's
    NIReceiver — the child's recv-ack sequence must equal the reference capture (8006/0007/800a/000e),
    and the child must read the join status 5. Exercises wrap_t_parent + parse_in on NI frames too."""
    sender = ni.ParentNISender()
    recv = ni.NIReceiver()
    acks, ts = [], 1
    while not sender.done:
        rec = gbaframe.parse_in(gbaframe.wrap_t_parent(sender.next_slot(), ts))
        ts += 1
        assert rec["type"] == "T"
        ack = recv.on_host_ni(rec.get("ni"))
        if ack is not None:
            acks.append(ack.hex())
    assert acks == ["8006", "0007", "800a", "000e"]
    assert recv.status == ni.RFU_STATUS_JOIN_GROUP_OK
    assert recv.complete


def test_parent_ni_receiver_acks_and_reassembles_child_game_data():
    """PARENT NI receiver: ack the console-child's game-data NI (mirror state/n/phase, ack=1, sz=0 in
    PARENT LLSF) and reassemble the 26-byte RfuGameData with the child's trainer id + uname."""
    src = ni.build_game_data(5, 0x2288, "EMU")
    child = ni.NISender(src)                          # models the console acting as RFU child
    recv = ni.ParentNIReceiver()
    acks = []
    while not child.done:
        slot = child.next_slot()
        if slot is None:
            break
        ack = recv.on_child_ni(ni.decode_child_ni_slot(slot))
        if ack is not None:
            acks.append(ack)
    assert recv.complete
    assert recv.game_data == src
    assert recv.data_type == 1 and recv.data_size == 26 and recv.payload_size == 12
    assert recv.trainer_id == 0x2288
    assert recv.uname == src[17:26]
    expected_acks = [                                 # NULL is NOT acked -> 5 acks for 6 child frames
        rfu.parent_ni_llsf(rfu.LCOM_NI_START, 1, 0, 1, 0, 1),
        rfu.parent_ni_llsf(rfu.LCOM_NI, 1, 0, 1, 0, 1),
        rfu.parent_ni_llsf(rfu.LCOM_NI, 1, 1, 1, 0, 1),
        rfu.parent_ni_llsf(rfu.LCOM_NI, 1, 2, 1, 0, 1),
        rfu.parent_ni_llsf(rfu.LCOM_NI_END, 0, 0, 1, 0, 1),
    ]
    assert acks == expected_acks


# --- Host beacon encoder (inverse of transport._dump_beacon / _b85_decode) --------------------
def test_b85_encode_inverts_decode():
    """beacon.b85_encode must be the exact inverse of transport._b85_decode for 4-byte groups."""
    for data in (bytes(range(24)), b"\x00\x00\x00\x00", b"\xff" * 24, os.urandom(24)):
        assert transport._b85_decode(beacon.b85_encode(data))[:len(data)] == data


def test_beacon_record_round_trips_through_dump_decoder():
    """build_beacon's RFU record must decode back to the same fields transport._dump_beacon reads
    (trainer id, name, RFU session id, tradeSpecies) — the only fields the decoder proves."""
    app = beacon.build_beacon(trainer_id=0x2288, name="EMU", rfu_session_id=beacon.RFU_SERIAL_GAME,
                              trade_species=277)
    assert len(app) == beacon.PIA_HDR + 30                 # 0x5C header + base85(24B) = 30 chars
    rec = transport._b85_decode(app[beacon.PIA_HDR:])[:beacon.RECORD_SIZE]
    assert int.from_bytes(rec[0:2], "little") == 0x2288
    assert transport._frlg_name(rec[2:10]) == "EMU"
    assert int.from_bytes(rec[10:12], "little") == beacon.RFU_SERIAL_GAME
    assert int.from_bytes(rec[20:24], "little") >> 16 == 277


def test_beacon_has_card_and_activity_bits_present():
    """The game-data word carries the hasCard bit and activity nibble (positions are live-tuned, but
    they must at least be encoded so there is something to calibrate)."""
    gd = beacon.game_data_word(activity=beacon.ACTIVITY_WONDER_CARD, has_card=True)
    assert gd & beacon.HASCARD_BIT                          # gname[0] |= 0x20
    assert (gd >> 8) & 0x7F == beacon.ACTIVITY_WONDER_CARD  # activity in the compat high byte (first cut)


def test_mutate_beacon_preserves_header_changes_record():
    """mutate_beacon keeps a captured Pia header verbatim and only rewrites overridden RFU fields."""
    captured = bytes(range(beacon.PIA_HDR)) + beacon.b85_encode(
        beacon.build_record(trainer_id=0x1111, name="ABC", rfu_session_id=9))
    out = beacon.mutate_beacon(captured, name="EMU", trainer_id=0x2288)
    assert out[:beacon.PIA_HDR] == captured[:beacon.PIA_HDR]     # header untouched
    rec = transport._b85_decode(out[beacon.PIA_HDR:])[:beacon.RECORD_SIZE]
    assert transport._frlg_name(rec[2:10]) == "EMU" and int.from_bytes(rec[0:2], "little") == 0x2288
    assert int.from_bytes(rec[10:12], "little") == 9            # not overridden -> preserved


# --- Hosting preflight (iw-phy AP-mode check) + trace harness ---------------------------------
_IW_MT7601U = """Wiphy phy0
	max # scan SSIDs: 4
	Supported interface modes:
		 * managed
		 * monitor
	software interface modes (can always be added):
		 * monitor
	Supported commands:
		 * new_interface
"""

_IW_MT76X2U = """Wiphy phy1
	Supported interface modes:
		 * managed
		 * AP
		 * monitor
		 * mesh point
	software interface modes (can always be added):
		 * monitor
	valid interface combinations:
		 * #{ managed, AP, mesh point } <= 2, #{ IBSS } <= 1,
"""


def test_preflight_rejects_mt7601u_with_clear_verdict():
    """The MT7601U case: managed+monitor only -> one clear RuntimeError, not ENOTSUP walls."""
    modes, soft = transport._parse_iw_modes(_IW_MT7601U)
    assert modes == ["managed", "monitor"] and soft == ["monitor"]
    try:
        transport.preflight_host("phy0", log=lambda *a: None, _iw_output=_IW_MT7601U)
    except RuntimeError as e:
        assert "no AP mode" in str(e) and "AP-capable adapter" in str(e)
    else:
        raise AssertionError("preflight should reject a phy without AP mode")


def test_preflight_accepts_ap_capable_phy():
    modes, _soft = transport._parse_iw_modes(_IW_MT76X2U)
    assert "AP" in modes
    assert transport.preflight_host("phy1", log=lambda *a: None, _iw_output=_IW_MT76X2U) is True


def test_tracer_writes_jsonl(tmp_path=None):
    """ldntrace.Tracer writes one JSON object per line with rec/kind/ts, and a closing summary."""
    import json
    import tempfile
    from frlgsim import ldntrace
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "trace.jsonl")
        tr = ldntrace.Tracer(path, log=lambda *a: None)
        tr.write("udp_out", dst="169.254.1.255", hex="5c00")
        tr.write("advert", nonce="00000001", hex="7f0022aa")
        tr.close()
        recs = [json.loads(line) for line in open(path)]
    kinds = [r["kind"] for r in recs]
    assert kinds == ["udp_out", "advert", "summary"]
    assert all(r["rec"] == "trace" and "ts" in r for r in recs)
    assert recs[0]["hex"] == "5c00" and recs[2]["counts"]["advert"] == 1


# --- save/RAM injection (Tier-2a payload delivery via mGBA) -----------------------------------
def _make_synthetic_sav(slot0_counter=10, slot1_counter=9):
    """A minimal but structurally faithful 128 KiB FRLG flash save: two slots of 14 signed
    sectors, logical ids rotated so id != physical index (id 4 lands on the last sector of each
    slot). Only the footers matter to the injector; sector data is zeroed."""
    sav = bytearray(si.SECTORS_COUNT * si.SECTOR_SIZE)
    for slot, counter in ((0, slot0_counter), (1, slot1_counter)):
        for within in range(si.NUM_SECTORS_PER_SLOT):
            phys = slot * si.NUM_SECTORS_PER_SLOT + within
            base = phys * si.SECTOR_SIZE
            sect_id = (within + 5) % si.NUM_SECTORS_PER_SLOT      # rotate: id 4 -> within 13
            sav[base + si.SECTOR_ID_OFF:base + si.SECTOR_ID_OFF + 2] = sect_id.to_bytes(2, "little")
            sav[base + si.SECTOR_SIGNATURE_OFF:base + si.SECTOR_SIGNATURE_OFF + 4] = \
                si.SECTOR_SIGNATURE.to_bytes(4, "little")
            sav[base + si.SECTOR_COUNTER_OFF:base + si.SECTOR_COUNTER_OFF + 4] = \
                counter.to_bytes(4, "little")
    return bytes(sav)


def test_save_inject_offsets_and_chunk_size():
    """The SaveBlock1 field offsets and the id-4 chunk size, straight from the decomp."""
    assert si.MYSTERYGIFT_CARDCRC_OFF == 0x3120 + 448
    assert si.MYSTERYGIFT_CARD_OFF == 0x3120 + 452
    assert si.RAM_SCRIPT_DATA_SIZE == 999
    assert si.sb1_chunk_size(3) == 3816            # min(0x3D68 - 3*3968, 3968)
    assert si.sb1_chunk_size(0) == 3968
    # Every byte we write must sit inside the checksummed range of the id-4 sector.
    size = si.sb1_chunk_size(3)
    for off, length in ((si.MYSTERYGIFT_CARDCRC_OFF, 4), (si.MYSTERYGIFT_CARD_OFF, 332),
                        (si.SB1_RAMSCRIPT_OFF, 4 + si.RAM_SCRIPT_DATA_SIZE)):
        lo = off - si.SAVEBLOCK1_END_CHUNK_BASE
        assert 0 <= lo and lo + length <= size, (hex(off), lo, length)


def test_sector_checksum_matches_calculatechecksum():
    """CalculateChecksum: u32-word sum then (>>16)+ fold to u16 (save.c:614)."""
    assert si.sector_checksum(b"\x01\x00\x00\x00\x02\x00\x00\x00", 8) == 3
    # Overflow fold: 0xFFFFFFFF + 0xFFFFFFFF -> 0xFFFFFFFE -> (0xFFFF + 0xFFFE) & 0xFFFF.
    assert si.sector_checksum(b"\xff" * 8, 8) == 0xFFFD
    # `size` truncates: trailing bytes past `size` are ignored.
    assert si.sector_checksum(b"\x01\x00\x00\x00\xff\xff\xff\xff", 4) == 1


def test_ram_script_struct_layout_and_checksum():
    _, script = wonder_card.build_berry_gift()
    data, crc = si.build_ram_script_struct(script)
    assert len(data) == 999
    assert data[0] == 51 and data[1] == 0xFF and data[2] == 0xFF and data[3] == 0xFF
    assert data[4:4 + len(script)] == script
    assert data[4 + len(script):] == bytes(995 - len(script))   # zero-padded body
    assert crc == mg.crc16(data)
    try:
        si.build_ram_script_struct(b"\x00" * 996)
    except ValueError:
        pass
    else:
        raise AssertionError("RAM script body over 995 B must be rejected")


def test_inject_selects_active_slot_by_counter():
    """The injected sector is the id-4 sector of the greatest-counter slot (GetSaveValidStatus)."""
    card, script = wonder_card.build_berry_gift()
    _, info = si.inject_gift(_make_synthetic_sav(slot0_counter=10, slot1_counter=9), card, script)
    assert info["slot"] == 0 and info["phys_sector"] == 13 and info["counter"] == 10
    _, info2 = si.inject_gift(_make_synthetic_sav(slot0_counter=3, slot1_counter=99), card, script)
    assert info2["slot"] == 1 and info2["phys_sector"] == 27 and info2["counter"] == 99


def test_inject_produces_a_console_valid_gift():
    """After injection the console's deliveryman gate accepts card + RAM script and yields our
    exact delivery bytecode (get_saved_ram_script_if_valid mirrors script.c:554)."""
    card, script = wonder_card.build_berry_gift()
    sav, _ = si.inject_gift(_make_synthetic_sav(), card, script)
    got_card, crc_ok = si.read_saved_wonder_card(sav)
    assert crc_ok and got_card == card and si.validate_wonder_card(got_card)
    body = si.get_saved_ram_script_if_valid(sav)
    assert body is not None and body[:len(script)] == script
    assert body[len(script):] == bytes(995 - len(script))


def test_inject_recomputes_the_sector_checksum():
    """The written footer checksum equals CalculateChecksum over the id-4 chunk size, and it
    actually changed from the pre-injection value."""
    card, script = wonder_card.build_berry_gift()
    before = _make_synthetic_sav()
    sav, info = si.inject_gift(before, card, script)
    base = info["phys_sector"] * si.SECTOR_SIZE
    stored = int.from_bytes(sav[base + si.SECTOR_CHECKSUM_OFF:base + si.SECTOR_CHECKSUM_OFF + 2], "little")
    assert stored == info["sector_checksum"]
    assert stored == si.sector_checksum(sav[base:base + si.SECTOR_DATA_SIZE], si.sb1_chunk_size(3))
    old = int.from_bytes(before[base + si.SECTOR_CHECKSUM_OFF:base + si.SECTOR_CHECKSUM_OFF + 2], "little")
    assert stored != old


def test_inject_is_nondestructive_and_local():
    """Only the target sector's data + its footer checksum change; footer id/signature/counter
    and every other sector are byte-for-byte identical; the input bytes object is untouched."""
    card, script = wonder_card.build_berry_gift()
    before = _make_synthetic_sav()
    sav, info = si.inject_gift(before, card, script)
    assert len(sav) == len(before)
    phys = info["phys_sector"]
    for p in range(si.SECTORS_COUNT):
        b0, b1 = p * si.SECTOR_SIZE, (p + 1) * si.SECTOR_SIZE
        if p != phys:
            assert sav[b0:b1] == before[b0:b1], f"unrelated sector {p} changed"
    base = phys * si.SECTOR_SIZE
    for off, width in ((si.SECTOR_ID_OFF, 2), (si.SECTOR_SIGNATURE_OFF, 4), (si.SECTOR_COUNTER_OFF, 4)):
        assert sav[base + off:base + off + width] == before[base + off:base + off + width]


def test_inject_validates_inputs_and_save():
    """Bad card size, an oversized script, and a save with no signed id-4 sector are all rejected."""
    card, script = wonder_card.build_berry_gift()
    for bad, exc in ((lambda: si.inject_gift(_make_synthetic_sav(), card[:-1], script), ValueError),
                     (lambda: si.inject_gift(_make_synthetic_sav(), card, b"\x00" * 996), ValueError),
                     (lambda: si.inject_gift(bytes(si.SECTORS_COUNT * si.SECTOR_SIZE), card, script), ValueError),
                     (lambda: si.inject_gift(b"\x00" * 1024, card, script), ValueError)):
        try:
            bad()
        except exc:
            continue
        raise AssertionError("expected rejection")


def test_deliveryman_gate_rejects_tampering():
    """A one-byte change to the card or RAM script fails its CRC and the deliveryman runs nothing."""
    card, script = wonder_card.build_berry_gift()
    sav, info = si.inject_gift(_make_synthetic_sav(), card, script)
    base = info["phys_sector"] * si.SECTOR_SIZE
    card_off = base + si.MYSTERYGIFT_CARD_OFF - si.SAVEBLOCK1_END_CHUNK_BASE
    ram_off = base + si.SB1_RAMSCRIPT_OFF + 4 - si.SAVEBLOCK1_END_CHUNK_BASE
    for pos in (card_off, ram_off):
        t = bytearray(sav)
        t[pos] ^= 0xFF
        assert si.get_saved_ram_script_if_valid(bytes(t)) is None
    # Corrupting the RAM-script magic also disqualifies it.
    t = bytearray(sav)
    t[ram_off] = 0
    assert si.get_saved_ram_script_if_valid(bytes(t)) is None


# --- standalone runner (no pytest) ------------------------------------------------------------
def _run():
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as e:                                  # noqa: BLE001 - report and continue
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
