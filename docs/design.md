# FaultBed System Design

## 1. Architecture

```
            ┌─────────────────────── Controller PC ───────────────────────┐
  browser ──┤ Web UI (FastAPI + WebSocket)  ·  CLI  ·  scenario YAML files │
            │ Runner: discover → configure → link-up → peers → clock sync  │
            │         → start → timeline → stop → drain → sync → analyse   │
            │ Metrics: PDR · RTT · one-way latency · goodput · recovery     │
            │          loss attribution · fault audit · reproducibility     │
            │ (Mosquitto broker, only for MQTT)                             │
            └──────┬──────────── USB hub (control + logs, out-of-band) ─────┘
                   │ newline-JSON over serial, 921600 baud
     ┌─────────────┼─────────────┬──────────────┬──────────────┐
  ┌──┴──┐       ┌──┴──┐       ┌──┴──┐        ┌──┴──┐        ┌──┴──┐
  │ESP32│       │ESP32│       │ESP32│        │ESP32│        │ESP32│   ← identical firmware
  └──┬──┘       └──┬──┘       └──┬──┘        └──┬──┘        └──┬──┘
     └──── radio (data plane): ESP-NOW | UDP | MQTT | CoAP ────┘
```

Firmware layers on each node:

```
 main.cpp        serial command loop, JSON config → structs
 ───────────────────────────────────────────────────────────────
 fbcore (portable C++, unit-tested on the PC with g++)
   Node          traffic generator · routing/forwarding · echo/RTT · event log
   IngressGate   virtual-topology filter → FaultEngine → DelayQueue
   FaultEngine   per-directed-link rules, counter-based decisions
   packet/coap   24-byte FaultBed header with CRC-16, minimal CoAP codec
 ───────────────────────────────────────────────────────────────
 Transport plug-ins (src/transports/*.cpp, self-registering)
   espnow · udp · mqtt · coap · <yours>
```

### Adding a new protocol (the extensibility requirement)

1. Create `firmware/src/transports/myproto_transport.cpp`.
2. Subclass `Transport` and implement `name / begin / end / loop / isUp / send`.
3. Pass every received frame to `gate_->admit(frame, len, rssi)`. If the protocol has ACKs,
   consult `admit()` **before** you acknowledge, and call `admitControl(src)` when an ACK arrives.
4. End the file with `FB_REGISTER_TRANSPORT(MyProtoTransport, "myproto");`.

Nothing else changes. The node advertises the new name in `hello`, the controller accepts any
transport name, and the UI falls back to a JSON parameter editor (add a form schema in
`web/server.py: TRANSPORT_PARAMS` if you want nicer inputs). Routing, faults, metrics and the
audit all work because they only ever see FaultBed frames.

Candidates for later: BLE GATT notifications (ESP32 / ESP32-C3), ESP-NOW broadcast flooding,
MQTT-SN, LoRa (an SX127x module on SPI), raw 802.11 vendor frames.

---

## 2. How the user interacts

Three equivalent front doors, all driven by the same **scenario** object:

| Interface | For | Command |
|---|---|---|
| **Web UI** | building experiments, watching live, browsing results | `faultbed serve` → http://127.0.0.1:8000 |
| **CLI** | batch runs, scripting, CI | `faultbed run scenarios/02_protocol_loss_sweep.yaml` |
| **Scenario file (YAML)** | the reproducibility artefact; commit it with the results | edit in any editor, or save from the UI |

Add `--sim 5` to any command to use 5 simulated boards instead of hardware.

### Web UI tabs

1. **Testbed**: connected boards (port, MAC, chip, firmware version, supported protocols, assigned
   node id, link state, IP). *Rescan* and *blink LED* (to find a physical board).
2. **Experiment**: the builder, in order:
   1. General: name, node count, duration, warm-up, drain, repeats, seed, TTL.
   2. Protocol under test, with protocol-specific parameters and Wi-Fi credentials.
   3. Topology: presets or click-to-draw links; drag nodes. *Click a link to set faults on each
      direction.*
   4. Default link faults: applied to every hop unless a link overrides them.
   5. Traffic flows: source, destination, interval, payload, periodic or Poisson, count, start
      offset, echo/RTT. The route each flow will take is shown.
   6. Timeline: kill or revive a node (soft, or hard = radio off), set or clear a link fault at time *t*.
   7. Sweep: any dotted parameter × list of values (e.g. `transport.name` × `faults.default.p_loss`).
   8. A preview of the exact YAML and the exact JSON each node will receive.
   *Validate* runs continuously; *Save* writes YAML; *Run* starts the campaign.
3. **Live**: phase and progress, node status (failed / link up / injected count), per-flow PDR,
   live RTT scatter, event log.
4. **Results**: experiment list. Per experiment: an aggregate table across repeats (mean ± sd, CV,
   audit) and comparison charts. Per run: summary tiles, flow table, PDR over time, RTT scatter
   and CDF, loss attribution, fault audit, real-channel per-hop table, recovery table. CSV and YAML
   download.

---

## 3. Parameters (what you can *set*)

### Experiment
| Parameter | Meaning | Range / default |
|---|---|---|
| `nodes` | node ids (optionally pinned to a board by `mac` or `port`) | 2–31 ids; 4–5 boards in practice |
| `duration_s`, `warmup_s`, `drain_s` | run length; start of metrics window; wait for in-flight packets | 30 / 2 / 3 s |
| `repeats`, `seed`, `vary_seed` | repetitions; master seed; new seed per repeat? | 1 / 1 / false |
| `ttl` | hop limit | 8 |
| `sweep` | `{dotted.path: [values]}` → Cartesian product of runs | – |

