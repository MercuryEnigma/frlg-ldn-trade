# SKILLS.md — frlg-ldn-trade development guide

This repository emulates FireRed/LeafGreen's GBA Wireless Adapter protocol through the Nintendo
Switch LDN/Pia bridge. It contains both a joiner and a leader; there is no repository-wide rule
that “Linux is the child.” Determine the entry point and role before applying any protocol rule.

For operator setup and commands, see [`README.md`](./README.md). For source-level behavior, use the
checked-in [`pokefirered/`](./pokefirered/) decomp as the primary oracle. Decomp references such as
`link_rfu_2.c:1813` refer to real files in this workspace.

## 1. Roles and current status

| Entry point | Linux role | Switch role | Activity | Hardware status |
|---|---|---|---|---|
| `frlgtrade.py` | LDN/RFU child, multiplayer id 1 | Parent/leader, id 0 | Direct Corner trade follower | Working |
| `frlgtrade_host.py` | LDN/RFU parent/leader, id 0 | Child/follower, id 1 | Direct Corner trade leader | Working end to end |
| `frlgmg_host.py` | LDN/RFU parent/leader, id 0 | Child/Mystery Gift client, id 1 | Wonder Card server through **Friend** | Working end to end (default no-item Celebi card; `--item ID` is optional) |
| `joyspot_probe.py` | LDN advertiser only | Scanner | Strict JoySpot discovery research | Friend control works; Wireless Communication does not |

Role-sensitive language must be qualified. For example, `TradeEngine` is the child/follower trade
engine, while `HostTradeEngine` is the parent/leader trade engine. `LiveTransport` joins a network;
`HostTransport` creates one. `NIReceiver` and `RFULeader` model opposite halves of the RFU link.

Use these evidence labels in comments and reviews:

- **Live-proven**: observed working against the Switch.
- **Capture-proven**: decoded from an actual JSONL/PCAP, but not necessarily accepted in every flow.
- **Decomp-backed**: follows checked-in game/library source.
- **Offline-tested**: verified by Python and/or C/reference models only; do not call it
  hardware-proven. The current Mystery Gift suites are Python models/transcriptions; the planned
  compiled-decomp differential oracle has not been implemented.
- **Hypothesis**: a bridge mapping or timing choice that still needs a controlled experiment.

Do not fix an application-layer stall by adding retries until the native state machine has been read
end to end. Repeating an RFU command can start a second operation rather than recover the first.

## 2. End-to-end paths

### 2.1 Linux joins a Switch-hosted trade

`frlgtrade.py` drives this path:

1. `LiveTransport` scans for and joins the Switch LDN network.
2. `PiaCrypto` derives the session key from the SSID.
3. `ConnectionManager` answers Net `0x11`, joins the Pia Session, finalizes, and handles RTT.
4. `Sim` opens Pia Reliable, sends RFU `C`, receives `A`, completes both NI directions, and then
   emits one child UNI command per VBlank.
5. `TradeEngine`, `LinkState`, and `BarrierResponder` act as multiplayer id 1/right-seat follower.
6. Received Pokémon are saved through `mon.py`; the Switch leader owns the room exit.

This entire path is child-specific. Its rolling tag, block owner, held-key, barrier, and leader
broadcast rules must not be copied into a parent engine without translating the role.

### 2.2 Linux hosts a Direct Corner trade

`frlgtrade_host.py` drives this path:

1. `HostApplication` loads the configured party and builds the per-run host stack.
2. `HostTransport` creates the LDN network; `BeaconInjector` maintains the required 802.11 beacons.
3. `HostPeerProtocol` originates Net `0x11`, accepts the Session join, handles RTT, encrypts Pia,
   batches Reliable output, and publishes the active discovery property.
4. `HostSession` composes `HostReliableSession`, `RFULeader`, and `HostTradeEngine`.
5. `RFULeader` accepts `C`, receives child NI, sends join-status NI, then emits a two-row parent UNI
   table: row 0 is Linux's command and row 1 reflects the child's command.
6. `HostTradeEngine` performs room entry, party exchange, selection, trade, save barriers, menu exit,
   room exit, and the activity-level close/grace sequence. `HostSession` emits the final close poll
   and asks `RFULeader` to build RFU `D` only when the engine requests disconnect.

