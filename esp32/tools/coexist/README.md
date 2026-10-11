<!--
Copyright (c) Meta Platforms, Inc. and affiliates.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# C5 BLE + Wi-Fi coexistence: overlays and bench

Overlays and an on-device bench used to size a resident NimBLE stack next to
Wi-Fi and the Noise session on the ESP32-C5 DevKitC-1, and to measure the
tunnel data path. Load overlays after `sdkconfig.defaults`, each build in its
own directory.

## Defaults this work changed

- `sdkconfig.defaults.esp32c5` (C5 only): lwIP and Wi-Fi heap buffers prefer
  PSRAM (`SPIRAM_TRY_ALLOCATE_WIFI_LWIP`) and the TCP windows double to 32 KiB.
- `CONFIG_HOMEHUB_FAST_GCM` (on for C5 with PSRAM): faster AES-GCM for TLS and
  Noise (`main/fast_gcm.c`, `main/ghash32.c`).
- One TLS record per WebSocket frame and zero-copy tunnel batches, for every
  board (`main/noise_control.cpp`, `main/tunnel_netif.c`).

## Overlays

| Overlay | What it does |
|---|---|
| `sdkconfig.ble-persist` | `CONFIG_HOMEHUB_BLE_PERSISTENT`: NimBLE stays up after setup, basic GATT service (`main/ble_basic.c`), slow advertising |
| `sdkconfig.nimble-tuned` | NimBLE for a gadget that is a GATT server **and** a central (scan, connect, GATT client, `main/ble_central.c`): all roles and SMP kept, 3 connections, trimmed prepare-write queue, lists and logging |
| `sdkconfig.ble-mem` | BLE resident without the RAM cost (after `ble-persist` and `nimble-tuned`): NimBLE host allocations in PSRAM, controller code in flash, BLE and Wi-Fi RX buffers trimmed |
| `sdkconfig.wifi-trim` | No SoftAP or WPA2-Enterprise (-58 KB flash) |
| `sdkconfig.net-internal16` | Bench baseline: the pre-change networking (lwIP in internal RAM, 16 KiB windows) |
| `sdkconfig.tx-by-ref` | Require PSRAM at boot so Wi-Fi transmits lwIP's PSRAM pbufs by reference and window scaling is available |
| `sdkconfig.tcp64`, `sdkconfig.tcp128` | 64 or 128 KiB TCP windows (128 needs `tx-by-ref`) |
| `sdkconfig.ctrl-flash` | BLE controller from flash (-21 KB IRAM; experimental, slows BLE) |
| `sdkconfig.bench`, `sdkconfig.bench-quick` | Boot into `main/pipeline_bench.cpp` instead of the gadget |

A coexistence build (the networking now comes from the C5 defaults):

```sh
idf.py -B build-coexist -DIDF_TARGET=esp32c5 -DSDKCONFIG=build-coexist/sdkconfig \
  -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;tools/coexist/sdkconfig.ble-persist;tools/coexist/sdkconfig.nimble-tuned" build
```

## Bench

`CONFIG_HOMEHUB_PIPELINE_BENCH` prints `BENCH ...` lines: memory copies,
AES-GCM parts and seal/open speed, a cross-check of `main/fast_gcm.c` against
ESP-IDF's GCM, then (with `CONFIG_HOMEHUB_WIFI_SSID`/`PASSWORD` set in the
build's own sdkconfig, never committed) raw TCP, TLS and an emulation of the
tunnel TX path to a host on the LAN, and BLE scan, central connect and
peripheral throughput beside Wi-Fi. `CONFIG_HOMEHUB_BENCH_SOAK_MINUTES` loops
tunnel TX with BLE scanning, TLS RX and BLE central connects instead, and
`CONFIG_HOMEHUB_BENCH_RX_DELAY_MS` delays inbound packets to emulate a WAN
round trip on a LAN.

```sh
python3 -u tools/coexist/bench/bench_server.py &          # on a host on the same LAN
tools/coexist/bench/run_variant.sh build-bench PORT /tmp/bench.log   # flash + live log
python tools/coexist/bench/ble_tput.py --name MuseGadget-XXXXXX   # when the log says wait_ble_client
```

The pipeline cases emulate the session's buffers and copies around the real
production frame sender and the real Noise `Transport`; they are not a VM
session. `full_legacy` is the code before these changes (staging copy into
internal RAM, one TLS record per 512 bytes, ESP-IDF GCM), `full_new` is after.

## Results (C5 DevKitC-1, 2026-10-10)

AES and GHASH (8232-byte frame, `BENCH crypto`/`aead`):

