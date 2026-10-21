# Research Notes: Is This a Real Problem, Is It Cheaper, What Is New?

> Status: first pass (Oct 2026). Each claim lists its source. Items marked **[verify]** come from
> secondary pages (search summaries, resellers) or general knowledge. Read the primary source
> before you cite them in the report.

---

## 1. The problem is documented in the literature

| Claim in our problem statement | Evidence |
|---|---|
| Physical IoT testbeds are expensive and hard to build | ICS testbed survey: building and maintaining physical testbeds is expensive, so cost is a barrier ([arXiv 2102.05631](https://arxiv.org/pdf/2102.05631)). IIoT testbed survey: some setups need expensive hardware or many devices ([arXiv 2404.17485](https://arxiv.org/pdf/2404.17485)). |
| Existing testbeds are often unavailable, costly or not open | **OpenIoT-Lab, ACM SenSys 2025 poster** (IIT Ropar / IIT Bhilai / Univ. of Essex). It argues that most state-of-the-art IoT testbeds are unavailable, costly, or closed source, which makes them hard to reproduce ([Essex repository](https://repository.essex.ac.uk/40892/), DOI 10.1145/3715014.3724066). *This is the closest recent statement of our gap. Cite it, and position FaultBed against it (they build a low-cost testbed; we add controlled fault injection and auditing).* |
| Shared remote testbeds have availability and flexibility limits | IIoT survey: shared remote testbeds may not guarantee availability, and some do not let users change software or parameters ([arXiv 2404.17485](https://arxiv.org/pdf/2404.17485)). |
| Reproducibility is a recognised open challenge | Papadopoulos et al., *Importance of Repeatable Setups for Reproducible Experimental Results in IoT* (FIT IoT-LAB) ([HAL](https://hal-supelec.archives-ouvertes.fr/INRIA/hal-01609092v1)). Systematic review of 32 testbed facilities from 359 papers (2011–2021). It lists reproducibility, documentation, ground truth and debugging support as open challenges ([JSAN 12(3):48, 2023](https://doi.org/10.3390/jsan12030048)). |
| Simulators miss real hardware behaviour | Identical experiments in Cooja/MSPSim and on real motes showed timing inconsistencies ([EWSN 2016, "Using Cooja for WSN Simulations: Some New Uses and Limits"](https://www.ewsn.org/file-repository/ewsn2016/319_324_roussel.pdf)). Castalia, Cooja, MiXiM and WSNet produced *different* outcomes when configured the same way ([Stetsko et al., SIMUTOOLS'12](https://www.ics.muni.cz/en/research/publications/977194)). RealSim had to feed Cooja with real link measurements to close the gap ([RealSim](https://www.ccs-labs.org/bib/struebe2011realsim/struebe2011realsim.pdf)). |
| Real links lose packets in bursts | Gilbert-Elliott analysis of a large sensor testbed (CC1101/CC2420): run and loss lengths separate good links from weak ones ([IEEE, via Infona](https://www.infona.pl/resource/bwmeta1.element.ieee-art-000007343635)). 802.11g measurements show strongly fluctuating loss and loss-free periods ([TU Berlin TKN](https://www.tkn.tu-berlin.de/bib/happ2013fault-impact/)). **This is why FaultBed has a Gilbert-Elliott model, not just random loss.** |
| Generic software emulators fall short for this use case | `tc netem` is limited by kernel timer granularity, and its slot model is only a crude approximation of bursty Wi-Fi MACs ([man7 tc-netem(8)](https://man7.org/linux/man-pages/man8/tc-netem.8.html)). It has scalability limits in large virtual testbeds ([arXiv 2208.05862](https://arxiv.org/pdf/2208.05862)). Off-the-shelf netem gave unrealistic results for some applications ([ERRANT, arXiv 2106.06335](https://arxiv.org/pdf/2106.06335)). netem also runs on a Linux interface, not on the MCU radio link between two ESP32s. |

### Prior work closest to ours (position against these)

- **JamLab (IPSN 2011)**: a low-cost add-on that turns sensor motes into repeatable interference generators by recording and replaying RF interference ([TU Delft](https://research.tudelft.nl/en/publications/jamlab-augmenting-sensornet-testbeds-with-realistic-and-controlle)). It injects *RF interference*; we inject *link-level faults* with per-packet determinism.
- **D-Cube / JamLab-NG**: a benchmarking testbed with controllable interference from Raspberry Pi 3 observers attached to nRF52840 boards ([arXiv 2304.00371](https://arxiv.org/pdf/2304.00371)). A capable but much larger and more expensive setup.
- **Model-based network fault injection for IoT protocols** (Yoneyama et al., ENASE 2019): a proxy that injects delays and failures into MQTT client-server software ([SciTePress](https://www.scitepress.org/Papers/2019/76181/)). It works on software, not on hardware radios.
- **MQTT vs CoAP on ESP32 under disruption** (JATI 2026): network disruption is simulated with the Windows tool *clumsy* ([ejournal.itn.ac.id](https://ejournal.itn.ac.id/jati/article/download/18769/9896/)). This is the typical ad-hoc approach: host-side, not per-link, not reproducible packet by packet. **A good example of what FaultBed improves.**
- **Gotham testbed**: a reproducible IoT testbed for security datasets ([arXiv 2207.13981](https://arxiv.org/pdf/2207.13981)). Different goal.

---

## 2. Cost comparison (the "it is cheaper" claim)

### FaultBed bill of materials (5 nodes)

| Item | Qty | Unit (₹) | Total (₹) | Source |
|---|---|---|---|---|
| ESP32-WROOM-32 DevKit | 5 | 285–350 | 1,425–1,750 | IndiaMART listings ₹285–₹350 (Feb 2026); Amazon.in DevKit V1 ₹499 (Oct 2026) **[verify on purchase day]** |
| Powered USB hub, 7-port | 1 | ~600–900 | ~750 | **[verify]** |
| Micro-USB data cables | 5 | ~60 | ~300 | **[verify]** |
| Wi-Fi access point | 0 | – | 0 | An existing router or phone hotspot. Only needed for UDP/MQTT/CoAP. |
| Controller PC + Mosquitto broker | 0 | – | 0 | Existing laptop; Mosquitto is free. |
| **Total** | | | **≈ ₹2,500–2,800 (≈ US$30–35)** | |

### What it is compared with

| Alternative | What you get | Cost | Notes |
|---|---|---|---|
| **Apposite Netropy 10G1** hardware WAN emulator | Wired Ethernet impairment (loss, delay, jitter) | Reseller listing AU$57,799–124,036 ([avcomm](https://avcomm.com.au/product/apposite-netropy-10g1-wan-emulator/)); undated UK MSRP £15,630 ([openreality](https://www.openreality.co.uk/?p=12069)) **[verify current]** | Wired links only. No radios, no IoT protocols, no multi-node topologies. |
| **Keysight Network Emulator 2/3** | FPGA-based Ethernet impairment, 100 MbE–100 GbE | Quote only ([catalog](https://www.keysight.com/au/en/assets/7125-1064/catalogs/Network-Emulators.pdf)) | Same category as Netropy. |
| **D-Cube-style testbed** | Up to 50 nodes, each an RPi 3 observer plus nRF52840 board, RF interference | Roughly ₹6–8k per node (RPi + nRF) × 50 **[verify]** | Excellent, but an institution-scale build. |
| **FIT IoT-LAB** (remote) | Large shared testbed | Free for researchers **[verify]** | Remote, shared, scheduled. Fixed hardware: you can't bring your own ESP32 protocol stack or physically control nodes. |
| **Software emulators** (netem, clumsy, Mininet-WiFi) | Impairment on a PC interface | Free | No real MCU radio, no per-link control between embedded nodes. See §1. |

**Claim that holds:** for under ₹3,000, FaultBed gives per-link fault control between real low-cost
radios across four IoT protocols. Commercial impairment appliances cost about 100–1000× more and
emulate wired Ethernet only. Phrase it as "orders of magnitude cheaper than commercial network
emulators, and unlike free host-side tools it acts on the MCU-to-MCU radio link." Don't claim
that FaultBed replaces RF-level testbeds such as D-Cube: it doesn't reproduce RF physics.

---

## 3. What is new: novelty claims, each backed by code

Search specifically for prior art on each claim before submitting **[verify]**. The implementation
behind each one is listed.

1. **Counter-based deterministic fault injection.** The fate of the *k*-th frame on directed link
   A→B is a pure function `f(seed, A, B, k)`, computed with SplitMix64. Traffic on other links,
   timing, and which other faults are enabled cannot change it. Two runs with the same seed drop
   exactly the same frames, even when the real radio behaves differently.
   (`firmware/lib/fbcore/fb_hash.h`, `fault_engine.cpp`; test
   `test_same_seed_same_faults_despite_different_channel`). Tools such as netem draw from a shared
   RNG stream, so one extra packet shifts every later decision.
2. **Self-auditing fault injection.** Every arrival and its verdict is logged. After the run, an
   independent Python implementation replays each decision and reports *conformance*, which must
   be 100%. Each link also gets a statistical test: configured vs realised rate, burst-corrected
   confidence interval, and p-value. This proves the testbed did what the scenario said, which is
   the "trickiest part" in the proposal. (`controller/faultbed/metrics.py: audit()`)
3. **Per-packet loss attribution.** Each lost packet is traced along its route and blamed on
   injected loss, injected corruption, a failed node, a reconnecting link, a transport send
   failure, or the real channel. This separates the testbed's faults from the radio's own
   behaviour and gives the per-hop natural channel delivery ratio for free. (`metrics.py: _blame,
   _link_delivery`)
4. **Software-defined virtual topologies on co-located boards.** All boards sit on one desk. The
   controller decides who may hear whom (an ingress neighbour filter) and installs shortest-path
   routes. Line, ring, star, tree, grid, mesh or a custom graph is a change in the scenario file,
   with no rewiring or moving boards. (`topology.py`, `Node::admit`)
5. **Faults injected below each protocol's reliability layer.** Frames pass the fault gate
   *before* a transport acknowledges them. CoAP ACKs cross the faulty link too, so CoAP
   retransmissions genuinely occur. All four protocols carry the same frame and are measured the
   same way, which is a precondition for a fair comparison. (`link.h` IngressGate,
   `coap_transport.cpp`)
6. **Out-of-band control, logging and clock sync.** Control and logs travel over USB serial, never
   over the radio under test. A min-RTT Cristian exchange before and after each run, plus linear
   drift regression, gives one-way latency on unsynchronised MCUs (sub-ms in tests). (`ClockMap`)
7. **Simulation twin.** A discrete-event model speaks the identical serial protocol, so the whole
   pipeline (UI, orchestration, analysis) is tested in CI without hardware. The simulator
   validates the *software*. It is never presented as a substitute for hardware results.

---

## 4. Reading list to finish (priority order)

1. OpenIoT-Lab poster (SenSys'25): closest related work. Compare features in a table.
2. JSAN 2023 systematic review: use its challenge taxonomy for the motivation chapter.
3. Papadopoulos et al. (FIT IoT-LAB reproducibility): definitions of repeatability and reproducibility.
4. JamLab and D-Cube: how RF-level controlled interference differs from link-level injection.
5. One or two ESP-NOW vs MQTT/CoAP measurement papers, to check our baseline numbers are plausible.
6. Gilbert-Elliott parameter fitting: so you can set GE parameters from a measured link (future work: "trace-to-model").