This path is live-proven end to end. Preserve its ordering and timing while changing shared layers.

### 2.3 Linux hosts Mystery Gift through Friend

`frlgmg_host.py` reuses the host path through RFU UNI and injects `HostMysteryGiftEngine` instead of
`HostTradeEngine`:

1. `build_wonder_card_app_data` advertises `ACTIVITY_WONDER_CARD` (21).
2. The player selects Linux under **Mystery Gift → Wonder Cards → Friend**.
3. LDN, Pia, Reliable, RFU `C/A`, child NI, parent NI, and continuous UNI complete.
4. Both sides must finish `Task_PlayerExchange` before the game constructs `MysteryGiftClient`.
5. `MysteryGiftServer` then drives every client action through `MysteryGiftLink` messages.

This complete flow is live-proven: the Switch completed the LinkPlayer barrier, accepted the
MysteryGiftLink conversation, saved the card, closed cleanly, and executed the delivery script. The
shipped script uses `end`, not `endram`, so it remains available for later deliveryman interactions.
Offline tests remain the regression guard; there is still no readily available native Mystery Gift
trace or compiled-decomp differential oracle in the current tree.

### 2.4 Discovery-only JoySpot probe

`joyspot_probe.py` creates a fresh network and advertises one or all named candidates. It deliberately
does **not** start Pia, Reliable, RFU, or Mystery Gift. Strict **Wireless Communication** discovery
requires RFU serial `0x7F7D`; the tested Switch bridge exposes peers as normal game serial `0x0002`.
The Friend control is visible and joinable, while the strict candidates were not visible.

Do not put `0x7F7D` into discovery record bytes `[10:12]`: those bytes are the per-run RFU parent
session id and must match the parent's `A` response. Treat `joyspot_discovery.py` as experimental;
the production Mystery Gift path currently uses Friend.

## 3. Architecture and ownership

Shared wire stack, bottom-up:

```text
14-byte RFU command
  → emulator frame (J/C/A/K/T/D)
    → Pia Reliable protocol 10
      → Pia message tiling + optional zstd
        → AES-GCM
          → UDP/IPv4 on the LDN interface
```

Client/joiner spine:

```text
frlgtrade.py
  └─ LiveTransport + ConnectionManager + Sim
       └─ TradeEngine + LinkState + BarrierResponder
```

Host/leader spine:

```text
frlgtrade_host.py / frlgmg_host.py
  └─ HostApplication / MysteryGiftHostApplication       OS resources and run loop
       └─ HostPeerProtocol                              Net, Session, RTT, Pia framing
            └─ HostSession                              Reliable + RFU composition
                 ├─ HostReliableSession
                 ├─ RFULeader                           C/A, NI, UNI table, D
                 └─ activity engine
                      ├─ HostTradeEngine
                      └─ HostMysteryGiftEngine
```

Current compatibility detail: `HostSession.activity` is the injected activity. Its `.trade`
property returns that same object so older host code/tests continue to work. Consequently
`HostPeerProtocol` currently reads `session.trade.established` even for Mystery Gift; the name is
stale, but the runtime behavior is application-generic through the alias.

### Resource and state ownership

- `HostApplication`: PHY/key resolution, network/injector/tracer lifecycle, event loop, cleanup.
- `HostPeerProtocol`: Pia peer identity, nonce and packet-id counters, Net/Session/RTT, Reliable
  datagram batching, active property updates.
- `HostSession`: Reliable ordering/retransmission, RFU scheduling, one activity tick per UNI poll,
  close-poll-before-`D` ordering.
- `RFULeader`: RFU parent state only; no sockets, Pia crypto, or game activity.
- Activity engine: seven-word parent `gSendCmd`, child UNI consumption, activity-specific progress,
  close intent, and completion.
- `TrainerProfile`: discovery, Pia Session, LinkPlayer, trainer-card identity. Edit only
  `DEFAULT_TRAINER` for the emulated trainer.

## 4. Module map

### Shared protocol and data

