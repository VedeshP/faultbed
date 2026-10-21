# faultbed

A Software-Driven Fault-Injection Testbed for Testing IoT Protocols on Low-Cost Hardware.

- **4 protocols** out of the box: ESP-NOW, UDP, MQTT, CoAP. New ones are a single plug-in file.
- **Per-link faults**: random or bursty (Gilbert-Elliott) loss, delay and jitter, corruption,
  duplication, link cuts, and node failures on a timeline.
- **Any topology** (line, ring, star, tree, grid, mesh, custom) with 4–5 boards on one desk, in software.
- **Reproducible by construction**: the *k*-th frame on a link meets the same fate in every run
  with the same seed. Each run is **audited**: every injected decision is replayed and checked.
- **Loss attribution**: separates injected loss from real radio loss, node failure and reconnection.
- Web UI, CLI, and versionable YAML scenarios. A full **simulator** for working without hardware.
- Hardware cost ≈ ₹2,500–2,800 for 5 nodes.

Docs: [architecture](docs/architecture.md) · [design](docs/design.md) · [research & novelty](docs/research.md) · [original proposal](../iot-testbed-proposal.md)

## Repository layout

```
firmware/                 PlatformIO project (one image for every board)
  lib/fbcore/             portable core: fault engine, packet, routing, CoAP codec
  src/transports/         espnow · udp · mqtt · coap  (plug-ins)
  test/                   host unit tests for the core (g++)
controller/               Python package `faultbed`
  faultbed/               runner, scenario, topology, metrics, simulator, web UI
  tests/                  pytest suite (runs entirely on the simulator)
scenarios/                example experiments
docs/                     design + research notes
```

## Quick start without hardware

```bash
cd controller
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cd ..
python -m faultbed --sim 5 serve --scenarios scenarios
```
Open http://127.0.0.1:8000, go to **Experiment → Load → Run**, then watch **Live** and **Results**.

CLI equivalent:
```bash
python -m faultbed --sim 5 run scenarios/04_bursty_link.yaml
```

## With hardware

1. Install [PlatformIO](https://platformio.org/install/cli), then flash every board with the same image:
   ```bash
   cd firmware
   pio run -e esp32dev -t upload --upload-port COM5
   ```
   Use `-e esp32-c3` for ESP32-C3 boards.
2. Plug all boards into a powered USB hub and check them:
   ```bash
   python -m faultbed boards
   ```
3. For UDP/MQTT/CoAP: put your Wi-Fi SSID and password in the scenario. For MQTT, also run
   Mosquitto on the PC and set `host` to the PC's IP address.
4. Start the UI:
   ```bash
   python -m faultbed serve
   ```
   Or run a scenario with `python -m faultbed run scenarios/01_baseline.yaml`.

## Tests

```bash
python firmware/test/run_native_tests.py
cd controller && python -m pytest
```
The first command runs the firmware core on the PC (10k+ checks, fault accuracy, CoAP codec,
3-node routing). The second runs the controller suite, including end-to-end runs for all four
protocols on the simulator.

## Adding a protocol

See [docs/design.md §1](docs/design.md#adding-a-new-protocol-the-extensibility-requirement): one
file in `firmware/src/transports/` with `FB_REGISTER_TRANSPORT(MyTransport, "name")`.
