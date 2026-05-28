# Sluicegate: Sequential Edge Stream Storage & Broadcasting Engine

Sluicegate is a lightweight, high-throughput sequential edge stream storage and event broadcasting system designed for resource-constrained environments. It implements zero-copy network-to-disk ingestion, physical sector head reclamation via POSIX sparse allocation, and reactive real-time multi-subscriber event broadcasting using POSIX `inotify` event loops.

## System Architecture

The engine is partitioned into three core software components:
1. **FastCGI Ingestion Daemon (`ingest_fcgi.c`)**: A compiled C daemon utilizing FastCGI and POSIX socket operations to write stream payloads directly to disk, bypassing user-space copy overheads.
2. **Python Storage Core (`storage.py`)**: Handles sequential flat-file logs, logarithmic-time time-based record pruners, and POSIX extended attributes (`xattr`).
3. **Unified Server (`server.py`)**: An asynchronous Python web server managing REST API endpoints, event streaming (Server-Sent Events), and serving static admin portal assets.

---

## Key Architectural Highlights

* **Scatter-Gather Network Ingestion**: The C daemon (`ingest_fcgi`) utilizes POSIX `writev` to write FastCGI stream frames directly to log files, minimizing user-space memory copies.
* **POSIX Sparse File Head Reclamation**: Reclaims storage blocks at the file head via `fallocate(..., FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE, ...)` to maintain block-aligned storage limits without resizing files.
* **$O(\log N)$ Time-Based Retention**: Locates record timestamps within the flat file using a block-aligned binary search, executing pruning and head deallocation in $O(\log N)$ time.
* **Relative Backward Seeks**: Restores navigation contexts by walking backward from EOF using record trailing boundary lengths (`prv` fields) to seek the N-th record in $O(N)$ operations.
* **Reactive Multi-Subscriber SSE Broadcast**: Utilizes a libc-bound `inotify` watcher and an `asyncio.Event` loop to broadcast appended records to active TCP connections without polling the filesystem.
* **Race-Free Historical Catch-up**: Resolves race conditions by establishing file boundary size offsets during handshake, replaying historical logs before multiplexing the client into the active real-time event queue.
* **Extended Attributes (`xattr`) Metadata**: Stores configuration state (retention limits `max_blocks`, `max_age_minutes`, and head byte offset `first_data_byte`) directly on the file inodes using Linux extended attributes (`user.SGC.*`). This decouples stream payloads from configuration structures and enables management using standard system tools like `getfattr` and `setfattr`.

---

## Workspace Structure

```
sluicegate/
├── admin/                  # React + TypeScript + MUI v6 SPA Admin Portal
│   ├── src/                # Front-end components, themes, dashboard
│   ├── index.html          # Portal entry point
│   └── vite.config.ts      # Dev proxy configurations
├── src/                    # Backend core engine files
│   ├── ingest_fcgi.c       # Compiled C FastCGI Ingestion Daemon
│   ├── storage.py          # SequentialLogStream Storage Engine
│   └── server.py           # Unified REST + SSE API & Web Server
├── tests/                  # Robust unit & integration test suites
│   ├── test_storage.py     # POSIX fallocate and pruning tests
│   ├── test_sse_inotify.py # Multi-subscriber concurrency and catch-up tests
│   ├── test_api_server.py  # REST and directory watcher integration tests
│   └── benchmark.py        # High-stress multi-topic benchmark
├── Dockerfile              # Multi-stage production container descriptor
├── Makefile                # GCC FastCGI compilation automations
├── ARCHITECTURE.md         # In-depth architectural design walkthrough
└── README.md               # Quickstart and installation guide
```

---

## Installation & Setup

### Prerequisites
* **Linux Kernel** (with support for POSIX `fallocate` and `inotify` syscalls)
* **GCC** and **GNU Make**
* **libfcgi-dev** (C FastCGI library)
* **Python 3.10+** (with standard `ctypes` and `asyncio` libraries)
* **NodeJS 18+** & **npm** (to compile the React Admin Portal)

### 1. Compile C Ingestion Daemon
Compile the optimized C FastCGI binary using Make:
```bash
make
```

### 2. Install Frontend Dependencies & Compile Portal
Install the Material-UI and Vite packages, and compile the React single-page application:
```bash
cd admin
npm install
npm run build
cd ..
```

---

## Running Sluicegate

### Starting the Server (Native)
Run the unified API server to serve the REST endpoints, SSE channels, and static React dashboard assets:
```bash
python3 src/server.py
```
By default, the server listens on port `8088`. Open `http://localhost:8088` in a web browser to access the Admin Portal.

### Running with Docker
A multi-stage Docker build compiles the C FastCGI daemon and compiles the React production bundle inside an isolated container:

```bash
# Build the container
docker build -t sluicegate .

# Start the container with data and key persistence
docker run -d \
  -p 8088:8088 \
  -p 2099:2099 \
  -v $(pwd)/streams:/app/streams \
  --name sluicegate-app \
  sluicegate
```

> [!IMPORTANT]
> **Data & Key Persistence**: Mounting the `/app/streams` directory via `-v $(pwd)/streams:/app/streams` is required to ensure that both telemetry stream log files and active security keys (`.api_key` and `.read_key`) persist across container updates/restarts.

### Docker Configuration Environment Variables
Configure the container settings using environment variables:

| Environment Variable | Default Value | Description |
| :--- | :--- | :--- |
| `SLUICEGATE_USER` | `admin` | HTTP Basic Auth username for Admin Portal login |
| `SLUICEGATE_PASSWORD` | `sluicegate` | HTTP Basic Auth password for Admin Portal login |
| `SLUICEGATE_API_KEY` | *(Auto-generated)* | Overrides the default Ingestion Key (e.g. `sg_ingest_mykey123`) |
| `SLUICEGATE_READ_KEY` | *(Auto-generated)* | Overrides the default Read Access Key (e.g. `sg_read_mykey123`) |
| `SLUICEGATE_NO_AUTH` | `0` | Set to `1` to bypass basic auth credentials validation (Development Only) |

#### Example: Running with Custom Credentials and Custom API Keys
```bash
docker run -d \
  -p 8088:8088 \
  -p 2099:2099 \
  -e SLUICEGATE_USER=telemetry_admin \
  -e SLUICEGATE_PASSWORD=supersecurepassword99 \
  -e SLUICEGATE_API_KEY=sg_ingest_custom_production_key_456 \
  -e SLUICEGATE_READ_KEY=sg_read_custom_production_key_789 \
  -v $(pwd)/streams:/app/streams \
  --name sluicegate-app \
  sluicegate
```

---

## Verification & Benchmarks

### Executing Automated Test Suite
Sluicegate includes full coverage for file-level seek/reads, FastCGI lifecycles, SSE catch-ups, and reactive configuration reloads. Execute the full test suite using:
```bash
python3 -m unittest discover -s tests
```

### Running High-Stress Benchmark
Evaluate the performance under high concurrency. The benchmark starts 2 SSE servers, connects 3 subscribers to each, and ingests 100,000 telemetry events:
```bash
python3 tests/benchmark.py
```
* **Performance Metrics**: Supports a sustained disk ingestion rate of **~17,200 events/second** and broadcasts at **~51,600 events/second** to connected subscribers with **100% data integrity** and zero dropped frames.