| | MB/s |
|---|---|
| AES-CTR on the AES DMA, internal RAM | 45.8 |
| AES-CTR on the AES DMA, PSRAM (32-byte aligned) | 7.7 |
| GHASH, ESP-IDF-style 4-bit tables / `ghash32` 8-bit tables | 3.6 / 6.0 (66 / 40 cycles per byte) |
| AES-256-GCM seal, ESP-IDF, PSRAM buffer | 2.4 |
| AES-256-GCM seal, `fast_gcm` (8-bit tables, internal staging), PSRAM buffer | 4.1 |
| ChaCha20-Poly1305, software | 2.3 |

Aligning the PSRAM buffers to 32 bytes does not help: ESP-IDF's GCM ran 10%
slower on an aligned buffer, because the aligned path DMAs straight from
PSRAM. Staging through internal RAM is what helps.

Tunnel TX and TLS RX, 5 GHz (channel 40), same run per row:

| Build | RSSI | Raw TCP tx/rx Mbit/s | `full_legacy` | `full_new` | TLS RX before/after |
|---|---|---|---|---|---|
| 16 KB windows (current defaults) | -41 | 9.7 / 11.7 | 2.36 (CPU 76%) | 3.78 (+60%) | 3.74 / 4.93 |
| lwIP in PSRAM + 32 KB (new C5 default) | -38 | 13.1 / 16.2 | 3.61 (CPU 97%) | 4.74 (+31%) | 4.99 / 6.34 |
| lwIP in PSRAM + 64 KB | -41 | 13.0 / 16.4 | 3.79 (CPU 100%) | 6.31 (+66%) | 7.55 / 7.63 |

Runs at RSSI near -68 dBm (the AP's mesh sometimes hands the board to a far
node) are 2-10x slower whatever the build; compare within a row.

Free internal DMA-capable RAM is the limit: with 64 KB windows its minimum
fell to ~1 KB during bulk TX (Wi-Fi copies TX frames into internal buffers),
so 64 KB windows need fewer Wi-Fi TX buffers or by-reference TX before use.

BLE beside Wi-Fi: scanning at 50% duty saw ~33 devices (21-27 reports/s,
55-65 idle); the central connected to a peer in 170-630 ms, MTU 256, 9
services and the Device Name read; notifications to a Mac ran at ~130 kbit/s.
A BLE stream took ~20% off Wi-Fi throughput.

Soak of the new C5 defaults (32 KiB windows, lwIP in PSRAM, fast GCM) with
BLE resident, 25 minutes, 5 GHz at -38 dBm: 79 cycles of tunnel TX beside a
BLE scan, TLS RX, and a BLE central connect every fourth cycle. No failures
(TX 0/79, RX 0/79, central connects 19/19), no resets. Tunnel TX 5.29 Mbit/s
average (4.83 lowest), TLS RX 5.95 (5.55 lowest); lowest free internal RAM
25.5 KiB, lowest free DMA-capable RAM 12.9 KiB.

BLE resident with `sdkconfig.ble-mem`, paired production firmware, idle:

| Build | Internal free (largest) | DMA-capable free (largest) |
|---|---|---|
| Upstream, BLE released after setup | 82 KiB (58) | 69 KiB (58) |
| New defaults + `ble-persist` + `nimble-tuned` | 47 KiB (19) | 35 KiB (19) |
| ... + `ble-mem` | 70 KiB (42) | 54 KiB (42) |

The bench with `ble-mem` (32 KiB windows, no delay) kept BLE scanning (33
devices idle, 30 with Wi-Fi at 5 Mbit/s) and central connects working, ran
tunnel TX at 4.66 Mbit/s and TLS RX at 6.13, and never let free DMA-capable
RAM fall below 46 KiB.

TCP windows at an emulated 50 ms delay (`CONFIG_HOMEHUB_BENCH_RX_DELAY_MS`):
throughput follows the window up to the C5's CPU.

| Window | Raw TCP tx/rx | TLS RX | Tunnel TX | Lowest DMA free |
|---|---|---|---|---|
| 16 KiB, internal | 1.3 / 1.1 | 1.2 | 1.25 | 11 KiB |
| 32 KiB, PSRAM | 2.4 / 2.3 | 2.4 | 2.5 | 1.5 KiB |
| 32 KiB + `tx-by-ref` | 2.5 / 2.2 | 2.4 | 2.5 | 12 KiB |
| 64 KiB + `tx-by-ref` | 4.6 / 4.8 | 4.7 | 4.4 | 12 KiB |
| 128 KiB RX / 112 KiB TX + `tx-by-ref` | 5.2 / 8.5 | 8.7 | 6.0 (CPU 96%) | 11 KiB |

lwIP caps the send buffer near 117 KiB (its send low-water mark is 16-bit).

Through a paired VM's Home Link tunnel, every build (16 or 32 KiB windows,
BLE on or off) ran at 2.1-2.5 Mbit/s LAN to VM and 1.6-1.9 VM to LAN, and
parallel flows shared that total: the limit is on the server path, past the
gadget, not its windows.
