# Mystery Gift Distributor — Plan & Progress

Status doc for adding a **Mystery Gift distributor** capability to `frlg-ldn-trade`: letting the
Python program hand a real FireRed/LeafGreen console a **Lansat Berry** (`ITEM_LANSAT_BERRY = 173`)
via a saved Wonder Card whose RAM script the in-game **deliveryman** runs.

Companion docs: [SKILLS.md](SKILLS.md) (Python stack), [pokefirered/SKILLS.md](pokefirered/SKILLS.md)
(§2–5, §9 wireless internals), `.claude/skills/mystery-gift/SKILL.md` (Mystery Gift protocol).
The working implementation plan also lives at `~/.claude/plans/next-let-s-plan-…md`.

---

## 1. Goal & the defining constraint

The project now has a production RFU **parent / LDN host** for Direct Corner trades: the console
scans for and joins Linux. A Mystery Gift distributor reuses those host-side LDN, Pia, Reliable, and
RFU foundations, but still needs the activity-21 discovery identity and Mystery Gift server protocol.

### Confirmed decisions
- **Delivery mechanism:** Wonder Card + RAM script → deliveryman (not an immediate mystery-event grant).
- **Receive path:** the **FRIEND** path, *not* the wireless distributor path. Beacon research found
  the RFU serial is **not** carried in the LDN beacon the game hands the Switch emulator (the
  `svc_47` handoff is only `gname[16] + uname[8]`, no serial); the emulator synthesizes
  `partner.serialNo` itself, ≈always **`0x0002`** for the FRLG title. So `0x7F7D` is **not externally
  injectable** and the auto-connect distributor path is a likely dead end. The friend path uses serial
  **`0x0002`** (the serial the trade code already relies on) + beacon `activity = ACTIVITY_WONDER_CARD
  (21)`; the console user picks us from a list with one **A-press**. After connection the friend and
  distributor paths are byte-identical (same `MysteryGiftClient`), so this choice changes only the
  beacon field values and the console-side UX.
- **Ground truth for host layers:** mirror the existing joiner FSM by symmetry and iterate against a
  real Switch (crypto is direction-agnostic). No capture of a hosting session is required or available.
- **Sequencing:** staged. **M1** = console joins us + player exchange (`gReceivedRemoteLinkPlayers`).
  **M2** = MysteryGiftLink transport + server driver. **M3** = the Wonder Card / RAM-script payload.

---

## 2. Testing tiers

Every step below is tagged with the cheapest tier that can validate it. The crucial fact: the
**RFU / link / MysteryGiftLink / game-logic** layers are identical whether carried over Switch-LDN or
a real GBA wireless adapter, so anything provable at the GBA level (Tier 2) also holds for the Switch
path. Only the **LDN + Pia + beacon** layers are Switch-exclusive.

### Tier 1 — Integrated / offline (Python, no hardware)
Pure unit tests of byte-exact builders and state machines against decomp facts. File:
[tests/test_mystery_gift_offline.py](tests/test_mystery_gift_offline.py) (13 tests, no new deps).
Run: `.venv/bin/python tests/test_mystery_gift_offline.py`. Fast, deterministic, CI-able. State
machines (MG transport, server script driver, parent link) are testable here by running them against
a **Python model of the console client** — no console needed.

### Tier 2 — mGBA emulator (GBA ROM, no Switch)
Runs the FRLG ROM (retail or a pokefirered build). Two uses:
- **(2a) Payload validation via save / RAM injection — proves M3 with no Switch and no link.**
  Inject our `WonderCard` + `cardCrc` at `SaveBlock1.mysteryGift` (offset **0x3120**) and the
  `RamScript` (checksum + body) at `SaveBlock1.ramScript` (offset **0x361C**), fix the affected
  save-sector footer checksums (`SECTOR_DATA_SIZE = 3968`, `CalculateChecksum`, save.c), OR poke those
  fields in RAM at runtime via mGBA's Lua scripting. Then walk to the Pokémon Center deliveryman and
  confirm the **Lansat Berry** is received and the one-shot (`setflag 0x2AA` / `endram`) holds.
