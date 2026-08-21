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
ITEM_MASTER_BALL = 1               # [include/constants/items.h:5]
ITEM_NONE = 0                      # [include/constants/items.h:4]
SPECIES_RAIKOU = 243               # [include/constants/species.h:250]
SPECIES_ENTEI = 244
SPECIES_SUICUNE = 245
SPECIES_CLAYDOL = 319              # [include/constants/species.h:328] - Wonder Card icon
OBJ_EVENT_GFX_ENTEI = 141          # overworld sprite gfx [include/constants/event_objects.h:147-149]
OBJ_EVENT_GFX_SUICUNE = 142
OBJ_EVENT_GFX_RAIKOU = 143
DIR_WEST = 3                       # [include/constants/global.h:112]

# Which legendary beast appears, by the player's starter. VAR_STARTER_MON [vars.h:98] is
# 0:Bulbasaur, 1:Squirtle, 2:Charmander (verified against the rival-starter scripts).
STARTER_BEASTS = {
    0: (SPECIES_SUICUNE, OBJ_EVENT_GFX_SUICUNE),   # Bulbasaur -> Suicune
    1: (SPECIES_ENTEI, OBJ_EVENT_GFX_ENTEI),       # Squirtle  -> Entei
    2: (SPECIES_RAIKOU, OBJ_EVENT_GFX_RAIKOU),     # Charmander-> Raikou
}

# --- event-script opcodes used by the delivery script [asm/macros/event.inc] ------------------
_OP_END = 0x02
_OP_CALLSTD = 0x09              # callstd <function:u8>
_OP_ENDRAM = 0x0D              # RAM-script terminator: ClearRamScript + StopScript [scrcmd.c:262]
_OP_SETVAR = 0x16              # setvar <dest:u16> <value:u16>
_OP_ADDVAR = 0x17              # addvar <dest:u16> <value:u16>
_OP_COPYVAR = 0x19            # copyvar <dest:u16> <src:u16>
_OP_SETVAR_OR_COPY = 0x1A      # setorcopyvar <dest:u16> <src:u16>
_OP_COMPARE_VAR_TO_VALUE = 0x21  # compare <var:u16> <value:u16> -> ctx->comparisonResult
_OP_SPECIAL = 0x25            # special <index:u16>: call a native function from data/specials.inc
_OP_DELAY = 0x28              # delay <frames:u16>
_OP_SETFLAG = 0x29             # setflag <flag:u16>
_OP_GETPLAYERXY = 0x42        # getplayerxy <x_var:u16> <y_var:u16> -> writes player's live map coords

# FRLG's Quest Log recorder desyncs the field script across a scripted battle; every stock
# dowildbattle script (Electrode/Zapdos) calls this right after to re-cut the recording so the
# script resumes. Omitting it is why a post-battle giveitem silently never runs. [data/specials.inc]
SPECIAL_QUESTLOG_CUTRECORDING = 392
_OP_FACEPLAYER = 0x5A
_OP_WAITMESSAGE = 0x66        # waitmessage: block until the message box finishes printing
_OP_CLOSEMESSAGE = 0x68
_OP_LOCK = 0x6A
_OP_RELEASE = 0x6C
_OP_WAITBUTTONPRESS = 0x6D
_OP_CREATEVOBJECT = 0xAA       # createvobject <gfx:u8> <id:u8> <x:u16> <y:u16> <elev:u8> <dir:u8>
_OP_SETWILDBATTLE = 0xB6       # setwildbattle <species:u16> <level:u8> <held_item:u16> -> CreateScriptedWildMon
_OP_DOWILDBATTLE = 0xB7        # dowildbattle: StartScriptedWildBattle + ScriptContext_Stop; resumes after battle
_OP_SETVADDRESS = 0xB8         # setvaddress <ptr:u32>: RAM-script relocation base for the v* commands
_OP_VGOTO_IF = 0xBB            # vgoto_if <condition:u8> <dest:u32>: goto_if via the setvaddress base
_OP_VMESSAGE = 0xBD            # vmessage <text:u32>: message using the setvaddress-relative address