### Protocol (`transport.name` + `transport.params`)
| Protocol | Parameters |
|---|---|
| `espnow` | `channel` (1–13), `unicast` (MAC ACK + retries) or broadcast |
| `udp` | `port`, `wifi.ssid/pass` |
| `mqtt` | `host` (broker IP), `port`, `keepalive_s`, `wifi` |
| `coap` | `confirmable` (CON/NON), `ack_timeout_ms`, `max_retransmit`, `port`, `wifi` |

### Topology
`preset`: full · line · ring · star (`center`) · tree · grid · custom (`edges`); `enforce` (false =
everyone hears everyone).

### Link fault rule (default, per directed link `"a->b"`, or scheduled)
| Field | Effect |
|---|---|
| `model` | `none`, `bernoulli` (independent), `gilbert_elliott` (bursty) |
| `p_loss` | Bernoulli loss probability |
| `ge_p_gb`, `ge_p_bg`, `ge_loss_good`, `ge_loss_bad` | GE transition and per-state loss. Mean loss = π_bad·lb + π_good·lg, mean burst ≈ 1/p_bg |
| `p_corrupt` | flip one bit (caught by CRC → counted as corruption loss) |
| `p_duplicate` | deliver twice |
| `delay_ms`, `jitter_ms` | fixed + uniform[0, jitter] extra latency (also causes reordering) |
| `blackout` | link cut |

### Traffic flow
`src`, `dst`, `interval_ms` (≥5), `size` (0–226 B payload), `pattern` (periodic/poisson),
`count` (0 = until stop), `start_ms`, `echo` (request a reply for RTT).

### Timeline events
`kill` (node, `hard`: radio stack off as well), `revive`, `fault` (link or `default`, rule),
`clear_fault`.

## 4. Outputs (what you *get*)

| Metric | Definition |
|---|---|
| PDR | unique delivered / sent within [t0 + warm-up, stop] |
| RTT | echo round trip on the origin's own clock (mean, p50/p95/p99, jitter) |
| One-way latency | rx time − tx time after mapping both node clocks to the host clock |
| Goodput | delivered payload bits / window |
| Duplicates | deliveries of an already-delivered sequence number |
| Loss attribution | injected_loss · injected_corrupt · node_down · link_down · send_fail · drop_ttl/noroute/queue · channel |
| Per-hop channel delivery | frames arriving at the receiver's gate / frames handed to the radio (natural loss) |
| Recovery | after revive or fault clear: time to first delivery, total outage, transport re-link time |
| Fault audit | conformance % (replayed decisions that match) + per-link configured vs measured, CI, p-value |
| Reproducibility | per variant across repeats: mean, sd, CV, 95% CI of every metric |
| Clock | drift (ppm) and sync error bound per node |

Every run directory holds `scenario.yaml`, `meta.json` (boards, firmware, seed, executed
timeline, sync samples), `events.jsonl` (raw), and `metrics.json`. A campaign also has
`campaign.json` and `campaign.csv`.

---

## 5. Hardware, wiring and topology

**No wiring between boards.** Each board connects to the PC through a USB cable and a powered USB
hub. That one cable provides power, control and logging. The boards talk to each other only over
the radio.

```
 Laptop ── powered USB hub ──┬── ESP32 #1
                              ├── ESP32 #2
                              ├── ESP32 #3      all within ~1–3 m of each other
                              ├── ESP32 #4
                              └── ESP32 #5
 (Wi-Fi AP / phone hotspot nearby: only for UDP, MQTT, CoAP)
```

**Mesh / topology question:** physically, every board can hear every other board (one broadcast
domain). Topology is imposed **in software**:
- Each node only accepts frames from its configured neighbours (ingress filter), and
- the controller installs shortest-path next hops, so traffic is relayed hop by hop.

So yes, one physical layout gives any logical topology: line, ring, star, tree, grid, full mesh
or custom, chosen per experiment, with nothing moved. With `enforce: false` you get the raw
physical mesh.

Notes for real deployments:
- **ESP-NOW** is truly peer-to-peer. The Wi-Fi protocols go *through the access point* (physically
  a star), so their virtual multi-hop routes are application-level relays. State this in the report.
- Keep boards on the same channel as the AP. Run ESP-NOW and Wi-Fi experiments as separate runs.
- For *real* multi-hop RF conditions (future work), spread the boards across rooms, or put
  attenuators/shielding on the antennas. The same software then measures both.
- Optional LED/buzzer: the firmware blinks the on-board LED on `identify`. No extra parts needed.

## 6. Limitations (state them in the report)

- Faults are emulated at the frame level after reception, not in the RF channel. Collisions,
  capture effect and interference are only present as natural channel behaviour.
- MQTT runs over TCP. Injected loss acts on delivery to the application *after* the broker, so it
  exercises application-level loss tolerance, not TCP retransmission.
- Serial logging bandwidth (921600 baud) limits event rate to a few hundred packets/s for the whole node.
- The simulator is for developing the software. Report only hardware numbers as results.

## 7. Roadmap

1. Flash 4–5 ESP32s; run `01_baseline` on every protocol; record natural channel numbers.
2. Fault-accuracy validation chapter: sweep p_loss 0–0.5 on one link, ≥10k frames each, plot
   configured vs measured with CIs, and audit conformance.
3. Headline comparison: `02_protocol_loss_sweep` (4 protocols × 4 loss levels × 3 repeats).
4. Failure and recovery: `03_relay_failure` for each protocol.
5. Bursty vs independent loss at equal mean loss: does burstiness hurt CoAP CON more?
6. Reproducibility chapter: the same campaign on two different days / board sets; compare CV.
7. Stretch goals: trace-to-model (fit GE parameters from a measured link and replay them), BLE transport.
