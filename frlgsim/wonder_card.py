"""Wonder Card + delivery RAM-script authoring (the Mystery Gift payload).

Two byte-exact builders for the gift we hand the console:

  build_wonder_card(...)  -> 332-byte `struct WonderCard` [include/global.h:655], the object the
                             console saves and shows in the Mystery Gift menu. Must pass
                             ValidateWonderCard [src/mystery_gift.c:191] or SaveWonderCard rejects it.

  build_delivery_ram_script(item, flag)
                          -> raw FRLG event-script bytecode run by the in-game deliveryman
                             ("Mystery Gift Man", CableClub_EventScript_MysteryGiftMan) via
                             scrcmd 0xCF `trywondercardscript` [src/scrcmd.c:275]. It gives the
                             player the item with the standard fanfare + message, sets the card's
                             receipt flag, then `endram` (clears the RAM script, making it one-shot).

The console wraps our RAM-script bytes in `struct RamScriptData` and computes the checksum itself
(CLI_SAVE_RAM_SCRIPT -> InitRamScript_NoObjectEvent, mystery_gift_client.c:230); we only supply the
script body. IMPORTANT: the script must NOT contain pointers into itself (the RAM script is run as a
normal event script with no pointer relocation), so we use only immediate-operand commands. `giveitem`
is safe because it resolves its message through `callstd` (a ROM standard-script INDEX, not a pointer).
"""

from . import charmap
from .mystery_gift import (
    CARD_TYPE_GIFT, SEND_TYPE_DISALLOWED, WONDER_CARD_FLAG_OFFSET, NUM_WONDER_CARD_FLAGS,
    FLAG_WONDER_CARD_UNUSED_1,
)

# --- struct WonderCard layout [include/global.h:655-669], packed, little-endian ---------------
WONDER_CARD_TEXT_LENGTH = 40        # per text field
WONDER_CARD_BODY_TEXT_LINES = 4
WONDER_CARD_SIZE = 332              # sizeof(struct WonderCard): 330 data + 2 pad (u32 idNumber align)

ITEM_LIECHI_BERRY = 168            # [include/constants/items.h:172]
ITEM_LANSAT_BERRY = 173            # [include/constants/items.h:177]

# --- event-script opcodes used by the delivery script [asm/macros/event.inc] ------------------
_OP_END = 0x02
_OP_CALLSTD = 0x09              # callstd <function:u8>
_OP_ENDRAM = 0x0D              # RAM-script terminator: ClearRamScript + StopScript [scrcmd.c:262]
_OP_SETVAR_OR_COPY = 0x1A      # setorcopyvar <dest:u16> <src:u16>
_OP_SETFLAG = 0x29             # setflag <flag:u16>
_OP_FACEPLAYER = 0x5A
_OP_LOCK = 0x6A
_OP_RELEASE = 0x6C

_VAR_0x8000 = 0x8000           # giveitem: item id
_VAR_0x8001 = 0x8001           # giveitem: amount
_STD_OBTAIN_ITEM = 0           # gStdScripts index [event_scripts.s:78] - "obtained the {ITEM}!" + fanfare


def _u16(v):
    return (v & 0xFFFF).to_bytes(2, "little")


def _setorcopyvar(dest, src):
    return bytes([_OP_SETVAR_OR_COPY]) + _u16(dest) + _u16(src)


def flag_for_flag_id(flag_id):
    """The receipt event flag a card flagId maps to (sReceivedGiftFlags[flagId-1000],
    mystery_gift.c:255). Only flagIds in [1000, 1000+20) are valid."""
    idx = flag_id - WONDER_CARD_FLAG_OFFSET
    if not (0 <= idx < NUM_WONDER_CARD_FLAGS):
        raise ValueError(f"card flagId {flag_id} out of range [1000, {1000 + NUM_WONDER_CARD_FLAGS})")
    # sReceivedGiftFlags[0..3] = AURORA(0x2A7), MYSTIC(0x2A8), OLD_SEA_MAP(0x2A9), UNUSED_1(0x2AA), ...
    return FLAG_WONDER_CARD_UNUSED_1 - 3 + idx


def build_delivery_ram_script(item=ITEM_LANSAT_BERRY, flag=None, flag_id=None, items=None):
    """Deliveryman script: lock, face player, `giveitem <item>, 1` per item (each with the standard
    fanfare + "obtained" message), set the receipt flag, release, `endram`. `endram` clears the RAM
    script so the gift is handed over exactly once (afterwards the deliveryman has nothing to run
    and the card self-retires). Pass either `flag` directly or `flag_id` (the card's flagId) to
    derive it. `items` (an iterable of item ids) overrides the single `item` - consecutive giveitem
    blocks are fine (each callstd waits for the player's A-press before the next)."""
    if flag is None:
        flag = flag_for_flag_id(flag_id) if flag_id is not None else (FLAG_WONDER_CARD_UNUSED_1)
    give = b""
    for it in (list(items) if items is not None else [item]):
        give += _setorcopyvar(_VAR_0x8000, it) \
            + _setorcopyvar(_VAR_0x8001, 1) \
            + bytes([_OP_CALLSTD, _STD_OBTAIN_ITEM])
    return bytes([_OP_LOCK, _OP_FACEPLAYER]) \
        + give \
        + bytes([_OP_SETFLAG]) + _u16(flag) \
        + bytes([_OP_RELEASE, _OP_ENDRAM])


