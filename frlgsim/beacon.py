"""FRLG Mystery-Gift host beacon encoder - the LDN advertisement `application_data` we broadcast so
the console's Mystery Gift -> Receive Gift -> Friend list shows us and lets the player pick us (one
A-press). This is the ENCODE side; transport._dump_beacon / _b85_decode is the matching DECODE side
(validated against real host beacons on the join path), and this module is its inverse.

Layout (a real FRLG host's advertisement, as decoded by transport._dump_beacon):

    application_data = <Pia system header, 0x5C bytes> <custom-base85( 24-byte RFU search record )>

The 24-byte RFU search record (little-endian), per the _dump_beacon decoder:
    [0:2]   playerTrainerId (u16)
    [2:10]  in-game name (uname, 8 bytes, FRLG charset, 0xFF-terminated)
    [10:12] RFU session id (u16)
    [12:20] partnerInfo (8 bytes)
    [20:24] game data (u32): high u16 = tradeSpecies; low u16 = the RfuGameData compatibility word
            (language/version) plus the activity/hasCard bits that route us to the friend/MG list.

!! LIVE-TUNED !! The Switch LDN glue is not in the pokefirered decomp: the exact Pia-header contents
and the precise bit positions of `activity`/`hasCard` inside the game-data word are INFERRED. The
decoder only proves the four fields it reads back (trainer id, name, session id, tradeSpecies), so the
round-trip test only guards those. Everything else is a first cut to calibrate at the console. The
pragmatic path to "console lists us" is mutate_beacon(): capture a REAL host's application_data (the
join path already dumps it) and tweak only the fields you need, so the Pia header is known-good.
"""

from . import charmap

PIA_HDR = 0x5C                      # transport._PIA_HDR - the Pia 6.16-6.41 system header length
RECORD_SIZE = 24                    # the base85-encoded RFU search record

# --- friend / Mystery-Gift beacon field defaults (link_rfu.h RfuGameData) --------------------------
RFU_SERIAL_GAME = 0x0002            # the serial the friend path relies on (the emulator synthesizes it)
ACTIVITY_WONDER_CARD = 21          # gRfuGameData.activity for the Mystery Gift card list [union_room]
LANGUAGE_ENGLISH = 2               # compatibility.language
VERSION_FIRE_RED = 4               # gGameVersion FireRed (5 = LeafGreen)
HASCARD_BIT = 0x20                 # gname[0] |= 0x20 marks "has a Wonder Card to give" [plan/union_room]


# --- custom base85 (inverse of transport._b85_decode) ----------------------------------------------
def _b85_char(digit):
    """Map a base85 digit (0..84) to its alphabet byte: 0x23.. skipping 0x5C ('\\')."""
    c = 0x23 + (digit % 85)
    return c + 1 if c >= 0x5C else c               # skip 0x5C, matching the decoder's `c - 0x24` branch


def b85_encode(data):
    """Encode 4-byte little-endian groups as 5 base85 chars each, LOW digit first - the exact inverse
    of transport._b85_decode. `data` length should be a multiple of 4 (the RFU record is 24 bytes)."""
    data = bytes(data)
    if len(data) % 4:
        data = data.ljust(len(data) + (4 - len(data) % 4), b"\x00")
    out = bytearray()
    for i in range(0, len(data), 4):
        v = int.from_bytes(data[i:i + 4], "little")
        for _ in range(5):                         # first char = least-significant digit (decoder reverses)
            out.append(_b85_char(v % 85))
            v //= 85
    return bytes(out)


def encode_name(name, width=8):
    """Encode an in-game name to the beacon's FRLG charset field (0xFF-terminated/padded), matching
    what transport._frlg_name renders back."""
    return charmap.encode(name or "", width=width, pad=0xFF)


def game_data_word(*, trade_species=0, activity=ACTIVITY_WONDER_CARD, has_card=True,
                   language=LANGUAGE_ENGLISH, version=VERSION_FIRE_RED):
    """Compose the [20:24] game-data u32: high u16 = tradeSpecies, low u16 = the RfuGameData
    compatibility word (language:4 | version:4 ...) with the activity + hasCard bits.

    !! LIVE-TUNED !! The low-u16 bit packing (where exactly `activity` and `hasCard` sit) is the
    inferred part; calibrate at the console. Kept in one place so tuning is a single edit."""
    compat = (language & 0xF) | ((version & 0xF) << 4) | ((activity & 0x7F) << 8)
    if has_card:
        compat |= HASCARD_BIT                      # gname[0] |= 0x20
    return ((trade_species & 0xFFFF) << 16) | (compat & 0xFFFF)