- `frlgsim/transport.py`: `LiveTransport`, `ReplayTransport`, and `HostTransport`; radio preparation,
  LDN network lifecycle, sockets, and UDP parsing.
- `frlgsim/beacon.py`: base85/search-record codecs and constants reused by production host beacons
  and JoySpot research; its older inferred layouts are not all live-proven.
- `frlgsim/crypto.py`: Pia AES-GCM, SSID-derived key, GCM nonce, and zstd.
- `frlgsim/reliable.py`: Pia message tiling plus client and host Reliable state.
- `frlgsim/pia_connect.py`: shared Net/Session/RTT codecs plus the client-side
  `ConnectionManager`; `host_pia.py` reuses its codecs.
- `frlgsim/gbaframe.py`: emulator `57 <type> <size> <body>` framing.
- `frlgsim/rfu.py`: 14-byte RFU commands, opcodes, rolling child tag, LLSF, serializers/parsers.
- `frlgsim/ni.py`: NI primitives and game-data/status helpers used by both RFU roles.
- `frlgsim/block.py`: 12-byte RFU fragment send and receive machinery.
- `frlgsim/linkplayer.py`: 60-byte `LinkPlayerBlock` and 100-byte trainer card.
- `frlgsim/charmap.py`: Gen III text encoding; names are not ASCII.
- `frlgsim/mon.py`, `stats.py`, `basestats.py`: Pokémon file/wire data and party reconstruction.

### Client/joiner orchestration

- `frlgsim/sim.py`: client per-VBlank orchestrator.
- `frlgsim/trade.py`: follower trade and entry FSM.
- `frlgsim/linkstate.py`: child held-key/seat/exit behavior.
- `frlgsim/barrier.py`: child standby and close responder.

### Parent/host only

- `frlgsim/host_profile.py`: immutable human-readable trainer profile.
- `frlgsim/host_beacon.py`: captured-template discovery data and userspace beacon injector.
- `frlgsim/host_support.py`: host key-path and small setup helpers.
- `frlgsim/ldntrace.py`: optional JSONL transport diagnostics.
- `frlgsim/host_pia.py`: leader Net/Session/RTT/Pia peer implementation.
- `frlgsim/host_session.py`: host Reliable → RFU → activity adapter.
- `frlgsim/rfu_leader.py`: single-child RFU parent.
- `frlgsim/host_trade.py`: live-proven Direct Corner leader activity.
- `frlgsim/host_app.py`: trade-host runtime.

### Mystery Gift

- `frlgsim/host_mg_app.py`: single-recipient Friend-path host runtime.
- `frlgsim/host_mystery_gift.py`: player exchange, gift block transport, and close FSM.
- `frlgsim/mg_link.py`: MysteryGiftLink logical header/chunk/CRC layer.
- `frlgsim/mg_script.py`: exact client scripts and 96-byte game-data parser.
- `frlgsim/mg_server.py`: native server conversation and existing-card branches.
- `frlgsim/mystery_gift.py`: message ids, sizes, and CRC.
- `frlgsim/wonder_card.py`: 332-byte card and delivery RAM script builders.
- `frlgsim/joyspot_discovery.py`, `joyspot_probe.py`: strict discovery experiments, not the gift
  application protocol.

## 5. Critical wire facts

### Pia and Reliable

- Pia UDP port is `12345`; the encrypted header is 29 bytes.
- Session key: `AES_ECB(FRLG_GAME_KEY, ssid)`.
- GCM nonce: `(CRC32(ssid[1:16]) XOR src_ip_be) || header_nonce`; the tag is truncated to 8 bytes.
- Hardware/mac80211 CCMP may be required on the MT7601U path. `--skip-encryption` disables LDN's
  duplicate software CCMP; it does not disable Pia AES-GCM. Other adapters may need the default.
- Host native-nonce mode is one session-wide increasing nonce sequence.
- Reliable `flagsA`: `0x07` complete DATA, `0x0F` opening DATA, `0x00` control ACK.
- Bulk SACK bit `i` means sequence `ack_id + i`, not `ack_id + 1 + i`. Native
  `ack_id=0xFFF7, mask=0x06` acknowledges `0xFFF8` and `0xFFF9`.