- **(2b) RFU/link protocol peer — investigate.** IF mGBA's GBA-Wireless-Adapter emulation is
  sufficient and exposes a link socket, drive the FRLG ROM as the MG *client* from Python at the RFU
  level to validate the parent link + MG transport **without** the Switch. mGBA wireless-adapter
  support is limited/experimental, so treat this as "worth trying," not guaranteed.

**mGBA cannot** test the Switch's LDN/Pia/beacon layers — they don't exist in a GBA emulator.

### Tier 3 — Physical Switch (FRLG on NSO)
Required for everything touching the closed LDN glue and the real Pia stack: beacon-over-LDN (serial
synthesis, activity visibility), the Pia host handshake, `ldn.create_network` hosting — and therefore
the full M1 bring-up and the full end-to-end gift. Debugged live with `--verbose --capture`,
iterating ("mirror by symmetry, tune live"). Setup as for trades: root, NetworkManager stopped,
`--phy` selected. Console flow: **Mystery Gift → Wonder Cards → Friend → [A] on our entry** (the
"Friend" option = the serial-0x0002 friend path; "Wireless Communication" = the 0x7F7D distributor
path, which is not injectable).

---

## 3. Milestones, steps, and how each is tested

Legend — status: ✅ done · 🔨 built, hardware-gated (needs the Switch to verify/tune) · ⬜ pending.
Tier: **1** offline · **2a/2b** mGBA · **3** Switch.

### Milestone 1 — Link bring-up (console joins us → `gReceivedRemoteLinkPlayers`)