def build_record(*, trainer_id, name, rfu_session_id, partner_info=b"", **game_data_kwargs):
    """Build the 24-byte RFU search record (pre-base85). `game_data_kwargs` -> game_data_word()."""
    rec = bytearray(RECORD_SIZE)
    rec[0:2] = (trainer_id & 0xFFFF).to_bytes(2, "little")
    rec[2:10] = encode_name(name, width=8)
    rec[10:12] = (rfu_session_id & 0xFFFF).to_bytes(2, "little")
    rec[12:20] = bytes(partner_info)[:8].ljust(8, b"\x00")
    rec[20:24] = game_data_word(**game_data_kwargs).to_bytes(4, "little")
    return bytes(rec)


def build_beacon(*, trainer_id=0x2288, name="EMU", rfu_session_id=0x0002, pia_header=None,
                 partner_info=b"", **game_data_kwargs):
    """Build a full `application_data` beacon: <Pia 0x5C header> <base85(24-byte RFU record)>.

    `pia_header` defaults to a zero-filled 0x5C block !! LIVE-TUNED !! - the console's Pia layer may
    require a well-formed system header (sysCommVer 21/22 + Switch nickname). If the console will not
    list a synthesized header, use mutate_beacon() with a captured real host header instead.

    `rfu_session_id` default 0x0002 = RFU_SERIAL_GAME (the friend-path serial). Other kwargs
    (trade_species/activity/has_card/language/version) flow to game_data_word()."""
    header = bytes(pia_header) if pia_header is not None else b"\x00" * PIA_HDR
    header = header[:PIA_HDR].ljust(PIA_HDR, b"\x00")
    record = build_record(trainer_id=trainer_id, name=name, rfu_session_id=rfu_session_id,
                          partner_info=partner_info, **game_data_kwargs)
    return header + b85_encode(record)


def mutate_beacon(captured_app_data, *, name=None, trainer_id=None, rfu_session_id=None,
                  **game_data_kwargs):
    """PRAGMATIC HW-A PATH: clone a REAL captured host `application_data` (keeping its known-good Pia
    header verbatim) and re-encode only the RFU record with the fields you override. Everything not
    overridden is preserved from the capture. `captured_app_data` = the bytes the join path's
    _dump_beacon logged for a genuine FRLG host. Returns new `application_data`."""
    from .transport import _b85_decode
    captured = bytes(captured_app_data)
    header = captured[:PIA_HDR]
    rec = bytearray(_b85_decode(captured[PIA_HDR:])[:RECORD_SIZE].ljust(RECORD_SIZE, b"\x00"))
    if trainer_id is not None:
        rec[0:2] = (trainer_id & 0xFFFF).to_bytes(2, "little")
    if name is not None:
        rec[2:10] = encode_name(name, width=8)
    if rfu_session_id is not None:
        rec[10:12] = (rfu_session_id & 0xFFFF).to_bytes(2, "little")
    if game_data_kwargs:
        rec[20:24] = game_data_word(**game_data_kwargs).to_bytes(4, "little")
    return header + b85_encode(bytes(rec))


def _selftest():
    from .transport import _b85_decode, _frlg_name
    rec = build_record(trainer_id=0x2288, name="EMU", rfu_session_id=0x0002, trade_species=277)
    assert len(rec) == RECORD_SIZE
    round_tripped = _b85_decode(b85_encode(rec))[:RECORD_SIZE]
    assert round_tripped == rec, (round_tripped.hex(), rec.hex())
    assert int.from_bytes(round_tripped[0:2], "little") == 0x2288
    assert _frlg_name(round_tripped[2:10]) == "EMU"
    assert int.from_bytes(round_tripped[10:12], "little") == 0x0002
    assert int.from_bytes(round_tripped[20:24], "little") >> 16 == 277
    print("beacon self-test OK (record round-trips through _b85_decode)")


if __name__ == "__main__":
    _selftest()
