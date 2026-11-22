# HTTP/3 over QUIC vs. HTTP/2 over TCP

This project measures HTTP/3 over QUIC against HTTP/2 over TCP in a controlled Mininet network. It compares connection setup, 0-RTT, multi-object page loading, loss recovery, head-of-line blocking evidence, and bulk-transfer goodput.

The testbed runs entirely on one Linux laptop. A client (`h1`) and server (`h2`) are separated by two Linux routers (`r1`, `r2`). The R1--R2 link is configured with delay, random packet loss, and a bandwidth limit. Caddy serves identical content over:

- HTTP/2 with TLS 1.3 on TCP port 443
- HTTP/3 with QUIC and TLS 1.3 on UDP port 443

## Topology

```text
h1                         R1                R2                         h2
10.0.1.10/24 --- 10.0.1.1 --- 10.0.12.1/30 --- 10.0.12.2/30 --- 10.0.2.1 --- 10.0.2.10/24
 client               netem + tbf on the R1--R2 link                   Caddy server
```

Static routes:

- `h1`: default route via `10.0.1.1`
- `r1`: `10.0.2.0/24` via `10.0.12.2`
- `r2`: `10.0.1.0/24` via `10.0.12.1`
- `h2`: default route via `10.0.2.1`

## Repository layout

```text
topology.py                         Mininet topology and static routes
Caddyfile                           Caddy HTTP/2 and HTTP/3 configuration
content/                            Small object, 100 page objects, and 50 MB bulk object
experiments.json                    Main experiment profile matrix
handshakes.json                     Dedicated handshake profile matrix
scripts/run_experiments.py          Automated runner, capture, and measurement controller
scripts/analyze_traces.py           Trace and transport-gap analysis
scripts/analyze_delivery.py         Application delivery-pause correlation
scripts/build_hol_evidence.py       HOL / independent-stream evidence builder
scripts/finalize_results.py         Verified-result aggregation and 95% CI plots
results/final-instrumented-evaluation/
                                    Final summary CSV files and plots
wiresharkevidence/                  Annotated Wireshark screenshots
```

## Prerequisites

Use Ubuntu or another Linux distribution that supports Mininet and Linux traffic control (`tc`). The runner must run with `sudo` because it creates network namespaces, changes qdisc settings, and captures packets.

Install system packages:

```bash
sudo apt update
sudo apt install -y mininet iproute2 ethtool tcpdump tshark wireshark-common \
  python3 python3-venv python3-pip build-essential libcurl4-openssl-dev
```

Create the analysis environment:

```bash
python3 -m venv .venv
.venv/bin/pip install pandas matplotlib scipy scapy
```

The runner expects project-local executables in `.tools/bin/`:

- `caddy` with HTTP/3 support
- `curl` built with `ngtcp2` and `nghttp3` HTTP/3 support
- `h2load`
- `page_client`, compiled from `scripts/page_client.c`

`.tools/` is intentionally ignored by Git because it contains local build artifacts. Build or supply compatible versions before running new experiments. Confirm that the custom curl supports HTTP/3:

```bash
.tools/bin/curl -V
```

The output must list `HTTP3` in the Features section.

Compile the page-load client against that same curl build:

```bash
mkdir -p .tools/bin
gcc -O2 scripts/page_client.c -o .tools/bin/page_client \
  $(.tools/bin/curl-config --cflags --libs)
```

## Quick POC

Start the interactive topology:

```bash
sudo /usr/bin/python3 topology.py
```

At the `mininet>` prompt, verify the routed path:

```text
h1 ping -c 3 10.0.2.10
h1 traceroute -n 10.0.2.10
```

Expected route:

```text
10.0.1.1  ->  10.0.12.2  ->  10.0.2.10
```

For a full HTTP/2 and HTTP/3 POC, use the automated runner below. It starts Caddy itself, verifies HTTP/2 and HTTP/3 before each sweep, captures port 443 traffic at both routers, and stores results in a timestamped directory.

