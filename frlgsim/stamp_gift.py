"""A STAMP-type Mystery Gift: a Wonder Card with two stamps (Solrock + Lunatone) and a
deliveryman RAM script whose reward scales with how many stamps the player has collected.

Reward tiers (this file's whole point):
  >= 1 stamp  -> a Lansat Berry
  == 2 stamps -> a Liechi Berry as well
and the script ends with `end` (NOT `endram`), so the RAM script is never cleared and the
deliveryman hands the berries out every time you talk to him (a repeatable faucet, not a
one-shot gift).

How the script reads the stamp count (the interesting part)
-----------------------------------------------------------
Stamps are NOT stored in the WonderCard; they live in `struct WonderCardMetadata`
(gSaveBlock1Ptr->mysteryGift.cardMetadata [include/global.h:671]). A stamp slot counts only
when BOTH halves are nonzero (GetNumStampsInMetadata [mystery_gift.c:260]):
    stampData[STAMP_SPECIES][i] && stampData[STAMP_ID][i].
The event-script way to read the live count is the same one FRLG's own stamp-card message
script uses [data/mystery_event_msg.s MysteryEventScript_StampCard]:
    setorcopyvar VAR_RESULT, GET_NUM_STAMPS   ; selector 0
    specialvar   <var>, GetMysteryGiftCardStat ; special #390 -> GetNumStampsInSavedCard()

How the script branches without knowing where it runs (the other interesting part)
----------------------------------------------------------------------------------
A RAM script executes in place inside SaveBlock1 (at gSaveBlock1.ramScript.data.script), so we
don't know its absolute address at authoring time and can't bake in self-pointers for `goto`.
FRLG solves this for downloaded scripts with `setvaddress` + the `v*` commands
[src/scrcmd.c:171-210]:
    setvaddress P sets sAddressOffset = P - &(setvaddress opcode)
    vgoto_if T   jumps to T - sAddressOffset  ==  runtime_start + (T - P)
So if we pick a virtual base P = 0 and give every branch target T = its byte offset in the
script, `vgoto_if` lands at runtime_start + offset regardless of where the buffer really is.
That's why this script uses `vgoto_if` (0xBB) instead of `goto_if` (0x06).

The console wraps these raw bytes in `struct RamScriptData` and checksums it for us; we only
supply the body (<= 995 B). See save_inject.build_ram_script_struct / gift_to_bin for delivery.
"""

from .mystery_gift import CARD_TYPE_STAMP, crc16
from .wonder_card import (
    build_wonder_card, WONDER_CARD_SIZE, ITEM_LANSAT_BERRY, ITEM_LIECHI_BERRY,
)

# --- species [include/constants/species.h] ---------------------------------------------------
SPECIES_LUNATONE = 348
SPECIES_SOLROCK = 349

# --- event-script opcodes [asm/macros/event.inc] ---------------------------------------------
OP_END = 0x02
OP_CALLSTD = 0x09                  # callstd <func:u8>
OP_SETORCOPYVAR = 0x1A             # setorcopyvar <dest:u16> <src:u16>
OP_COMPARE_VAR_TO_VALUE = 0x21     # compare <var:u16> <value:u16>  -> ctx->comparisonResult
OP_SPECIALVAR = 0x26               # specialvar <out:u16> <special:u16>
OP_FACEPLAYER = 0x5A
OP_LOCK = 0x6A
OP_RELEASE = 0x6C
OP_SETVADDRESS = 0xB8              # setvaddress <ptr:u32> (RAM-script relocation base)
OP_VGOTO_IF = 0xBB                 # vgoto_if <condition:u8> <dest:u32>

# --- script operands -------------------------------------------------------------------------
VAR_RESULT = 0x800D                # gSpecialVar_Result; also the GetMysteryGiftCardStat selector
VAR_0x8000 = 0x8000                # giveitem: item id
VAR_0x8001 = 0x8001                # giveitem: amount
VAR_STAMP_COUNT = 0x8008           # scratch var holding the read-back stamp count
STD_OBTAIN_ITEM = 0                # gStdScripts index: "obtained the {ITEM}!" + fanfare
GET_NUM_STAMPS = 0                 # GetMysteryGiftCardStat selector [constants/mystery_gift.h:4]
SPECIAL_GET_MYSTERY_GIFT_CARD_STAT = 390    # index of GetMysteryGiftCardStat in data/specials.inc
CONDITION_LESS_THAN = 0            # sScriptConditionTable row 0 == "<" [scrcmd.c:65]

VSCRIPT_BASE = 0                   # virtual base P; branch targets are plain byte offsets

