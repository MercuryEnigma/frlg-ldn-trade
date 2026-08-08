# SKILLS.md — frlg-ldn-trade Python codebase

Working knowledge for developing the Python that lets a Linux PC trade Pokémon with
FireRed/LeafGreen (FRLG) running on a Nintendo Switch over local wireless (LDN). The PC
**joins** the console's link session as the wireless **CHILD** (never the host), emulates the
GBA Wireless Adapter (RFU) protocol tunneled through Nintendo's Pia/LDN transport, drives the
full link handshake + trade FSM, and writes each received mon as a `.pk3`/`.ek3`.

For *running* it, hardware requirements, and step-by-step usage see
[`README.md`](./README.md). This document is about the code.

> Scope: the `frlgsim/` package (18 modules) + `frlgtrade.py`. The `pokefirered/` decomp is a
> reference only — most modules cite `file.c:line` from it. Do not treat those citations as
> files in this repo.

---

## 1. End-to-end flow

CLI invocation → LDN join → Pia connection → RFU emulation → link handshake → trade → `.pk3`:

1. **CLI** (`frlgtrade.py:main`): parse 1..6 party `.pk3`/`.ek3` files, `--trades N`, seat
   options. `--live` (join a real Switch) or `--replay CAPTURE` (offline). Hard-fails if
   `zstandard` is missing (the host's Pia messages are zstd-compressed; without it nothing parses).
2. **LDN join** (`transport.LiveTransport.start`): free the radio, scan with kinnay's `ldn`
   library on the chosen phy, join the FRLG network, learn our/host IP + MAC, set up a UDP TX
   socket (`SO_BROADCAST`) and an `AF_PACKET` RX socket on the `169.254.x` link-local subnet.
3. **Pia session key** (`crypto.PiaCrypto(t.ssid)`): derive the AES-GCM session key from the
   LDN SSID.
4. **S0 connection handshake** (`pia_connect.ConnectionManager`, driven by `sim.Sim`): answer the
   host's Net `0x11` connection request → `0x12` response → Session(13) join → finalize → RTT. The
   host does not register us as a peer (no in-game "OK" prompt) until this completes.
5. **RFU emulation** (`sim.Sim` + `reliable`, `gbaframe`, `rfu`, `ni`): open the Pia Reliable(10)
   stream with a metadata frame, send the emulator RFU connect (`'C'`), receive the host's accept
   (`'A'`), run the librfu NI game-data handshake both directions, then stream per-VBlank RFU
   command slots (`'T'` frames) and ack host slots (`'K'` frames).
6. **Overworld/seat phase** (`linkstate.LinkState` + `barrier.BarrierResponder` +
   `trade.EntryPhase`): held-keys keepalive (`0xBE00`), sit at the RIGHT seat (READY `0x16`),
   answer the union-room→trade-center entry standby/card barriers.
7. **Trade FSM** (`trade.TradeEngine`): exchange LinkPlayer + party blocks, react to the host's
   `SET_MONS`/`START`/`CONFIRM_FINISH` broadcasts, run the trade animation timer, commit.
8. **Output** (`mon.Mon.save_pk3`/`save_ek3`, `frlgtrade.save_received`): saved the instant the
   trade commits (robust to an abrupt exit), and again at run-end.
9. **Leave**: after the Nth trade, cancel-to-leave and let the **host** lead the walk-out /
   sever the link.

---

## 2. Architecture / layering

The package docstring (`frlgsim/__init__.py`) states the bottom-up stack. Each per-VBlank RFU
command slot is wrapped up through these layers:

```
                       ┌─────────────────────────────────────────────┐
   frlgtrade.py  ─────▶│ CLI, run_live loop, save_received           │
                       └─────────────────────────────────────────────┘
                                        │ drives
                       ┌─────────────────────────────────────────────┐
   sim.Sim       ─────▶│ per-VBlank orchestrator: RX decrypt/parse,   │
   (the spine)         │ S0 gate, Reliable window, K-ack, NI, TX      │
                       └─────────────────────────────────────────────┘
        ┌──────────────┬───────────────┬───────────────┬──────────────┐
        ▼              ▼               ▼               ▼              ▼
  pia_connect     trade.TradeEngine  linkstate     barrier        ni
  (S0 Net/       (trade FSM,        (held-keys    (standby/      (librfu NI
  Session/RTT)    block supply,      seat/exit     close-link    game-data
                  EntryPhase)        FSM 0xBE00)   0x6600/0x5F00) send/recv)
        │              │
        │              ▼
        │         block (RFU block send/recv sub-FSM)
        │         mon / linkplayer / stats / basestats / charmap (data structures)
        │
        ▼  ── every OUT slot is framed downward ──▶
   14-byte RFU slot (rfu.py)
     └─ emulator 0x54/'T' frame (gbaframe.py)
         └─ Reliable(10) sub-header + Pia message tiling (reliable.py)
             └─ optional zstd + Pia AES-GCM (crypto.py)
                 └─ UDP :12345 over 169.254.x (transport.py)
```

**Two phases**, gated in `sim.Sim`:
- **S0 (connection)** — `ConnectionManager` completes Net + Session(new) + RTT. Nothing
  trade-related is emitted until `sim.connected` (i.e. `conn.state == CONNECTED`).
- **S1+ (trade)** — once connected, the TradeEngine's per-VBlank RFU slots ride Reliable(10).

**Offline vs live**: `sim.Sim.conn is None` on the offline `--replay` path (`ReplayTransport`,
no S0, no `'A'`, no NI, bare UNI/idle `'T'` frames). Live path has `conn` set and runs the full
stack. Many modules branch on `conn is not None`.

---

## 3. Per-module summaries

### `frlgtrade.py` — entry point (~440 lines)
- `main()` — argparse; validates 1..6 party files; fatal-exits without `zstandard`.
- `_Log` — two-level logger. Calling it prints a DETAIL line **only with `--verbose`**; `.info()`
  prints an identifier-free MILESTONE line **only without `--verbose`**. Every line stamped with
  seconds since start (`_START`). Passed as `log` throughout; modules call `getattr(log,"info",log)`.
- `make_engine()` — loads party mons, builds `LinkPlayer`, constructs `TradeEngine`.
- `run_live()` — the big loop. Builds `LiveTransport`, `PiaCrypto`, `ConnectionManager`,
  `LinkState`, random nonzero 2-byte `connect_id`, `Sim`. Then `while True: s.tick()` with an
  elaborate state machine of milestone flags: gate on `s.connected`, sit only when
  `engine.host_ready`, save on `engine.commits` change, answer host `EXIT_ROOM`, mirror
  `READY_CLOSE_LINK`, `LEAVE_TAIL_S = 120.0` overworld leave tail. `period = 1.0/59.727`.
- `run_replay()` — feeds a capture's IN datagrams through the RX stack; sets `anim_delay=5` unless
  overridden (the real ~1935-frame anim would outlast the finite capture).
- `save_received()` — `trades==1` saves the single `--out` file; `trades>1` saves each as
  `<stem>_trade<k>_<species>.pk3`.

**Key CLI knobs** (also see README "Optional Flags"; many are experimental):
`--slot`/`--slots`, `--trades N` (1..6), `--self-id` (locked to `1`), `--ot`, `--version`,
`--anim-delay`, `--decline`, `--refuse-illegit`, `--trust-pia`/`--no-trust-pia`,
`--compress`, `--connect-id`/`--parent-pid`, `--phy` (default `phy0`), `--keys`
(default `~/.switch/prod.keys`), `--comm-id`, `--capture FILE`, `--password` (hex; default = the
built-in 64-byte passphrase).

### `frlgsim/transport.py` — radio + datagram plumbing (566 lines)
- **`GBA_APP_PASSPHRASE`** — the built-in **64-byte** emulator LDN passphrase
  (`fcb6f6adb9dfea66...bdc81d8c`). Belongs to the GBA emulator container, **shared** across its
  titles (FRLG today, RSE later). `assert len == 64`.
- `LiveTransport` — joins via `ldn.connect` in a trio thread (daemon), retries transient failures
  (`start(timeout=30, attempts=3)`). `LOCAL_COMMUNICATION_ID = 0x0100610011000000` (FRLG emulator
  title id). Learns `our_ip/host_ip` (from the interface, ground truth) and `our_mac/host_mac`
  (LDN participant list = the Pia connection GUIDs). TX = bound UDP `:12345` with `SO_BROADCAST`;
  RX = `AF_PACKET SOCK_RAW` (so subnet-directed broadcasts survive) with an 8 MiB `SO_RCVBUF`
  request. `recv()` parses raw Ethernet/IP/UDP itself (`_parse_udp`). The host **initiates** by
  broadcasting Net `0x11` to `169.254.x.255` then unicasts.
- Radio cleanup: `free_radio` deletes stale LDN vifs (`{"ldn","ldn-mon","ldn-tap","ldnclient"}`),
  takes other interfaces off the radio (fixes `SET_CHANNEL -> EBUSY`), kills `wpa_supplicant`.
  `tune_iface` relaxes `rp_filter`, forces the `.255` broadcast route, drops stray zeroconf
  addresses. **All of this needs root.** `_format_join_error`/`_flatten_exc` unwrap trio's opaque
  `ExceptionGroup` to the leaf cause.
- `ReplayTransport` — dispenses IN datagrams from a `.jsonl` capture, collects OUT. `batch = 4`
  IN datagrams per `recv()`. Loads SSID/IPs from a `meta`/`session` record; **fails if the capture
  has no SSID** (can't decrypt).
- `_dump_beacon`/`_b85_decode`/`_frlg_name` — diagnostics only: decode the host's LDN
  advertisement (Pia 0x5C header + custom base85 24-byte RFU record). The connect id is **not**
  taken from here.

### `frlgsim/crypto.py` — Pia AES-GCM + zstd (184 lines)
The bottom encryption layer. **Recipe, locked against a reference capture via GCM-tag verification:**
- `session_key = AES_ECB(FRLG_GAME_KEY, ssid)` where `ssid` is one 16-byte block.
  `FRLG_GAME_KEY = 83ca7fab734c34633b10183526c1e85b`.
- `net_id = CRC32(ssid[1:16])`.
- GCM nonce = `(net_id XOR src_ip_be)(4) || header_nonce(8)`. AAD empty. Tag = **first 8 bytes** of
  the GCM tag.
- **29-byte plaintext header** (`PiaHeader`, `HDR=29`): `[0:4]` magic `32ab9864`; `[4]` enc/version
  (`0x90`); `[5]` flags (`(pad<<4)|zstd|establishing`, default `0x50`); `[6:8]` dst var-id (BE);
  `[8:10]` src var-id (BE); `[10:12]` packet id (BE); `[12]` footer size (`=2`); `[13:21]` header
  nonce; `[21:29]` truncated GCM tag; `[29:]` ciphertext.
- **Station var-ids** (BE u16): `STATION_HOST = 0x7620`, `STATION_JOINER = 0xc493`. The live path
  **learns** both from the wire (`Pia header = [dst_var][src_var]`); these constants matter for
  replay / defaults. Comment warns they were once swapped.
- zstd: `compress()` uses **`ZSTD_LEVEL = 4`** + `_to_window_frame()` (normalises to FHD `0x00` +
  window descriptor `0x18` = 8 KiB) — **verified byte-identical** to the console (level 3 matched
  only 98.8%). `decompress()` peels the optional frame. `HAVE_ZSTD` guards the whole thing.

### `frlgsim/reliable.py` — Pia Reliable(10) sliding window + message tiling (432 lines)
- **`ReliableLink`** — selective-repeat both directions, per peer. RTO/timers all in
  **milliseconds**. Buffers unacked frames, retransmits on RTO or NACK, delivers in order,
  dedupes retransmits. Constants: `RTO_BASE_MS = 33`, `RTO_RTT_FACTOR = 1.4`, `RTT_WINDOW = 7`
  (median), `MAX_INFLIGHT = 128` (default; `sim` overrides to 6). Console-faithful defaults plus
  tunable knobs (`rtt_jitter_k`, `dup_nack_threshold`, `rto_ceil_ms`, `rto_backoff`,
  `rto_bootstrap_ms`) — each a documented divergence for the userspace Wi-Fi bridge (see §6).
  RTO formula: `RTO = 33 + 1.4*median(RTT)` (+ jitter term), no backoff, no floor once samples
  exist. `rto_bootstrap_ms` is a **bootstrap, not a floor** — arms an RTO before the first RTT
  sample so connect-phase frames retransmit, then the pure formula takes over.
- **flagsA** (Reliable sliding-window flags): `1=AppData 2=MsgStart 4=MsgEnd 8=Initialized`.
  `FLAGSA_GBA = 0x07` (complete single-fragment data), `FLAGSA_INIT = 0x0f` (stream-opening;
  peer ignores DATA until it sees one), `FLAGSA_CTRL = 0x00` (bulk-ack payload).
- **`METADATA_FRAME`** — the emulator's first Reliable payload (title/version metadata,
  "LeafGreen_e"), carried on the INIT frame. `4a002a005801004c656166477265656e5f65` + 28×`00`.
  Note: its leading byte is `0x4a` ('J') but it is **not** a `0x57` frame — do not confuse it.
- **`Reliable`** sub-header (8 bytes, BE): `flags(1) size(2) seq(2) window_base(2) N(1) [payload]`.
  `N` = multicast recipient count, 0 for unicast.
- **Bulk ack**: `build_bulk_ack(next_expected, mask)` / `parse_bulk_ack` — cumulative next-expected
  + 128-bit selective mask (bit i set ⇒ `ack_id+1+i` received).
- **Message tiling** (`parse_messages`/`build_message`/`parse_app`/`build_app`): a decrypted
  application blob = `<message>* <2-byte station-id footer> <0xff padding>`. Each message has a
  presence-flag header; fields **inherit** from the previous message when the bit is clear. Flags:
  `0x1=msgflags 0x2=size 0x4=proto 0x8=1-byte port` (6.32+ format; the old 5.27-6.30 format had an
  8-byte u64 here). `PROTO_NAMES = {1:Net, 3:RTT, 4:Sync, 5:Unreliable, 9:Clock, 10:Reliable,
  13:Session}`. The footer is the **recipient** var-id.

### `frlgsim/pia_connect.py` — S0 connection state machine (331 lines)
- **`ConnectionManager`** — host-ack-gated FSM: `ST_NET → ST_FINALIZE → ST_CONNECTED`. Never
  advances on our own send; advances when the **host** acknowledges (it retransmits each stage).
- Net (proto 1): `0x11` conn request (host→us), `0x12` response (echoes the `0x11` seqid — do
  **not** hardcode). `0x50` "update network property" / `0x51` ack (host retransmits every 500ms).
- **`parse_net_conn_request`**: the host's Pia **constant id** is the emulator's fixed virtual
  GBA-adapter MAC (`e5395b69d280`, identical across physical Switches), learned **from the wire**,
  **not** from the LDN participant list. The Session join must address this constant id.
- RTT (proto 3): byte 0 = type (0 request / 1 response); **byte 3 = protocol version (3), not part
  of the type** — an old u32-LE read broke this. `build_rtt_response` flips only byte 0 and echoes
  the timestamp. `maybe_originate_rtt` sends a type-0 probe every `RTT_ORIGINATE_PERIOD = 10`
  VBlanks; matching the echoed systime yields a round-trip that feeds the reliable RTO. RTT rides
  header `dst=0x0001` (`SESSION_VAR`) with footer = host var.
- Session (proto 13): `build_session_join` reproduces the reference capture's message #3
  byte-for-byte given its values. Finalize (`build_session_finalize`, type 6) is emitted **only**
  on the host's `SESSION_UPDATE` (type 5) join-accept, never on the type-2 follow-up.
- Var-id header progression: `net 0x12 → (0,0)`, `session join → (0, our_var)`,
  `finalize/reliable → (host_var, our_var)`. Establishing frames force `pktid=0`.

### `frlgsim/rfu.py` — 14-byte RFU command slot (167 lines)
The unit the child emits once per VBlank. `COMM_SLOT_LENGTH = 14` (7× u16 LE).
- OUT opcodes (high byte of word0): `IDLE=0x0000`, `SEND_BLOCK_INIT=0x8800`, `SEND_BLOCK=0x8900`,
  `SEND_HELD_KEYS=0xBE00`, `READY_EXIT_STANDBY=0x6600`, `READY_CLOSE_LINK=0x5F00`.
- IN-only (we react, never emit): `SEND_BLOCK_REQ=0xA100`, `SEND_PLAYER_IDS=0x7700`,
  `DISCONNECT=0xED00`.
- **Rolling tag** (`childSendCmdId`, 0..7): lives in bits 5-7 of word0's low byte, `+1 mod 8` on
  every **non-idle** slot. Host hard-errors after >4 bad ids. **Idle = 14 zero bytes and does NOT
  advance the tag.** `SlotBuilder` (one per link) applies + advances it.
- `FRAG_INDEX_MASK = 0x1F` — SEND_BLOCK index is low 5 bits. `OWNER_FLAG = 0x80` — owner word2 =
  `mpId | 0x80` (joiner owner=1 → `0x81`).
- **LLSF (librfu Link-Layer Sub-Frame)** states: `LCOM_NULL=0, LCOM_NI_START=1, LCOM_NI=2,
  LCOM_NI_END=3, LCOM_UNI=4`. CHILD LLSF = 2-byte LE: `state<<10 | ack<<9 | n<<7 | phase<<5 | size`.
  PARENT LLSF = 3-byte: `state<<14`. `uni_slot(cmd14)` wraps a 14-byte cmd in a UNI sub-frame
  (`(4<<10)|14 = 0x100e` → `0e 10` + cmd = 16-byte slot).
- `parse_slot` decodes an IN slot; note `SEND_BLOCK_REQ` reqtype selector is in **word1**
  (`slot[2:4]` LE), not word0's low byte (word0 is exactly `0xA100`).

### `frlgsim/gbaframe.py` — emulator 0x54 frame layer (149 lines)
The Switch adapter's wrapper around the RFU slot. `57 <type:1> <len:u16 LE> <body>`. Types:
`J=0x4A` (metadata/config), `C=0x43` (connect), `A=0x41` (host accept), `K=0x4B` (data ack),
`T=0x54` (slot data), `D=0x44` (host disconnect).
- **`build_connect(connect_id)`** — `57 43 02 00 <connect_id:2>`. `connect_id` is our own
  self-chosen 2-byte RFU id; any nonzero value works (host does not match it — it just seats us).
  Host echoes it in `'A'` and repeats it in `'D'`.
- **`wrap_t(slot, ts)`** — CHILD/joiner `'T'`: `57 54 <len> | <ts:u32 LE> 00 <slot_len:u8 @body[5]>
  00 00 | <slot, pad mult-4>`. `ts` = per-NEW-frame counter (must increase; **reused on
  retransmit**).
- **`build_k(k_seq, mid, acked_ts)`** — `57 4b 0c 00 <k_seq:u32><mid:u32><acked_ts:u32>` LE. One per
  UNIQUE host `'T'` ts. `k_seq` global +1 from 1; `mid` = 1-based position in the OUT datagram.
- **`parse_in(payload)`** — parses HOST/parent frames. HOST `'T'` slot_len is at **body[4]**
  (child's is body[5] — an off-by-one between directions). `slot_len<=1` = host idle keepalive
  (still K-acked). For UNI (parent LLSF state 4), the slot coalesces N×14-byte `gRecvCmds` by mpId
  (`slots`/`positional` = `[(mpId, 14-byte slot)...]`, chunk0=host's own, chunk1=our reflection).
  For NI windows, attaches `record['ni'] = {state,ack,n,phase,size,payload}`. `'A'` →
  `{host_session_id, connect_id}`. The engine reads the `positional` alias.

### `frlgsim/ni.py` — librfu NI game-data handshake (220 lines)
After the host's `'A'`, the child runs the librfu NI (reliable acknowledged) sender to deliver its
26-byte `RfuGameData` before any UNI traffic. Because Pia Reliable already guarantees delivery,
this is a **faithful single-pass** sender (each sub-frame emitted once, no retransmit window).
- `build_game_data(version_low, trainer_id, ot_name)` — the 26-byte src: `serialNo(2 LE) +
  gname[15] + uname[9]`. `RFU_SERIAL_GAME = 0x0002`. `compat = language & 0xF | (version_low&0xF)<<10`.
  `ACTIVITY_TRADE = 0x04`. Verified `build_game_data(5, 0x2288, "EMU")` == reference capture.
- **`NISender`** — emitted sub-frame sequence for 26 bytes (payloadSize 12): `NI_START n=1 ph0 sz7`
  (7-byte header: dataType, payloadSize u16 LE, dataSize u32 LE) → `NI n=1 ph0 sz12` → `NI n=1 ph1
  sz12` → `NI n=1 ph2 sz2` → `NI_END n=0 sz0` → `NULL n=1 sz0`. `WINDOW_COUNT = 4`,
  `CHILD_FRAME_SIZE = 2`, `NI_HEADER_SIZE = 7`.
- **`NIReceiver`** / `recv_ack_slot` — the host runs its **own** NI sender right after; the child
  must ACK every host NI sub-frame (mirror state/n/phase, `ack=1 sz=0`) or the host faults the link.
  `RFU_STATUS_JOIN_GROUP_OK = 5`: the host's NI carries a 1-byte join **status**; anything ≠ 5 means
  the host **rejected** us → `sim.ni_rejected` → clean abort. The host's terminal NULL is **not**
  acked. Host's first UNI slot (state 4) means its NI is done.

### `frlgsim/sim.py` — per-VBlank orchestrator (761 lines, the spine)
Wires transport ↔ crypto ↔ Pia ↔ FSMs. `MS_PER_VBLANK = 1000/59.727`. `RELIABLE_SEQ_START = 0xFFF0`.
`TS_SEED = 0x0000362E`.
- `tick()` — one VBlank: `recv()` + `process_datagram`, feed RTT samples, drain the
  ConnectionManager outbox, then `_drive_reliable()` (live) or a bare `'T'` (offline).
- `process_datagram` — learn var-ids, decrypt, decompress, tile messages, dispatch by proto.
  Reliable AppData: `note_received` + `_on_gba_in`. Reliable CTRL: `on_ack`. Counts `rx_protos`,
  `rx_fail`.
- `_on_gba_in` — dispatch `'A'`/`'T'`/`'D'`/`'K'`. K-ack every unique host ts; handle host NI;
  detect host UNI (state 4); feed UNI slots to `engine.feed_in_frame`.
- `_drive_reliable` — the core send pump. Order per VBlank: (1) open stream (METADATA/INIT); (2)
  emulator `'C'` connect; (3) batch = retransmits (gap-targeted) + K-acks + one `'T'` slot +
  bulk-ack **last**. Batches ≤ `RELIABLE_BATCH_MAX = 9` messages per datagram.
- `_gba_frame` — builds this VBlank's `'T'`: NI handshake first (send-NI → recv-NI ack → wait for
  host UNI), then `engine.tick()` UNI slot, or a held-keys keepalive (only in the seat phase, only
  after establishment).
- **Reliable congestion knobs** (documented divergences, see §6): `MAX_INFLIGHT = 6`,
  `RTT_JITTER_K = 4.0`, `DUP_NACK_THRESHOLD = 3`, `RTO_CEIL_MS = 670`, `RTO_BACKOFF = 1.0`
  (disabled), `RTO_BOOTSTRAP_MS = 200`. K pacing: `K_PER_VBLANK = 3`, `K_INFLIGHT_MAX = 3`,
  `K_BACKLOG_MAX = 32`. `ACK_PERIOD = 2` VBlanks. `COMPRESS_MIN = 62` (compress iff message body
  ≥ 62 bytes — the exact host rule). `RTX_GAP_LIMIT = 1` (block phase), `RTX_GAP_LIMIT_NI = 2`.
  Per-channel pktid counters keyed by header dst var-id (three independent counters observed:
  establishing/session-RTT/reliable). Pure-acks carry msgflags `0x40` (SACK bit); data stays 0.

### `frlgsim/trade.py` — trade FSM + entry phase (1451 lines, the largest module)
The JOINER is a **reactive Follower** (mpId 1 = RIGHT seat): it stages blocks, supplies them when
the Leader pulls with `SEND_BLOCK_REQ`, pushes 20-byte LINKCMD blocks, and reacts to the Leader's
broadcasts. It **never** emits `SET_MONS`/`START`/`CONFIRM`/cancel broadcasts.
- **LINKCMD opcodes**: OUT `READY_TO_TRADE=0xAABB`, `INIT_BLOCK=0xBBBB`,
  `READY_FINISH_TRADE=0xABCD`, `REQUEST_CANCEL=0xEEAA`, `READY_CANCEL_TRADE=0xBBCC`. IN
  `SET_MONS_TO_TRADE=0xDDDD`, `START_TRADE=0xCCDD`, `CONFIRM_FINISH_TRADE=0xDCBA`,
  `PLAYER_CANCEL_TRADE=0xDDEE`, `BOTH_CANCEL_TRADE=0xEEBB`, `PARTNER_CANCEL_TRADE=0xEECC`.
- **Block-request selectors**: `BLOCK_REQ_SIZE_NONE=0`, `_200=1`, `_100=2` (trainer card),
  `_220=3` (mail), `_40=4` (giftRibbons). `REQ_SIZE` maps to byte sizes {200,200,100,220,40}.
- **Block counts** (`ceil(size/12)`): `COUNT_LINKCMD=2`, `COUNT_PARTY=17` (200B),
  `COUNT_MAIL=19` (220B), `COUNT_RIBBON=4` (40B), `COUNT_TRAINER_CARD=9` (100B).
- **Validity verdicts** (`CheckValidityOfTradeMons`): `PLAYER_MON_INVALID=0`, `BOTH_MONS_VALID=1`,
  `PARTNER_MON_INVALID=2`. `SPECIES_MEW=151`, `SPECIES_DEOXYS=410` (the illegit-legend gate,
  opt-in via `--refuse-illegit`; legitimacy is not offline-decodable).
- **Timing**: `DEFAULT_ANIM_FRAMES = 1935` (~32.4s DoTradeAnim @ 59.727Hz, content-dependent
  stand-in — the early-arrival guard makes the FSM correct for any value). `INVALID_CANCEL_DELAY =
  180`. `SAVE_BARRIER_GAP = 60`, `SAVE_CHAIN_TIMEOUT = 600`, `BUFFERTRADE_SETTLE = 600`,
  `WARP_STANDBY_EMITS = 6`, `POST_SEAT_STANDBY_DELAY = 20`, `WARP4_WATCHDOG = 180`.
- **FSM states**: `S1_LINK, S4_PARTY, S5_SELECT, S6_CONFIRM, S7_ANIM, S8_DONE, S_CANCEL`.
- **`EntryPhase`** (P0..P5, one-shot per session, never re-fires on trades 2..6):
  P0 warp-quiesce #1 → P1 card exchange (`BLOCK_REQ_SIZE_100`) → P2 seat barrier → P3 warp-quiesce
  #2 → P4 trade menu (latches `seat_phase_over`) → P5 in-trade (`complete`).
- **`TradeEngine`**: block supply keyed by REQ size (`_block_for_size`) — 200 = LinkPlayer(one-shot,
  round 0) then 3 party blocks; 100 = trainer card; 220 = mail (none); 40 = ribbons (none). Big
  `tick()` priority ladder: block send → cancel-exit barrier (d) → return-to-field barrier (e) →
  post-cancel overworld → save chain (c) → wall-clock timers → menu/scene-seam barrier (b) →
  warp-quiesce standbys (#1/#2/#3/#4) → selection [S5] → pending LINKCMD push → priority-5 barrier
  → idle. **Multi-trade loop**: `offered_slots` must be distinct (received mon swaps into the
  offered slot); after the Nth trade, `leaving=True` → cancel-to-leave via `REQUEST_CANCEL`.
- **Key properties**: `in_seat_phase`, `established` (`gReceivedRemoteLinkPlayers`),
  `host_in_seat` (host's first `0xBE00`), `host_ready` (host's READY `0x16`), `host_exiting`
  (host's `EXIT_ROOM 0x17`), `done`, `commits`, `received_mons`.

### `frlgsim/block.py` — RFU block send/recv sub-FSM (234 lines)
`FRAG_BYTES = 12`. Loss tolerance is first-class.
- `RecvBlock`/`BlockReceiver` — idempotent, order-independent reassembly (bitmask of received
  fragment indices). A same-size INIT resend mid-transfer keeps progress.
- `BlockSender` — ACK-gated child send: `INIT → STREAM → HOLD → DONE`. Re-sends INIT until the host
  echoes it (reflection into `peers[1]`, owner `0x81`), streams fragments, HOLDs the last and
  re-queues missing ones until `receivedFlags` full. `watchdog_init=4`/`watchdog_hold=6` are the
  **offline** (no-reflection) backstops only. **`trust_pia`** (default OFF): fire-and-forget each
  fragment once — a bridge adaptation, not "more faithful"; see §6.

### `frlgsim/barrier.py` — standby/close-link barrier responder (285 lines)
Models the child-initiated `READY_EXIT_STANDBY (0x6600)` / `READY_CLOSE_LINK (0x5F00)` mirror. The
real strict-ROM host's leader branch **waits** for the child's standby, so a purely reactive sim
deadlocks — we must **initiate**. Two entry paths: INITIATE (FSM-driven) and REACTIVE (host went
first). `INITIATE_TIMEOUT = 120` (offline watchdog release, **does not** increment `local_count`),
`IDLE_TIMEOUT = 90` (reactive round ended, **does** increment). CLOSE is terminal (accepts any
count; never auto-clears). Live: `max_emits` (`BARRIER_EMITS=6`) bounds NEW frames per count then
goes quiet (reliable retransmit redelivers) to avoid the standby-flood deadlock.

### `frlgsim/linkstate.py` — held-keys overworld FSM (209 lines)
Mirrors `CB1_UpdateLinkState`/`SendKeysToRfu`: emits a `0xBE00 SEND_HELD_KEYS` keepalive every
VBlank in the seat phase. Key codes: `EMPTY=0x11`, `READY=0x16` (sit), `EXIT_ROOM=0x17` (leave),
`IDLE=0x1A`, `EXIT_SEAT=0x1D`. Host-side states: `PLAYER_LINK_STATE_READY=0x82` etc. FSM:
`PRE_SEAT → (sit) READY once → SEATED → (exit) EXIT_ROOM once → EXITING → SEND_NOTHING`.
**heldKeyCount packing**: `w1 = (heldKeyCount<<8) | keycode`; heldKeyCount is a static u8, first
emit carries high byte **1** (not 0), rolling mod 256 — a pure liveness nonce; the host reads only
the low byte. `self_id` asserted `== 1` (JOINER, RIGHT seat).

### `frlgsim/linkplayer.py` — 60-byte LinkPlayerBlock + trainer card (138 lines)
- **`LinkPlayerBlock` (60B)** = `GAMEFREAK_MAGIC[16]` + `struct LinkPlayer(28B)` + `GAMEFREAK_MAGIC[16]`.
  `GAMEFREAK_MAGIC = b"GameFreak inc.\x00\x00"`. The host strcmp-validates **both** magics or drops
  to `CB2_LinkError`. On the wireless path the host pulls it via `SEND_BLOCK_REQ` type NONE (a
  fixed 200-byte buffer, count=17). `VERSION_FIRE_RED=0x4004`, `VERSION_LEAF_GREEN=0x4005`
  (`gGameVersion + 0x4000`). `LANGUAGE_ENGLISH=2`. Default `trainer_id=0x47ED8822`.
- **`build_trainer_card`** — the 100-byte `BLOCK_REQ_SIZE_100` buffer for the union-room entry.
  `struct TrainerCard` (0x60=96B) + wonder-card u16 @96 + 2 residue. Reuses the LinkPlayer's
  OT/trainerId/version. Cosmetic to the trade but pulled before the trade menu exists.

### `frlgsim/mon.py` — Gen-3 mon `.pk3`/`.ek3` I/O (308 lines)
- `struct Pokemon` (100B) = `BoxPokemon(80B)` + 20-byte party tail (status u32 @80, level u8 @84,
  mail u8 @85, hp/maxHP/atk/def/spe/spa/spd u16 @86..98). `PARTY_MON_SIZE=100`, `BOX_SIZE=80`,
  `PARTY_BLOCK_SIZE=200`, `SECURE_OFF=32`, `SECURE_END=80`.
- **Encryption**: `.ek3` (raw wire/save) = 48-byte secure region XOR'd by `PID^OTID` and 4
  substructs shuffled by `PID%24` (`SUBSTRUCT_ORDER` table, G/A/E/M). `.pk3` = decrypted +
  unshuffled to canonical G,A,E,M. Header (incl. checksum @28) + party tail are plaintext in both.
  `to_decrypted`/`to_encrypted` convert; a `Mon` internally always holds the **wire (.ek3)** form.
- `decode_mon` — the **checksum oracle** (16-bit sum over the 48-byte secure region as 24 u16s vs
  stored @28). The only validity gate that matters; party stats are not covered (cosmetic).
- `Mon.from_pk3` — auto-detects `.ek3` vs `.pk3`. **Gotcha**: when `PID==OTID` (key 0) the XOR is
  identity so both forms checksum-validate; it assumes the injection case (a decrypted `.pk3`) and
  rebuilds the wire form. An 80B box is widened to 100B (mail byte → `0xFF`) and its party tail is
  derived from box data via `stats.build_party_tail` when the level byte is 0.
- `build_player_party` (600B), `party_blocks` (three 200B blocks). `save_pk3` writes decrypted,
  `save_ek3` writes raw.

### `frlgsim/stats.py` + `frlgsim/basestats.py` — level/stat reconstruction
`build_party_tail(canon)` reconstructs the 20-byte tail (level + 6 stats) from box data so a
box-sourced `.pk3` shows the right level instead of 0. Uses `EXP_TABLES` (6 growth curves),
`NATURE_STAT_TABLE` (`nature = PID%25`), IVs from misc[4:8] (5 bits each), EVs from evs[0:6].
Special cases: `SHEDINJA=303` (HP=1), `DEOXYS=410` (Attack forme). `basestats.BASE_STATS` maps
internal species index → `(hp, atk, def, spe, spa, spd, growthRate)` (396 lines of data; the
252-276 index gap is absent). The tail is **not** checksummed.

### `frlgsim/charmap.py` — GBA English character map
`encode`/`decode` for names. `EOS=0xFF`, `PAD=0xFF`. `0xBB..0xD4 = A..Z`, `0xD5..0xEE = a..z`,
`0xA1..0xAA = 0..9`, `0x00 = space`. Used only for the sim's own LinkPlayer OT name / trainer-card
text / NI uname (mon names inside a `.pk3` are already in this charmap). The docstring notes a
prior table had wrong punctuation (`. - … /`) — **wire-affecting** via `encode()` for OT names
containing those glyphs.

---

## 4. Critical protocol knowledge (quick reference)

| Thing | Value / fact | Location |
|---|---|---|
| LDN passphrase | 64-byte `fcb6f6ad...bdc81d8c`, shared across GBA emulator titles | `transport.py:218` |
| FRLG title id | `LOCAL_COMMUNICATION_ID = 0x0100610011000000` | `transport.py:284` |
| Pia game key | `FRLG_GAME_KEY = 83ca7fab734c34633b10183526c1e85b` | `crypto.py:42` |
| Pia magic | `32ab9864` | `crypto.py:43` |
| zstd magic / level | `28b52ffd`; **level 4** + window desc `00 18` (byte-identical) | `crypto.py:44,133` |
| Session key | `AES_ECB(game_key, ssid)`; `net_id = CRC32(ssid[1:16])` | `crypto.py:156` |
| GCM nonce | `(net_id ^ src_ip_be)(4) || header_nonce(8)`; AAD empty; tag = 8 bytes | `crypto.py:159` |
| Pia header | 29 bytes; `[6:8]`dst `[8:10]`src var-ids (BE); `[10:12]` pktid | `crypto.py:56` |
| Station var-ids | host `0x7620`, joiner `0xc493` (learned live from wire) | `crypto.py:51` |
| Host Pia constant id | emulator virtual MAC `e5395b69d280` (from Net 0x11, not participant list) | `pia_connect.py:79` |
| RFU slot | 14 bytes (7× u16 LE); rolling tag bits 5-7 word0.low, +1 mod 8 non-idle | `rfu.py` |
| GBA frame types | J=0x4A C=0x43 A=0x41 K=0x4B T=0x54 D=0x44 | `gbaframe.py:25` |
| Connect frame | `57 43 02 00 <connect_id:2>`; any nonzero id, host echoes it | `gbaframe.py:35` |
| Reliable seq start | `0xFFF0`; `RTO = 33 + 1.4*median(RTT)` ms | `reliable.py`, `sim.py` |
| METADATA_FRAME | `4a002a005801004c656166477265656e5f65` + 28×00 (INIT payload) | `reliable.py:41` |
| GameFreak magic | `b"GameFreak inc.\x00\x00"` (both ends of the LinkPlayerBlock) | `linkplayer.py:19` |
| LINKCMD opcodes | READY_TO_TRADE 0xAABB, SET_MONS 0xDDDD, START 0xCCDD, CONFIRM_FINISH 0xDCBA, REQUEST_CANCEL 0xEEAA | `trade.py:18` |
| Block counts | LINKCMD 2, party 17, mail 19, ribbon 4, card 9 (= ceil(size/12)) | `trade.py:38` |
| NI game data | 26 bytes; `RFU_SERIAL_GAME=0x0002`; JOIN_GROUP_OK=5 | `ni.py` |
| Mon secure region | bytes 32..80, XOR by PID^OTID, shuffle by PID%24; checksum @28 | `mon.py` |
| VBlank rate | 59.727 Hz (`MS_PER_VBLANK`, `period` in run_live) | `sim.py:25` |
| Anim duration | `DEFAULT_ANIM_FRAMES = 1935` (~32.4s) | `trade.py:63` |

---

## 5. Data structures

- **`.pk3` / `.ek3`** (`mon.py`) — 80B box or 100B party `struct Pokemon`. Wire form IS the
  canonical PKHeX `.pk3` layout, so injecting a chosen mon is essentially a memcpy (no
  re-encryption). Internally a `Mon` holds the encrypted+shuffled wire (`.ek3`) form;
  `save_pk3` decrypts, `save_ek3` writes raw.
- **`gPlayerParty`** — 6 slots × 100B = 600B (`build_player_party`), streamed as three 200B party
  blocks (`party_blocks`); each block = 2 mons; empty slots zeroed.
- **`LinkPlayerBlock`** (60B, `linkplayer.py`) — dual GameFreak magic + 28-byte `struct LinkPlayer`.
- **Trainer card** (100B `build_trainer_card`) — `struct TrainerCard(0x60)` + wonder-card u16.
- **NI game data** (26B, `ni.build_game_data`) — the child's `RfuGameData` connection config.
- **Base stats / stat reconstruction** — `basestats.BASE_STATS` (internal-index → tuple),
  `stats.build_party_tail` rebuilds level + stats for box-sourced mons.
- **Received mon** — indexed from the host party by `host_cursor % PARTY_SIZE`
  (`partnerCursorPosition = recv[0][1] + PARTY_SIZE`), captured at `_commit`, swapped into our
  offered slot (mirrors `TradeMons`).

---

## 6. Gotchas, invariants, and fragile areas

**Hardware / environment**
- **Root required** (raw sockets, `iw`/`ip`/`sysctl`/`nmcli`, radio manipulation). The `--live`
  path cannot be exercised offline; it's written to mirror the proven bridge code path.
- **WiFi phy selection** (`--phy`, default `phy0`) must be a monitor/injection-capable card;
  reliability is card-dependent (README: ALFA AWUS036ACHM high, RZ616 low/deadlocks). NetworkManager
  must not manage the interface (`free_radio` forces it unmanaged and downs it — restores need a
  separate step).
- **`zstandard` must be installed in the running interpreter** or the sim silently never replies
  (`main()` fatal-exits early to prevent this). `pycryptodome`, `trio`, `ldn==0.0.17` also pinned.
- **`prod.keys`** at `~/.switch/prod.keys` (or `--keys`) — needed to scan/join LDN.

**Timing-sensitive (this is a soft-real-time protocol emulator)**
- Every wall-clock timer (the 1935-frame anim → READY_FINISH, the 180-frame invalid-mon →
  READY_CANCEL) **must advance every VBlank**. They live in `_advance_timers`, driven from both
  `tick()` and `poll_send_done()` (when the send window is gated) — otherwise they crawl behind
  the window and the host parks forever ("Take good care of…"). This "engine-state-gated-behind-
  emission" bug class has bitten 3 times (see `trade.py` comments).
- **Barrier counts must stay in lockstep with the host.** Bursting the next standby count before
  the host echoed the current one desyncs its FSM → in-game "Communication error". Watchdog
  releases must **not** increment `local_count` (no real round passed); reactive round-end **must**.
- **Do not sit early.** Emit READY (`0x16`) only after the host itself sits (`host_ready`, its own
  `0x16`), not merely when it enters the room — an out-of-sequence READY faults the host's cable-seat
  FSM. Held keys + sit are also gated on `engine.established` (both LinkPlayers exchanged); a tagged
  `0xBE00` racing ahead of the NI/block handshake faults the host's `childSendCmdId` check in ≤5
  frames. Pre-establishment idle VBlanks must be bare all-zero IDLE slots.
- **Let the host lead the exit.** After the trade, keep the link alive and answer the host's
  `EXIT_ROOM`/`READY_CLOSE_LINK` reactively. Emitting `EXIT_ROOM` proactively hit the host
  mid-`CB2_ReturnToFieldFromMultiplayer` and tripped `LinkRfu_FatalError`. `LEAVE_TAIL_S = 120`.

**Reliable-layer divergences (the userspace Wi-Fi bridge breaks the console's assumptions)**
The console's defaults (large window, RTO=33+1.4·median, fast-retransmit on 1 NACK) assume a
near-constant-latency local radio. This bridge has a ~50ms median RTT but a ~1s tail (~20× jitter)
and almost no real loss, so the console settings collapse into a retransmit storm. `sim.py`
overrides, each a documented divergence that defaults to console behavior in `reliable.py`:
- `MAX_INFLIGHT = 6` — the ceiling; **18 and 128 both comms-error** shortly before the save.
- `RTT_JITTER_K = 4.0` (cover the tail), `DUP_NACK_THRESHOLD = 3` (a single NACK usually means
  in-flight, not lost), `RTO_CEIL_MS = 670`, `RTO_BACKOFF = 1.0` (**backoff off** — it caused a
  massive regression; stuck frames genuinely need many fast resends), `RTO_BOOTSTRAP_MS = 200`
  (arms an RTO before the first RTT sample so the connect-phase `'J'`/`'C'` retransmit until the
  host's reliable side engages ~2s in — **not a floor**).

**Experimental / incomplete flags** (README warns undocumented flags may be unfinished):
- **`--trust-pia`** (default **OFF** / faithful re-send). `trust_pia` send-once "crawled and never
  completed" against the real client; the faithful re-send-until-confirmed loop (~1.5×) completes
  in 1-2s. It was a workaround for a "flood" that turned out to be the RTT deadlock (now fixed).
- **`--decline`**, **`--refuse-illegit`** — trade-refusal paths (confirm-NO / Deoxys-Mew gate);
  the illegit gate can't decode the fateful-encounter flag offline, so it's a species heuristic.
- **`--compress`** (zstd OUT payloads), **`--connect-id`/`--parent-pid`** (debug override; the
  alias is deprecated), **`--capture`** (record every Pia datagram both directions to `.jsonl`).

**Invariants that must never break**
- The sim is **always** the JOINER = mpId 1 = RIGHT seat = Follower (`--self-id` locked to 1;
  asserted in `TradeEngine`, `LinkState`). mpId 0 is the host. The Follower only reacts to the
  Leader broadcast opcodes (`LEADER_BROADCAST_OPCODES`); emitting one is a bug.
- The rolling RFU tag advances only on non-idle slots; **a barrier and a block never coexist on the
  wire** (guaranteed by the `tick()` priority ladder) so the tag can't be corrupted.
- `offered_slots` must be **distinct** (the received mon swaps into the offered slot;
  re-offering re-gives a just-received mon). N trades need ≥N party mons.
- The `EntryPhase` (P0..P5) is **one-shot per session** — it never re-fires on trades 2..6 (the
  post-trade loop re-enters `CB2_StartCreateTradeMenu`, not the seat barrier). `_lp_sent` is a
  session one-shot that `_reset_round_state` deliberately does **not** clear (clearing it would
  resend the LinkPlayer as party block #1 and drop party pair #3).
- The 100B trainer-card pull is REQ-driven and tolerated whether or not the live host issues it
  (a "live residual").

**Reference-capture provenance**
Many constants (var-ids, session-join layout, NI sequence, zstd level, `TS_SEED`, RTT format) are
"verified byte-exact vs the reference capture" or "templated from the reference capture and may need
live tuning." When touching these, cross-check against a fresh `--capture` `.jsonl` — the code
comments flag which values are proven vs. templated. Live residuals (things not observable offline)
are explicitly labeled: the fateful-encounter flag, the egg flag, the warp/seat field transition,
the RTT host-echo.

---

## 7. How to run

See [`README.md`](./README.md). Short form (needs root, the Switch, and the deps):

```bash
sudo -E ./venv/bin/python frlgtrade.py --live -o output.pk3 PARTY1.pk3 PARTY2.pk3
```

Offline self-check (replays a capture through the full RX stack, no Switch):

```bash
python3 frlgtrade.py --replay capture.jsonl dummy.pk3 trademon.pk3
```

On the console: select trading at the Direct Corner, be the "Leader", run the script (may take
several tries to connect), approve the join from "EMU", walk to the LEFT chair, select a mon,
accept, then cancel and walk out. `PARTY2.pk3` ends up in your party; the mon you gave lands in
`output.pk3`.