- Reliable must deduplicate and order DATA before calling `RFULeader.receive`.

### Emulator and RFU

- Emulator frame types: `J=0x4A`, `C=0x43`, `A=0x41`, `K=0x4B`, `T=0x54`, `D=0x44`.
- One RFU command is 14 bytes / seven little-endian words.
- Parent RFU ids observed natively are `0xF1xx`; their raw little-endian bytes are `xx f1`. The
  exact same raw bytes must appear in the discovery record and the parent-id field of `A`. The
  later `D` contains the child's connect id echoed from `C`, not the parent id.
- Child rolling `childSendCmdId` is bits 5–7 of word 0's low byte and advances on non-idle child
  commands. It is transport metadata, not part of the application opcode/index.
- In parent UNI, row 0 is the parent's current command and row 1 is the reflected child command.
- Before copying a child command into row 1, native `RfuMain2_Parent` validates the rolling id and
  clears it with `childRecvBuffer[i][0] &= 0x1F` (`pokefirered/src/link_rfu_2.c`).
- Parent and child `T` frame layouts are direction-specific; do not reuse byte offsets blindly.
- Call the RFU leader once for each unique in-order Reliable payload and call `tick()` at most once
  per VBlank. Queue at most one new parent RFU frame from that tick.

### Blocks and LinkPlayer

- `SEND_BLOCK_INIT=0x8800`; `SEND_BLOCK=0x8900`; fragments are 12 bytes.
- `SEND_BLOCK_REQ=0xA100`; the request type is word 1.
- A 200-byte request uses 17 fragments. Native parent block send exposes **four** INIT polls total,
  then fragments 0 through 16. Native code reads bytes 192–203 from the backing buffer; the Python
  host explicitly produces zeros for bytes 200–203, as confirmed in the latest outbound trace.
- `LinkPlayerBlock` is 60 bytes: 16-byte magic, 28-byte player, 16-byte magic. Both magic fields
  must contain NUL-terminated `GameFreak inc.` or `LinkPlayerFromBlock` selects `CB2_LinkError`.
- Current `linkplayer.parse_block()` checks only the first 14 bytes of each magic and therefore does
  not enforce the ROM's terminating-NUL requirement. Do not treat that weaker diagnostic parser as
  proof that an arbitrary inbound block would pass native `strcmp`.
- `RECV_STATE_FINISHED` is not `RECV_STATE_READY`. Only `Rfu_ResetBlockReceivedFlag` clears the
  received flag and returns the receiver to READY.

## 6. `Task_PlayerExchange`: current Mystery Gift barrier

The Switch runs `Task_PlayerExchange` at `pokefirered/src/link_rfu_2.c:1813`. Read the entire task
and `RfuHandleReceiveCommand` before changing `HostMysteryGiftEngine`.

Native task progression:

1. Case 0 waits until every receive slot is READY, resets block flags, and fills
   `gBlockSendBuffer` with the local LinkPlayer block.
2. Cases 1–2 obtain player ids/count. The parent sends `SEND_PLAYER_IDS`; the child waits for it.
3. Case 3: parent sends one `SEND_BLOCK_REQ`; child advances immediately.
4. Case 4 waits until both player slots are `FINISHED` and their `blockReceived` flags are true.
5. Case 5 parses **both** blocks with `LinkPlayerFromBlock` and resets both receive slots.
6. Case 6 sets `gReceivedRemoteLinkPlayers`, allowing the Mystery Gift menu task to continue.

Consequences:

- A block arriving before case 0 may either make `AreAllPlayersReadyToReceive` fail or be discarded
  by `ResetBlockReceivedFlags`. A valid console LinkPlayer block proves case 0 has run because
  `LocalLinkPlayerToBlock` fills the console's send buffer there.
- `MoveSendCmdToRecv` clears the parent's `gSendCmd`, so a native `SEND_BLOCK_REQ` occupies one poll.
  `Rfu_InitBlockSend` rejects a repeat while a callback/send is active, but a later repeat can start
  an unwanted second transfer after the first completes. Do not continuously re-request once the
  console has started sending.