DEFAULT_TIERS = ((1, ITEM_LANSAT_BERRY), (2, ITEM_LIECHI_BERRY))

# --- struct WonderCardMetadata layout [include/global.h:671] ---------------------------------
MAX_STAMP_CARD_STAMPS = 7
STAMP_SPECIES = 0                  # stampData first index [constants/mystery_gift.h:37]
STAMP_ID = 1
WONDER_CARD_METADATA_SIZE = 4 * 2 + 2 * MAX_STAMP_CARD_STAMPS * 2   # 4 u16 stats/icon + [2][7] u16 = 36


def _u16(v):
    return (v & 0xFFFF).to_bytes(2, "little")


def _setorcopyvar(dest, src):
    return bytes([OP_SETORCOPYVAR]) + _u16(dest) + _u16(src)


def _giveitem(item, amount=1):
    """The `giveitem` macro: two setorcopyvars + `callstd STD_OBTAIN_ITEM` (12 B)."""
    return _setorcopyvar(VAR_0x8000, item) + _setorcopyvar(VAR_0x8001, amount) \
        + bytes([OP_CALLSTD, STD_OBTAIN_ITEM])


def build_stamp_delivery_script(tiers=DEFAULT_TIERS, stamp_var=VAR_STAMP_COUNT):
    """Deliveryman script: read the stamp count, then for each (threshold, item) in `tiers`
    give one `item` iff stamp count >= threshold, and finish with `end` (repeatable). `tiers`
    should be ascending by threshold. Returns the raw script body (bytes)."""
    prologue = (
        bytes([OP_SETVADDRESS]) + (VSCRIPT_BASE & 0xFFFFFFFF).to_bytes(4, "little")
        + bytes([OP_LOCK, OP_FACEPLAYER])
        + _setorcopyvar(VAR_RESULT, GET_NUM_STAMPS)              # selector = GET_NUM_STAMPS
        + bytes([OP_SPECIALVAR]) + _u16(stamp_var) + _u16(SPECIAL_GET_MYSTERY_GIFT_CARD_STAT)
    )
    # Each tier block is compare(5) + vgoto_if(6) + giveitem(12) = 23 B, laid out back to back.
    tier_len = 5 + 6 + len(_giveitem(0))
    body = b""
    cur = len(prologue)
    for threshold, item in tiers:
        skip_target = VSCRIPT_BASE + cur + tier_len              # offset just past this giveitem
        block = (
            bytes([OP_COMPARE_VAR_TO_VALUE]) + _u16(stamp_var) + _u16(threshold)
            + bytes([OP_VGOTO_IF, CONDITION_LESS_THAN]) + (skip_target & 0xFFFFFFFF).to_bytes(4, "little")
            + _giveitem(item, 1)
        )
        assert len(block) == tier_len, len(block)
        body += block
        cur += tier_len
    return prologue + body + bytes([OP_RELEASE, OP_END])


def build_stamp_card(*, flag_id=1003, icon_species=SPECIES_SOLROCK, id_number=0x5354414d,  # "STAM"
                     title="STAMP RALLY", subtitle="Collect the stamps!",
                     body=("1 stamp: a Lansat Berry.", "2 stamps: a Liechi Berry too!"),
                     footer1="frlg-ldn-trade"):
    """A CARD_TYPE_STAMP Wonder Card with two stamp slots (maxStamps=2). The individual stamp
    species (Solrock/Lunatone) live in the WonderCardMetadata, not the card - see
    build_stamp_card_metadata. Passes ValidateWonderCard (type<3, maxStamps<=7)."""
    return build_wonder_card(
        flag_id=flag_id, icon_species=icon_species, id_number=id_number,
        card_type=CARD_TYPE_STAMP, max_stamps=2,
        title=title, subtitle=subtitle, body=body, footer1=footer1)