def _card_text(s):
    """Encode one 40-byte Wonder Card text field (game charset, 0xFF-terminated + 0xFF pad)."""
    return charmap.encode(s or "", width=WONDER_CARD_TEXT_LENGTH, pad=0xFF)


def build_wonder_card(*, flag_id=1003, icon_species=1, id_number=0,
                      card_type=CARD_TYPE_GIFT, bg_type=0, send_type=SEND_TYPE_DISALLOWED,
                      max_stamps=0, title="", subtitle="", body=(), footer1="", footer2=""):
    """Build a 332-byte `struct WonderCard`. Defaults pass ValidateWonderCard: flagId != 0,
    type < 3, sendType in {0,1,2}, bgType < 8, maxStamps <= 7. flagId default 1003 -> the first
    unused receipt-flag slot (FLAG_WONDER_CARD_UNUSED_1), a clean one-shot for a custom gift.
    `body` is up to 4 lines of <=39 chars each."""
    if flag_id == 0:
        raise ValueError("flagId 0 is rejected by ValidateWonderCard")
    if not (0 <= card_type < 3 and 0 <= bg_type < 8 and send_type in (0, 1, 2) and 0 <= max_stamps <= 7):
        raise ValueError("WonderCard field out of the range ValidateWonderCard accepts")
    flag_for_flag_id(flag_id)  # validate flagId range early

    bitfield = (card_type & 0x3) | ((bg_type & 0xF) << 2) | ((send_type & 0x3) << 6)
    out = bytearray()
    out += _u16(flag_id)                       # +0
    out += _u16(icon_species)                  # +2
    out += (id_number & 0xFFFFFFFF).to_bytes(4, "little")  # +4
    out += bytes([bitfield, max_stamps & 0xFF])            # +8, +9
    out += _card_text(title)                   # +10
    out += _card_text(subtitle)                # +50
    body = list(body)[:WONDER_CARD_BODY_TEXT_LINES]
    for i in range(WONDER_CARD_BODY_TEXT_LINES):           # +90 .. +250
        out += _card_text(body[i] if i < len(body) else "")
    out += _card_text(footer1)                 # +250
    out += _card_text(footer2)                 # +290
    out += b"\x00\x00"                          # +330: 2 pad bytes (u32 alignment of struct)
    assert len(out) == WONDER_CARD_SIZE, len(out)
    return bytes(out)


def build_berry_gift():
    """The Milestone-3 payload: a Wonder Card + deliveryman RAM script that hands over one
    Lansat Berry and one Liechi Berry. Returns (card_bytes_332, ram_script_bytes)."""
    flag_id = 1003
    card = build_wonder_card(
        flag_id=flag_id, icon_species=1, id_number=0x4C414E53,  # "LANS"
        title="BERRY GIFT", subtitle="A gift for you",
        body=("Visit the Mystery Gift man", "to receive your berries."),
        footer1="frlg-ldn-trade")
    script = build_delivery_ram_script(items=(ITEM_LANSAT_BERRY, ITEM_LIECHI_BERRY),
                                       flag_id=flag_id)
    return card, script


def _selftest():
    card, script = build_berry_gift()
    assert len(card) == WONDER_CARD_SIZE, len(card)
    # flagId, iconSpecies, bitfield readback
    assert int.from_bytes(card[0:2], "little") == 1003
    bitfield = card[8]
    assert (bitfield & 0x3) == CARD_TYPE_GIFT
    assert ((bitfield >> 2) & 0xF) < 8
    assert ((bitfield >> 6) & 0x3) == SEND_TYPE_DISALLOWED
    assert card[9] == 0  # maxStamps
    # RAM script: exact bytecode for `giveitem LANSAT(173), 1; giveitem LIECHI(168), 1;
    # setflag 0x2AA; endram`.
    expected = bytes([0x6A, 0x5A,
                      0x1A, 0x00, 0x80, 0xAD, 0x00,
                      0x1A, 0x01, 0x80, 0x01, 0x00,
                      0x09, 0x00,
                      0x1A, 0x00, 0x80, 0xA8, 0x00,
                      0x1A, 0x01, 0x80, 0x01, 0x00,
                      0x09, 0x00,
                      0x29, 0xAA, 0x02,
                      0x6C, 0x0D])
    assert script == expected, script.hex()
    assert flag_for_flag_id(1003) == 0x2AA
    assert flag_for_flag_id(1000) == 0x2A7
    print("wonder_card self-test OK (card=%d B, ram_script=%d B)" % (len(card), len(script)))


if __name__ == "__main__":
    _selftest()