_CONDITION_EQ = 1              # sScriptConditionTable row "=" (jump when comparisonResult == equal)
_CONDITION_LT = 0              # sScriptConditionTable row "<"
VAR_ALTERING_CAVE_WILD_SET = 0x4024  # selects the Altering Cave encounter header [vars.h:71]
NUM_ALTERING_CAVE_TABLES = 9   # base Zubat table plus the eight event species tables
_VAR_STARTER_MON = 0x4031      # [include/constants/vars.h:98] 0:Bulbasaur 1:Squirtle 2:Charmander
_VAR_PLAYER_X = 0x8004          # scratch vars for the player's live coordinates (getplayerxy)
_VAR_PLAYER_Y = 0x8005
_VAR_FAR_X = 0x8006             # scratch: player's x + far_offset, for the first (right-side) Raikou

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


def build_battle_delivery_script(pre_items=(ITEM_LANSAT_BERRY,), species=SPECIES_RAIKOU,
                                 level=65, held_item=ITEM_NONE, post_items=(ITEM_LIECHI_BERRY,),
                                 one_shot=False):
    """Deliveryman script: hand over `pre_items`, start a scripted WILD battle vs `species` at
    `level`, then hand over `post_items` after the battle ends. `dowildbattle` stops the script
    and the engine resumes it right after the battle [scrcmd.c ScrCmd_dowildbattle], so the
    post-battle gift is given whether the player wins, runs, or catches the mon. This script is
    linear (no branches), so no setvaddress/relocation is needed. Ends with `end`
    (one_shot=False, default: repeatable every time you talk to the deliveryman) or `endram`
    (one_shot=True: the RAM script is cleared, so the gift retires after one delivery)."""
    def give(items):
        out = b""
        for it in items:
            out += _setorcopyvar(_VAR_0x8000, it) + _setorcopyvar(_VAR_0x8001, 1) \
                + bytes([_OP_CALLSTD, _STD_OBTAIN_ITEM])
        return out
    return bytes([_OP_LOCK, _OP_FACEPLAYER]) \
        + give(pre_items) \
        + bytes([_OP_SETWILDBATTLE]) + _u16(species) + bytes([level & 0xFF]) + _u16(held_item) \
        + bytes([_OP_DOWILDBATTLE]) \
        + give(post_items) \
        + bytes([_OP_RELEASE, _OP_ENDRAM if one_shot else _OP_END])


def build_raikou_battle_gift(level=65):
    """The battle payload: a Wonder Card + deliveryman script that gives a Lansat Berry, starts a
    wild RAIKOU battle at `level`, then gives a Liechi Berry. Returns (card_bytes_332, ram_script)."""
    flag_id = 1003
    card = build_wonder_card(
        flag_id=flag_id, icon_species=SPECIES_RAIKOU, id_number=0x5241494B,  # "RAIK"
        title="A LEGENDARY GIFT", subtitle="A special gift and a beastly surprise!",
        body=("A Lansat Berry, A Liechi Berry", "and then, what else is in store?"),
        footer1=" - MercuryEnigma")
    script = build_battle_delivery_script(species=SPECIES_RAIKOU, level=level)
    return card, script


# Text control codes [pokefirered charmap.txt]: {PLAYER}=FD 01 (player name), {NL}=FE (newline),
# {P}=FB (new paragraph: wait for A, clear the box), {SCROLL}=FA. Messages terminate with FF.
_MSG_TOKENS = {"PLAYER": b"\xFD\x01", "NL": b"\xFE", "P": b"\xFB", "SCROLL": b"\xFA"}


def _encode_message(text):
    """Encode a deliveryman message to FRLG text bytes (0xFF-terminated). `{...}` tokens expand
    per _MSG_TOKENS (e.g. {PLAYER}, {NL}, {P}); everything else goes through the game charset
    (charmap.encode). Unknown glyphs are dropped by charmap."""
    out = bytearray()
    i = 0
    while i < len(text):
        if text[i] == "{":
            j = text.index("}", i)
            tok = text[i + 1:j]
            if tok not in _MSG_TOKENS:
                raise ValueError(f"unknown message token {{{tok}}}")
            out += _MSG_TOKENS[tok]
            i = j + 1
        else:
            out += charmap.encode(text[i])
            i += 1
    return bytes(out) + b"\xFF"