- Repeated child `SEND_BLOCK_INIT` is normal until row 1 reflects its own INIT. Nine INITs can simply
  mean nine polls of echo latency; do not classify that alone as a retransmission storm.
- The host's own LinkPlayer block must be sent only when the Switch task can receive it, and all 17
  fragments must be observable in order in outbound diagnostics.

### Resolved Stage-4 incident: row-1 tags and LinkPlayer ordering

The pre-success trace showed a byte-correct host block and a valid console block (`GREEN`), but
`RFULeader` reflected tagged child fragments such as `0x8980`, `0x89A1`, and `0x89C2`. Native
`RfuMain2_Parent` clears bits 5–7 of byte 0 before publishing `gRecvCmds`; leaving those tags in row
1 corrupts the Switch's self-receive block indices. The leader now normalizes every child command
before both queueing its row-1 echo and delivering it to the activity engine. Tagged-fragment tests
cover all 17 LinkPlayer fragments.

The Mystery Gift engine now follows observable task transitions rather than the removed
`post_join_status_quiet_frames` and block-request retry timers:

1. Send an eight-poll `SEND_PLAYER_IDS` burst, then exactly one `SEND_BLOCK_REQ`.
2. If the console proves it still has multiplayer id 0, repeat only the id burst.
3. Wait for a **completed, valid** console LinkPlayer block; an INIT alone can expose stale
   pre-case-0 data.
4. Send the 200-byte host block (four INIT polls, fragments 0–16), then retain any early standby
   until that transfer finishes.
5. On `READY_EXIT_STANDBY`, begin the Mystery Gift client conversation.

This sequence cleared the live barrier and completed the card delivery. Do not reintroduce repeated
block requests without a new source-backed attempt boundary.

## 7. Mystery Gift protocol after player exchange

Once `gReceivedRemoteLinkPlayers` is set, the menu constructs `MysteryGiftClient` and waits for
server ident 16. The no-existing-card success flow is:

| Direction | Ident | Payload |
|---|---:|---|
| Host → Switch | 16 `CLIENT_SCRIPT` | 32-byte SendGameData script |
| Switch → Host | 17 `GAME_DATA` | 96 bytes |
| Host → Switch | 16 `CLIENT_SCRIPT` | 48-byte SaveCard script |
| Host → Switch | 22 `CARD` | 332-byte Wonder Card |
| Host → Switch | 25 `RAM_SCRIPT` | 1024 bytes: script prefix then zero padding |
| Switch → Host | 20 `READY_END` | 1024 bytes; validate header/CRC, ignore contents |

`MysteryGiftLink` rules:

- Header is `<u16 ident, u16 crc, u16 size>` little-endian in its own SendBlock.
- Payload follows in separate SendBlocks of at most 252 bytes.
- Maximum logical payload is 1024 bytes.
- A requested send size of zero means **1024 bytes**, never an empty message.
- CRC covers the complete declared/padded buffer.
- Wrong ident, oversize, or bad final CRC is fatal. A missing/truncated transfer waits until the
  surrounding watchdog/link failure; it is not an immediate codec rejection.

Server branches mirror `mystery_gift_scripts.c`:

- No card: send the configured card and RAM script.
- Same card flag: report already owned, then receive READY_END.
- Different card: ask whether to toss it; false means replace/continue, true means cancel.
- Invalid game data: reject cleanly.
- Every terminal branch receives READY_END before close.

The current default gift is defined in `wonder_card.py` (presently a no-item level-50 Celebi) and is configurable
through `frlgmg_host.py`. The card/server/link code is covered by Python offline tests, but those
tests are not an independent compiled-decomp conformance oracle, and none of these bytes has yet run
on hardware because `Task_PlayerExchange` is still blocking stage 5.

## 8. Trade and identity invariants

- `DEFAULT_TRAINER` in `host_profile.py` is the host's single identity source for discovery, Pia,
  LinkPlayer, and trainer card. Gen III name fields use `charmap.py`, not ASCII.
- Host name padding is all `0xFF`; do not replace it with NUL padding. The default display identity
  is `EMU`, TID `0x8822`, SID `0x47ED`, LeafGreen.