def build_stamp_card_metadata(stamp_species=(SPECIES_SOLROCK, SPECIES_LUNATONE),
                              icon_species=SPECIES_SOLROCK):
    """The 36-B `struct WonderCardMetadata` [global.h:671] pre-filled with the given stamps, so
    GET_NUM_STAMPS reads len(stamp_species). Each slot needs BOTH species and a nonzero stamp id
    to count (GetNumStampsInMetadata), so we assign ids 1..N. Returns (metadata_bytes, crc16).

    This is the piece that actually makes stamps *present*; the card + script alone read a count
    that is zero on a fresh save. Inject it at SaveBlock1 + 0x3434 (mysteryGift.cardMetadata),
    with its CRC at +0x3430, if you want the berries handed out without collecting stamps first."""
    if len(stamp_species) > MAX_STAMP_CARD_STAMPS:
        raise ValueError(f"{len(stamp_species)} stamps > {MAX_STAMP_CARD_STAMPS} max")
    species_row = list(stamp_species) + [0] * (MAX_STAMP_CARD_STAMPS - len(stamp_species))
    id_row = [i + 1 for i in range(len(stamp_species))] + [0] * (MAX_STAMP_CARD_STAMPS - len(stamp_species))
    out = bytearray()
    out += _u16(0)                      # battlesWon
    out += _u16(0)                      # battlesLost
    out += _u16(0)                      # numTrades
    out += _u16(icon_species)           # iconSpecies
    for v in species_row:               # stampData[STAMP_SPECIES][0..6]
        out += _u16(v)
    for v in id_row:                    # stampData[STAMP_ID][0..6]
        out += _u16(v)
    assert len(out) == WONDER_CARD_METADATA_SIZE, len(out)
    return bytes(out), crc16(out)


def build_stamp_gift():
    """The stamp-card payload: (332-B WonderCard, raw RAM-script body). Give Lansat at >=1 stamp,
    Liechi at 2 stamps, `end` (repeatable). Pair with build_stamp_card_metadata to have stamps."""
    return build_stamp_card(), build_stamp_delivery_script()


def _selftest():
    card, script = build_stamp_gift()
    assert len(card) == WONDER_CARD_SIZE, len(card)
    assert (card[8] & 0x3) == CARD_TYPE_STAMP and card[9] == 2, (card[8], card[9])

    # Exact expected bytecode: read stamp count, tiered giveitem, end.
    expected = bytes([
        0xB8, 0x00, 0x00, 0x00, 0x00,        # setvaddress 0
        0x6A,                                 # lock
        0x5A,                                 # faceplayer
        0x1A, 0x0D, 0x80, 0x00, 0x00,         # setorcopyvar VAR_RESULT, GET_NUM_STAMPS(0)
        0x26, 0x08, 0x80, 0x86, 0x01,         # specialvar VAR_0x8008, special #390
        0x21, 0x08, 0x80, 0x01, 0x00,         # compare VAR_0x8008, 1
        0xBB, 0x00, 0x28, 0x00, 0x00, 0x00,   # vgoto_if LESS_THAN, 0x28 (skip Lansat)
        0x1A, 0x00, 0x80, 0xAD, 0x00,         # setorcopyvar VAR_0x8000, LANSAT(173)
        0x1A, 0x01, 0x80, 0x01, 0x00,         # setorcopyvar VAR_0x8001, 1
        0x09, 0x00,                           # callstd STD_OBTAIN_ITEM
        0x21, 0x08, 0x80, 0x02, 0x00,         # compare VAR_0x8008, 2
        0xBB, 0x00, 0x3F, 0x00, 0x00, 0x00,   # vgoto_if LESS_THAN, 0x3F (skip Liechi)
        0x1A, 0x00, 0x80, 0xA8, 0x00,         # setorcopyvar VAR_0x8000, LIECHI(168)
        0x1A, 0x01, 0x80, 0x01, 0x00,         # setorcopyvar VAR_0x8001, 1
        0x09, 0x00,                           # callstd STD_OBTAIN_ITEM
        0x6C,                                 # release
        0x02,                                 # end (not endram -> repeatable)
    ])
    assert script == expected, script.hex(" ")
    assert len(script) == 65, len(script)
    # vgoto_if targets (u32 LE at offsets 24 and 47) equal the byte offset just past each
    # giveitem block: 0x28 (past Lansat) and 0x3F (past Liechi).
    assert int.from_bytes(script[24:28], "little") == 0x28, script.hex(" ")
    assert int.from_bytes(script[47:51], "little") == 0x3F, script.hex(" ")

    meta, meta_crc = build_stamp_card_metadata()
    assert len(meta) == WONDER_CARD_METADATA_SIZE
    # 2 stamps: species row Solrock(349)/Lunatone(348), id row 1/2 -> both nonzero -> counts as 2.
    assert int.from_bytes(meta[8:10], "little") == SPECIES_SOLROCK
    assert int.from_bytes(meta[10:12], "little") == SPECIES_LUNATONE
    assert int.from_bytes(meta[22:24], "little") == 1 and int.from_bytes(meta[24:26], "little") == 2
    print("stamp_gift self-test OK (card=%d B, script=%d B, metadata=%d B, metaCrc=0x%04X)"
          % (len(card), len(script), len(meta), meta_crc))


if __name__ == "__main__":
    _selftest()