def build_altering_cave_script():
    """Repeatable deliveryman script implementing
    ``VAR_ALTERING_CAVE_WILD_SET = (value + 1) % NUM_ALTERING_CAVE_TABLES``.

    The nine encounter headers are indexed 0..8. The script uses virtual addresses for its branch
    and embedded message, then ends with ``end`` so it remains installed for the next interaction.
    It deliberately does not set the Wonder Card receipt flag or use ``endram``.
    """
    message = _encode_message(
        "Thank you for using the{NL}MYSTERY GIFT system.{P}"
        "The wild POKEMON in{NL}ALTERING CAVE have changed!"
    )

    code = bytearray(bytes([_OP_SETVADDRESS]) + b"\x00\x00\x00\x00")
    code += bytes([_OP_ADDVAR]) + _u16(VAR_ALTERING_CAVE_WILD_SET) + _u16(1)
    code += bytes([_OP_COMPARE_VAR_TO_VALUE]) + _u16(VAR_ALTERING_CAVE_WILD_SET) \
        + _u16(NUM_ALTERING_CAVE_TABLES)
    code += bytes([_OP_VGOTO_IF, _CONDITION_LT])
    notify_fixup = len(code)
    code += b"\x00\x00\x00\x00"
    code += bytes([_OP_SETVAR]) + _u16(VAR_ALTERING_CAVE_WILD_SET) + _u16(0)

    notify_offset = len(code)
    code += bytes([_OP_LOCK, _OP_FACEPLAYER, _OP_VMESSAGE])
    message_fixup = len(code)
    code += b"\x00\x00\x00\x00"
    code += bytes([_OP_WAITMESSAGE, _OP_WAITBUTTONPRESS, _OP_RELEASE, _OP_END])

    message_offset = len(code)
    code[notify_fixup:notify_fixup + 4] = notify_offset.to_bytes(4, "little")
    code[message_fixup:message_fixup + 4] = message_offset.to_bytes(4, "little")
    return bytes(code) + message


def build_altering_cave_gift():
    """Wonder Card + persistent deliveryman script that cycles Altering Cave's wild Pokemon."""
    card = build_wonder_card(
        flag_id=1003, icon_species=41, id_number=0x43415645,  # "CAVE", Zubat icon
        title="ALTERING CAVE", subtitle="The rumors have changed!",
        body=("Talk to the delivery man", "to alter the cave again."),
        footer1="frlg-ldn-trade")
    return card, build_altering_cave_script()