- The host trade engine is the leader. It owns `SET_MONS`, `START`, confirmation, save barriers,
  cancel, held-key room movement, close, and `D` timing.
- The client trade engine is the follower. It must never emit leader broadcast opcodes.
- Party data is always three 200-byte blocks representing six 100-byte slots; a party of 1–6 mons
  is encoded by zeroing unused slots, not by changing the number or size of party blocks.
- Offered slots for multiple trades must be distinct because each received mon replaces its offered
  slot.
- Keep host traffic alive until the Switch has completed close/departure handling; the live-proven
  trade path deliberately includes a grace interval.

## 9. Hardware/runtime invariants

- Live operation needs root, `prod.keys`, the bundled host-capable LDN checkout, and an appropriate
  Wi-Fi PHY. NetworkManager/wpa_supplicant must not seize `ldn`, `ldn-mon`, `ldn-tap`, or
  `ldnclient`.
- Host and joiner use different role-specific communication-id defaults. The host value is
  live-proven; the joiner value is an older compatibility default. Do not “unify” them without a
  controlled LDN visibility test.
- Host `max_participants` remains 6 even for one Switch. It controls the Net `0x11` station-array
  shape; a live Mystery Gift run with 2 joined LDN but received no Pia response.
- A single activity tick is a soft-real-time VBlank operation. If the Reliable send window is full,
  the activity stops advancing and the Switch can time out; diagnose why the window is full rather
  than raising it blindly.
- Keep logs milestone-oriented by default. High-frequency per-frame diagnostics belong behind
  verbose/capture output, with counters and first-occurrence summaries in the normal log.
- Generated `.log`, `.jsonl`, `.pcap`, and `.pcapng` files are diagnostics, not fixtures unless an
  exact minimal byte fixture is deliberately extracted into a test.

## 10. Testing and change workflow

Before changing shared RFU/Pia/block code:

1. State which endpoint is parent and which is child.
2. Cite the decomp path and function that owns the behavior.
3. Check whether the claim is live-, capture-, decomp-, or offline-backed.
4. Add a focused regression using wire-shaped bytes. For rolling tags, use `SlotBuilder`; an
   untagged synthetic command cannot catch parent normalization bugs.
5. Run the focused test, then every script under `tests/`.
6. Preserve the live-proven trade-host behavior while advancing Mystery Gift.

Run the whole script-style suite from the repository root:

```bash
set -e
for test in tests/test_*.py; do
    echo "== $test =="
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./.venv/bin/python "$test"
done
```

Important coverage groups:

- Pia/host runtime: `test_pia_host_session.py`, `test_host_app_runtime.py`,
  `test_host_profile_pia_helpers.py`.
- Reliable/RFU: `test_host_reliable.py`, `test_rfu_leader.py`,
  `test_host_transport_latency.py`.
- Trade: `test_host_trade_engine.py`, `test_host_end_to_end.py`.
- Mystery Gift: `test_mystery_gift_offline.py`, `test_mystery_gift_flow.py`,
  `test_mystery_gift_end_to_end.py`, `test_mystery_gift_host_wiring.py`.
- Discovery: `test_joyspot_discovery.py`, `test_ldn_host_data.py`.

Offline E2E success does not prove Switch timing or Sloop translation. Localize live failures by
stage and counters first; request an air PCAP only when JSONL plus the milestone log cannot identify
the boundary.

## 11. Run commands

Direct Corner host:

```bash
sudo -E ./.venv/bin/python -u frlgtrade_host.py --live \
  --phy phy3 --skip-encryption --native-nonce-sequence --session-response-first \
  -o output.pk3 Lola.pk3 Lola.pk3
```

Mystery Gift Friend-path host:

```bash
sudo -E ./.venv/bin/python -u frlgmg_host.py --live \
  --phy phy3 --skip-encryption --native-nonce-sequence --session-response-first
```

Switch workflow for the gift path: unlock Mystery Gift, choose **Wonder Cards → Friend**, select
the Linux trainer, and allow the card/save/close sequence to finish. Then visit the second floor of
a Pokémon Center to receive the default Celebi. Do not use Wireless Communication for
`frlgmg_host.py`; that menu is the separate strict JoySpot discovery problem.