## Running experiments

Always inspect the planned jobs first:

```bash
sudo /usr/bin/python3 scripts/run_experiments.py --dry-run
```

Run three repetitions of only the page experiment:

```bash
sudo /usr/bin/python3 scripts/run_experiments.py --experiment page --runs 3
```

Run the full matrix in `experiments.json`:

```bash
sudo /usr/bin/python3 scripts/run_experiments.py --runs 3
```

The runner creates a directory like:

```text
results/automated-YYYYMMDDTHHMMSS-ffffffZ/
```

Each trial folder contains:

- `result.json`: protocol, profile, timings, status, and trace-evidence result
- `r1.pcapng` and `r2.pcapng`: packet captures
- `trace-analysis/`: transport gaps, stream progress, and delivery correlation
- page-client logs and request timings for page runs
- qdisc settings, Caddy log, curl/Caddy version details, and TLS key log

## Handshake, resumption, and 0-RTT

Use `handshakes.json` for the dedicated handshake matrix. It includes cold handshakes, resumed connections, and HTTP/3 0-RTT.

```bash
sudo /usr/bin/python3 scripts/run_experiments.py \
  --config handshakes.json --runs 3
```

For 0-RTT, the runner primes a session ticket, uses curl early data, and fails the trial if the server does not accept it.

## Reproducing the final aggregate results

The final report dataset combines verified bulk, handshake, and instrumented page results. Recreate a new aggregate directory from the completed source sweeps with:

```bash
.venv/bin/python scripts/finalize_results.py \
  --workloads results/automated-20261121T173514-701197Z \
  --handshakes results/automated-20261121T200045-061914Z \
  --pages results/automated-20261121T213013-819626Z \
  --output results/reproduced-final-evaluation
```

This creates `trials.csv`, `summary.csv`, a manifest, and plots with 95% confidence intervals. The submitted final summary and plots are in `results/final-instrumented-evaluation/`.

## Wireshark evidence

Open a capture and configure Wireshark to use the matching run's `server.keys` file:

```text
Edit -> Preferences -> Protocols -> TLS
(Pre)-Master-Secret log filename -> select server.keys
```

Reload the capture with `Ctrl+R`.

Useful display filters:

```text
tcp.port == 443
http2
quic
http3
tcp.analysis.retransmission
quic.long.packet_type == 1
```

The `wiresharkevidence/` directory contains the five report screenshots: HTTP/2 request/response, HTTP/3 multiplexing, HTTP/2 loss/retransmission, HTTP/3 loss/stream evidence, and HTTP/3 0-RTT.

## Final results

The final results are based on 81 verified trials across 27 protocol-condition groups, with three runs per group. Key findings at 100 ms RTT and 50 Mbit/s:

| Metric | HTTP/2 | HTTP/3 |
| --- | ---: | ---: |
| Cold setup time, 0% loss | 210.3 ms | 107.1 ms |
| Page time, 0% loss | 1.890 s | 1.678 s |
| Page time, 2% loss | 58.972 s | 44.620 s |
| Page time, 5% loss | 121.181 s | 73.511 s |
| Bulk goodput, 5% loss | 0.476 Mbit/s | 0.748 Mbit/s |

The `summary.csv` files contain means, sample standard deviations, and two-sided 95% Student-t confidence intervals.

## Security and repository hygiene

Do not commit `server.keys`, `wireshark.keys`, private certificates, raw captures, `.tools/`, `.build/`, `.lab-data/`, or `.venv/`. The `.gitignore` file excludes these local, large, or sensitive artifacts.

## AI-use disclosure

Codex was used to assist with the automated experiment runner, result processing, and report drafting and formatting. The initial Mininet configuration, standalone test runs, experimentation, experimental evaluation, Wireshark review, and evidence annotation were performed manually by the student authors. The authors reviewed the automation and verified the reported measurements against saved experiment outputs.