def build_raikou_cutscene_script(level=65, delay_frames=30, one_shot=False):
    """A more "official" cutscene deliveryman script that ENDS with the battle, where the
    legendary beast is chosen by the player's STARTER (VAR_STARTER_MON):
        Bulbasaur -> Suicune,  Squirtle -> Entei,  Charmander -> Raikou.

      1. "Thank you for using the MYSTERY GIFT system."
      2. "You must be {PLAYER}. There is something here for you."
      3. give a Lansat Berry
      4. give a Liechi Berry
      5. the matching beast sprite appears right beside the player (createvobject), facing them
      6. "What is that? It looks like a Legendary Beast! Here, take this."
      7. give a MASTER BALL
      8. release, then a wild battle vs the matching beast (setwildbattle + dowildbattle) as finale

    createvobject's graphicsId and setwildbattle's species are immediate operands (not vars), so
    the beast can't be chosen with a variable -- we branch with `compare VAR_STARTER_MON` +
    `vgoto_if` into one of three self-contained blocks (each ends with `end`, so no fallthrough).
    Charmander (2) is the fallthrough default. Branch targets are relocation-safe: with
    `setvaddress` base 0, a vgoto_if/vmessage operand is just the byte offset of its label/text.

    All gifts are handed over BEFORE the battle on purpose: doing anything *after* a battle from
    the deliveryman's RAM script is unreliable -- catching the mon re-inits the field script
    context, so execution falls through to `gRamScriptRetAddr` (the cable-club colosseum code) and
    the player gets warped. So the battle is the last thing each block does; we `release` first so
    the player is free no matter how the battle ends. Message 2 uses {PLAYER} (FD 01), expanded to
    the trainer name at render time. Ends with `end` (repeatable) or `endram` (one_shot)."""
    msgs = [
        _encode_message("Thank you for using the{NL}MYSTERY GIFT system."),
        _encode_message("You must be {PLAYER}!{P}There is something here{NL}for you."),
        _encode_message("What is that? It looks{NL}like a Legendary Beast!{P}Here, take this."),
    ]

    def giveitem(item):
        return _setorcopyvar(_VAR_0x8000, item) + _setorcopyvar(_VAR_0x8001, 1) \
            + bytes([_OP_CALLSTD, _STD_OBTAIN_ITEM])

    code = bytearray()
    vmessage_fixups = []                                       # (operand_pos, msg_index)
    branch_fixups = []                                         # (operand_pos, label_name)
    labels = {}

    def vmessage(idx):
        code.append(_OP_VMESSAGE)
        vmessage_fixups.append((len(code), idx))
        code.extend(b"\x00\x00\x00\x00")
        code.extend(bytes([_OP_WAITMESSAGE, _OP_WAITBUTTONPRESS, _OP_CLOSEMESSAGE]))

    def vgoto_if_starter(value, label_name):                  # if VAR_STARTER_MON == value: goto label
        code.extend(bytes([_OP_COMPARE_VAR_TO_VALUE]) + _u16(_VAR_STARTER_MON) + _u16(value))
        code.extend(bytes([_OP_VGOTO_IF, _CONDITION_EQ]))
        branch_fixups.append((len(code), label_name))
        code.extend(b"\x00\x00\x00\x00")

    def beast_block(species, gfx):
        # legendary appears beside the player (x=playerX+1, y=playerY, facing west toward them)
        code.extend(bytes([_OP_CREATEVOBJECT, gfx & 0xFF, 0]) + _u16(_VAR_PLAYER_X)
                    + _u16(_VAR_PLAYER_Y) + bytes([3, DIR_WEST]))
        code.extend(bytes([_OP_DELAY]) + _u16(delay_frames))
        vmessage(2)                                            # "What is that? ... take this."
        code.extend(giveitem(ITEM_MASTER_BALL))
        code.extend(bytes([_OP_RELEASE]))                      # free the player before the battle
        code.extend(bytes([_OP_SETWILDBATTLE]) + _u16(species) + bytes([level & 0xFF]) + _u16(ITEM_NONE))
        code.extend(bytes([_OP_DOWILDBATTLE]))                 # the wild battle is the finale
        code.extend(bytes([_OP_ENDRAM if one_shot else _OP_END]))

    code += bytes([_OP_SETVADDRESS]) + b"\x00\x00\x00\x00"     # setvaddress 0 (virtual base)
    code += bytes([_OP_LOCK, _OP_FACEPLAYER])
    vmessage(0)                                                # 1: "Thank you for using..."
    vmessage(1)                                                # 2: "You must be {PLAYER}..."
    code += giveitem(ITEM_LANSAT_BERRY)                        # 3: Lansat Berry
    code += giveitem(ITEM_LIECHI_BERRY)                       # 4: Liechi Berry
    code += bytes([_OP_GETPLAYERXY]) + _u16(_VAR_PLAYER_X) + _u16(_VAR_PLAYER_Y)
    code += bytes([_OP_ADDVAR]) + _u16(_VAR_PLAYER_X) + _u16(1)   # one tile to the player's right (east)

    # 5-8, chosen by starter. Charmander (VAR_STARTER_MON == 2) is the fallthrough default.
    vgoto_if_starter(0, "suicune")                            # Bulbasaur -> Suicune
    vgoto_if_starter(1, "entei")                              # Squirtle  -> Entei
    beast_block(*STARTER_BEASTS[2])                            # Charmander-> Raikou (default)
    labels["suicune"] = len(code); beast_block(*STARTER_BEASTS[0])
    labels["entei"] = len(code); beast_block(*STARTER_BEASTS[1])

    # Patch message offsets (text sits right after the code) and branch targets (label offsets);
    # with virtual base 0 each operand is simply the absolute byte offset within the script.
    code_len = len(code)
    offsets, at = [], code_len
    for m in msgs:
        offsets.append(at)
        at += len(m)
    for pos, idx in vmessage_fixups:
        code[pos:pos + 4] = (offsets[idx] & 0xFFFFFFFF).to_bytes(4, "little")
    for pos, name in branch_fixups:
        code[pos:pos + 4] = (labels[name] & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(code) + b"".join(msgs)


def build_raikou_cutscene_gift(level=65):
    """Wonder Card + the "official" cutscene deliveryman script. Gives two berries + a Master Ball
    and ends in a wild battle vs the legendary beast matching the player's starter (Bulbasaur ->
    Suicune, Squirtle -> Entei, Charmander -> Raikou). Returns (card_bytes_332, ram_script_bytes)."""
    flag_id = 1003
    card = build_wonder_card(
        flag_id=flag_id, icon_species=SPECIES_CLAYDOL, id_number=0x42454153,  # "BEAS"
        title="LEGENDARY BEAST", subtitle="A shocking encounter!",
        body=("Meet the delivery man for", "berries and a beastly battle!"),
        footer1="frlg-ldn-trade")
    return card, build_raikou_cutscene_script(level=level)


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