| Step | Status | Files | Test tier & method |
|---|---|---|---|
| Beacon strategy decided — FRIEND path (serial `0x0002` + activity 21, manual A-press) | ✅ | (decision; see §1) | — design decision, not code |
| Parent 0x54 framing primitives (`wrap_t_parent`, `build_accept`/`disconnect`, 3-byte PARENT LLSF, 70-byte echo table) | ✅ | `gbaframe.py`, `rfu.py` | **1** round-trip vs `parse_in`; **3** live in bring-up |
| Parent opcode payloads (`0x7700` SEND_PLAYER_IDS, `0xA100` SEND_BLOCK_REQ NONE, `LinkPlayerBlock`) | ✅ | `rfu.py`, `linkplayer.py` | **1** byte-assert vs `RfuPrepareSendBuffer`; **3** live |
| Parent NI sender (join status `RFU_STATUS_JOIN_GROUP_OK (5)`) + receiver (ack the child's game-data NI) | ✅ | `ni.py` | **1** byte-exact — sender frames verified vs the child's recv-ack capture (`8006/0007/800a/000e`); receiver reassembles the 26-byte game data; **3** real handshake |
| Beacon encoder `frlgsim/beacon.py` (friend: serial `0x0002`, `activity = 21`, gname/uname; invert `_dump_beacon`) | 🔨 | `frlgsim/beacon.py` | **1** first cut ✅ (b85 + record round-trip through `_dump_beacon`); **3** LIVE-TUNE field packing / Pia header until the console lists us (HW-A) |
| HostTransport via `ldn.create_network` / `APNetwork`; generate SSID; `set_application_data` | 🔨 | `frlgsim/transport.py`, `frlgtrade_host.py` | **1** import/smoke ✅; **3** run the production trade host to confirm the card can AP-host (HW-0); Mystery Gift discovery still requires its activity-21 beacon |
| Pia HOST FSM (Net `0x11`/`0x50`, Session acceptance, RTT, property updates) | ✅ | `frlgsim/host_pia.py` | **1** message/FSM tests; **3** proven by the Direct Corner host |
| Dedicated parent composition without a user-selectable wire role | ✅ | `frlgsim/host_session.py`, `frlgsim/rfu_leader.py`, `frlgsim/host_trade.py` | **1** host regressions; **3** live Direct Corner trade |
| **Verify M1 on hardware** — console joins our beacon, player exchange, reach `gReceivedRemoteLinkPlayers` | ⬜ | — | **3** ONLY (needs the Switch) — the M1 gate |

**M1 gate (the "Verify M1" row):** console auto-lists us, A-press connects, our logs show
`0x7700`/`0xA100` sent, the console's `LinkPlayerBlock` received, `gReceivedRemoteLinkPlayers` reached,
no `CB2_LinkError`.

**Host bring-up — the earliest hardware checkpoints (run `frlgtrade_host.py` as root):**
- **HW-0 (can the card AP-host?): ❌ FAILED — root cause CONFIRMED (re-tested 2026-08-07).** The only
  adapter (GenBasic dongle = **MT7601U**, `mt7601u`) registers only `managed` + `monitor` with
  nl80211 — **no AP mode**, so `create_ap` → `NL80211_CMD_NEW_INTERFACE(IFTYPE_AP)` → kernel
  `EOPNOTSUPP` (errno 95), always. This is a **driver capability, not a setting** (the chip does
  SoftAP under Windows vendor drivers, but the mainline Linux driver never implemented it; the
  kernel's "software interface modes: monitor" line confirms AP can't be layered on). The child/JOIN
  path is unaffected (managed mode only).
- **Adapter purchase required.** Shortlist (corrected by this repo's own README tables):
  **ALFA AWUS036ACM** (MT7612U/`mt76x2u`) or **ALFA AWUS036ACHM** (MT7610U/`mt76x0u` — the model the
  README rates *High* for the join path). **AVOID AR9271/ath9k_htc** (in the README's *Known
  Problematic* table) and Intel iwlwifi. Pre-purchase: verify the exact hardware revision's chipset,
  then that its driver sets `BIT(NL80211_IFTYPE_AP)` (elixir.bootlin.com; `mt76x02_util.c` covers
  both mt76 picks) and has monitor-TX/injection reports. **On arrival (30 s):**
  `iw phy <phyX> info` → `* AP` under Supported interface modes, then the production host command.
- The MT7601U remains useful as the **second radio: a sniffer** (`sniff.py`).

**Hosting debug harness (built 2026-08-07; all offline-tested, 24/24):**
- **Preflight** — `transport.preflight_host()` runs before `ldn.create_network` and turns the ENOTSUP
  wall into ONE clear verdict line (`frlgtrade_host.py --skip-preflight` to bypass). Unit-tested against
  canned `iw` output for both the MT7601U (reject) and an mt76 (accept), plus the live negative case.
- **`frlgtrade_host.py --verbose`** enables detailed host/protocol logging; **`--capture FILE`**
  records a byte/action JSONL trace via `frlgsim/ldntrace.py`, which observes
  the live `APNetwork` (our code, no fork): advertisement bytes (once + on nonce change), our raw
  `application_data`, each auth request/response (hex + MAC + status), join/leave events, data frames
  (first 20 each way + counters), and every UDP :12345 datagram both directions.
- **`sniff.py`** — air-side ground truth on the MT7601U: monitor vif on the host's channel, prints
  every LDN advertisement action frame (`7f 00 22 aa 04 00 01 01`) with source MAC + hexdump on
  change, `--mgmt` adds probe/assoc/auth counts (is the console scanning? trying to join?), `--pcap`
  archives for Wireshark. Note: advertisement bodies are encrypted — for a decrypted beacon use the
  join path's `_dump_beacon`; the sniffer proves presence/cadence/source.
- **Vif assertions** — after bring-up, `HostTransport._assert_vifs()` verifies the README design is
  live: `ldn`=AP (mgmt/auth), `ldn-mon`=monitor (advertisement + data frames), `ldn-tap`=tap (our UDP
  plane). The `zz-ldn-unmanaged.conf` in the README already covers all three names. **Trap:** do not
  split AP and monitor across phys — data frames carry the monitor's MAC as source/bssid, which must
  match the AP the console associated to.
- **`LDN/`** — gitignored reference clone of kinnay/LDN @ `39d0b20` (= v0.0.17, verified byte-identical
  to the installed venv package), matching the `pokefirered/` convention.

**Debug runbook (next hardware session, staged gates):**
1. Preflight passes on the new adapter.
2. `sudo -E ./venv/bin/python frlgtrade_host.py --live --verbose --capture host.jsonl PARTY1.pk3 PARTY2.pk3`
   → "AP up" + 3-vif check clean. This advertises Direct Corner; use the future Mystery Gift entry
   point with the activity-21 beacon when testing whether the Friend list shows a gift distributor.
3. `sudo sniff.py --channel <same>` on the MT7601U sees our advertisements (~10/s). Silence =
   monitor-TX/injection problem despite "AP up".
4. Console → Mystery Gift → Wonder Cards → Friend: listed? If not → beacon content (capture a real host's
   `application_data` via the join path's `_dump_beacon`, then `beacon.mutate_beacon` /
   `--beacon-hex`; `sniff.py --mgmt` shows whether the console is even probing).
5. A-press → trace shows `auth_req`/`auth_resp` → `*** CONSOLE JOINED ***`.
6. Post-join `udp_in` records in the trace = the console's first Pia datagrams — the ground truth
   for building the Pia host FSM.
- **HW-A (does the console list us?):** with the AP up, on the console go Mystery Gift → Wonder Cards →
  **Friend** (not Wireless Communication) and watch for our entry. The synthesized beacon is a first cut; iterate `beacon.py`
  (or pass `--beacon-hex` from a captured real host beacon, then `beacon.mutate_beacon`) until listed.
- A `*** CONSOLE JOINED ***` line in the log = we got a step past HW-A (the LDN association).

### Milestone 2 — MysteryGiftLink transport + server script driver

| Step | Status | Files | Test tier & method |
|---|---|---|---|
| MysteryGiftLink transport (6-byte `{ident,crc,size}` header, ≤252B chunks, block-ack pacing, CRC16) | ⬜ | `mystery_gift.py` | **1** loopback vs a Python client model (chunking, CRC, reassembly); **3** live — note the `crc16()` primitive itself is already ✅ (see §4) |
| Parent block-send path (`0x8800`×3 + `0x8900\|n` 12B chunks; ~4-frame received-flag defer) | ⬜ | `mystery_gift.py`/`rfu.py` | **1** against the model; **2b**/**3** live |
| Server script driver (send CLIENT_SCRIPT `SendGameData` → recv GAME_DATA(17) → CLIENT_SCRIPT `SaveCard` → CARD(22) → RAM_SCRIPT(25) → recv READY_END(20)) | ⬜ | `mystery_gift.py` | **1** drive a Python `MysteryGiftClient` model end-to-end; **3** live |

**Note:** M2 is heavily **Tier-1 testable** — the entire server/client conversation can run in-process
against a faithful Python model of `MysteryGiftClient` before any hardware.

### Milestone 3 — Payload + clean close + save

| Step | Status | Files | Test tier & method |
|---|---|---|---|
| `WonderCard` (332B) builder — passes `ValidateWonderCard` | ✅ | `wonder_card.py` | **1** size + field-range asserts |
| Delivery RAM script (`giveitem LANSAT,1; setflag 0x2AA; endram`) | ✅ | `wonder_card.py` | **1** byte-exact vs event.inc opcodes |
| **Deliveryman actually gives the berry** (card+script effect in-game) | ⬜ | (save/RAM injection tooling) | **2a** mGBA save/RAM injection → walk to deliveryman → berry + one-shot; **3** final end-to-end |
| Clean close (`0x5F00 READY_CLOSE_LINK`; drive `0x6600` standby counter) | ⬜ | `mystery_gift.py`/`rfu.py` | **3** live (console self-disconnects, force-saves) |

### Engine & CLI wiring (spans M1–M3)

| Step | Status | Files | Test tier & method |
|---|---|---|---|
| `MysteryGiftEngine` (`mg_engine.py`) implementing the `Sim` duck-typed interface; wire into `make_engine`/`run_live` + CLI `--host` | ⬜ | new `frlgsim/mg_engine.py`, `frlgtrade.py`, `sim.py` | **1** import/smoke + duck-type conformance; **3** live end-to-end |

### End-to-end (Tier 3)
Console shows "Wonder Card received", force-saves, deliveryman later hands over the Lansat Berry;
talking again does nothing (one-shot). This is the only fully-Switch-gated result.

---

## 4. Progress so far (built & tested this session)

**All offline, byte-verified against the decomp; 17/17 tests + a regression smoke test pass.**

New files:
- `frlgsim/mystery_gift.py` — `crc16()` (proven equal to the game's table-driven `CalcCRC16WithTable`,
  util.c:250), MG_LINKID idents, buffer sizes, Wonder Card enums.
- `frlgsim/wonder_card.py` — `build_wonder_card()` (332B, passes `ValidateWonderCard`),
  `build_delivery_ram_script()` (byte-exact deliveryman script), `build_lansat_berry_gift()`.
- `frlgsim/beacon.py` — host beacon encoder (first cut): `b85_encode` (inverse of `_b85_decode`),
  `build_beacon` / `build_record` / `game_data_word`, `mutate_beacon` (clone a real capture + tweak).
- `frlgtrade_host.py`, `frlgsim/host_app.py`, `frlgsim/host_beacon.py` — maintained production host
  path for HW-0, LDN/Pia diagnostics, and trade discovery. It replaces the removed spike harness;
  a Mystery Gift entry point must supply the activity-21 beacon before it can serve as HW-A for MG.
- `tests/test_mystery_gift_offline.py` — the Tier-1 suite (21 tests).

Modified (purely additive; existing child/trade path unaffected — verified):
- `frlgsim/transport.py` — `HostTransport` (the `ldn.create_network` / `APNetwork` hosting counterpart
  of `LiveTransport`: beacon broadcast, join-event logging, UDP data plane over `ldn-tap`).
- `frlgsim/gbaframe.py` — `wrap_t_parent`, `build_accept`, `build_disconnect`.
- `frlgsim/rfu.py` — PARENT LLSF shifts, `parent_uni_slot`, `parent_ni_llsf`, `pack_recv_cmds`,
  `send_player_ids_words`, `send_block_req_words`, `BLOCK_REQ_SIZE_NONE`.
- `frlgsim/ni.py` — `_ni_send_sequence` (shared, faithful librfu NI walk; cross-checked to reproduce
  the byte-verified child `NISender`), `ParentNISender` (delivers the join status), `ParentNIReceiver`
  (acks + reassembles the child's game data), `decode_child_ni_slot`, `parent_recv_ack_slot`.

What the tests prove: CRC16 correctness (+ regression anchors), Wonder Card size/validation +
rejection of bad fields, the exact Lansat RAM-script bytes, flagId→receipt-flag mapping, the 70-byte
echo table round-tripping through `parse_in` into 5 mpId rows, the `0x7700`/`0xA100` payloads, the
60-byte `LinkPlayerBlock` magics, and the **parent NI handshake**: the shared NI sequence reproduces
the verified child sender; the parent join-status frames, once wrapped in a HOST `'T'` and parsed,
make the child's `NIReceiver` emit exactly the reference-capture acks (`8006/0007/800a/000e`) and read
status 5; and the `ParentNIReceiver` acks the child's game-data NI and reassembles the 26-byte
`RfuGameData` (trainer id + uname).

### Reproduce
```bash
.venv/bin/python tests/test_mystery_gift_offline.py   # 13/13 passed
.venv/bin/python -m frlgsim.mystery_gift              # crc self-test
.venv/bin/python -m frlgsim.wonder_card               # payload self-test
```

---

## 5. Open questions & risks

- **WiFi AP mode:** does the card support hosting (`ldn.create_network`)? Station-mode compatibility
  (README table) may not carry over. Confirm early in M1 (Tier 3).
- **Pia host handshake:** the biggest unknown; built by symmetry, no reference capture. Tier-3 iterate.
- **Beacon LDN byte order:** the Switch LDN glue isn't in the decomp; record bytes `[10:20]` and exact
  field order are inferred and must be tuned live. Only `activity` visibility + serial `0x0002`
  synthesis are needed for the friend path.
- **Use rev10 (NSO) timings** everywhere (name-accept 360, connect 480, recovery 720) vs the Switch build.
- **Parent must echo the child's own command row every frame** or the console's MG send stalls.
- **mGBA Tier-2b viability** (RFU peer) is unproven; Tier-2a (payload injection) is the reliable emulator win.

---

## 6. Suggested next step

Two Tier-1/Tier-2 tasks can proceed **without the Switch** and de-risk the most:
1. **M2 transport + server driver against a Python `MysteryGiftClient` model** (Tier 1) — validates the
   whole gift conversation in-process.
2. **mGBA payload injection** (Tier 2a) — proves the Lansat Berry is actually delivered by the
   deliveryman from our exact card + RAM-script bytes.

The remaining M1 host layers (Pia host FSM, HostTransport, beacon visibility) are Tier-3 and best done
live at the console.
