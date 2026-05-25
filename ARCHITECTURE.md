# Sluicegate Architecture: High-Performance Edge Stream Design

This document details the underlying architectural layers, format specifications, and reactive mechanics of the Sluicegate edge storage and broadcasting engine.

---

## 1. Unified Flat-File Storage Format

Sluicegate avoids heavy database engines. Instead, it serializes JSON telemetry streams directly into a contiguous POSIX flat-file. To support ultra-fast bidirectional seeks, log traversals, and boundary recovery, every record is wrapped in a highly succinct wrapper:

```
+---------------------------------------------------------------------------------------------------+
| {"nxt": <nxt_val>, "ts": <unix_float>, "src": "<ip:port>", "data": <raw_json>, "prv": <prv_val>}, |
+---------------------------------------------------------------------------------------------------+
```

### Layout Elements
* **`nxt`**: The logical record size (in bytes), including the wrapper and trailing comma separator. It acts as an absolute forward seek offset: `next_record_offset = current_offset + nxt_val`.
* **`ts`**: High-precision Unix Epoch float timestamp (e.g., `1779821040.123456`) generated on disk sync.
* **`src`**: Telemetry emitter origin (e.g., `"127.0.0.1:4892"` or `"admin-portal"`).
* **`data`**: Raw nested JSON payload (e.g. `{"temp": 24.5, "status": "ok"}`).
* **`prv`**: Identical to `nxt`. The trailing position enables walking backward from any random offset: `previous_record_offset = current_offset - prv_val`.

---

## 2. Dynamic Disk Reclamation & Seek Mechanics

### Zero-Copy Head Reclamation (POSIX Hole Punching)
To limit disk space consumption without incurring expensive file rewrites or memory copy overheads, Sluicegate leverages POSIX sparse file operations:
* When physical allocations exceed limits (`MAX_BLOCKS`), or records expire (`MAX_AGE_MIN`), the core calculates block-aligned deallocation sectors from the stream's logical start (`DATA_START`).
* It invokes the kernel via `fallocate(fd, mode=PUNCH_HOLE | KEEP_SIZE, offset, length)`.
* This zeroes the targeted sector region and immediately reclaims physical sectors, returning them to the OS free block pool. The logical file size itself remains unchanged, preventing writing pointer race conditions.

```
       [ Punched Sparse Hole (Zeroed) ]             [ Active Telemetry Stream ]
       +--------------------------------------------+-----------------------------+
Disk:  | 0x00 0x00 0x00 0x00 ... 0x00 0x00 0x00 0x00 | {"nxt":480,"ts":177...      |
       +--------------------------------------------+-----------------------------+
       0                                            ^                             ^
                                              DATA_START                         EOF
```

### Logarithmic Time-Based Pruning
To trim records older than a dynamic age cutoff in massive streams:
* Sluicegate avoids scanning records sequentially (which is $O(N)$ and slow).
* Instead, it executes a block-aligned **binary search** ($O(\log N)$) between `DATA_START` and the active file `size`.
* On each mid-point, it seeks forward to the next valid record boundary, parses its timestamp (`ts`), and compares it with the cutoff epoch.
* This isolates the exact record boundary that divides expired and active events in milliseconds.

### Relative Seeks from EOF
To support historical catch-up queries like "re-stream the last 10 events", Sluicegate utilizes trailing backward pointers:
1. Starts at active `file_size` (EOF).
2. Seeks backward by 48 bytes (enough to read the trailing `"prv": <prv_val>},` structure).
3. Parses the record size from the `EOD` regex pattern: `r'.*"prv":(\d+)},$'`.
4. Decrements the cursor offset by `prv_val`.
5. Repeats `N` times to discover the exact absolute offset of the N-th record from the end.

---

## 3. High-Performance FastCGI Ingestion

The compiled C daemon (`ingest_fcgi`) compiles with `-O3` and links to `libfcgi`. It handles the telemetry hot path:
* **Scatter-Gather I/O (`writev`)**: Rather than allocating large memory blocks to concatenate wrapper strings and payload bodies, the C daemon populates a structured array of `iovec` descriptors.
* It passes these vectors directly to the `writev` system call, allowing the OS kernel to gather and write the packet fragments from separate memory segments to the storage file in a single atomically synced I/O transaction.

---

## 4. Reactive Broadcasting & Coalesced SSE Loop

The Sluicegate API Server (`src/server.py`) manages concurrent HTTP GET `/stream` subscribers reactively:

```
                             [ server.py (API Server) ]
                                         |
     +-----------------------------------+-----------------------------------+
     |                                   |                                   |
[ Inotify Directory Watcher ]     [ In-Memory Client Queues ]      [ Active Connection Tasks ]
     |                                   |                                   |
     v                                   v                                   v
Catches IN_MODIFY & IN_ATTRIB     Event-driven coalescing          Maintains task set, canceling
metadata attribute updates        loop drains queue directly       dangling queues during
on stream files reactively.       to subscriber TCP sockets.       shutdowns cleanly.
```

### The Coalesced Event Loop
* Spawning separate broadcast tasks for thousands of rapid filesystem modifies would choke the event loop with task-scheduling overhead.
* Sluicegate solves this by creating a single persistent background `_broadcast_loop` task per active topic, controlled by an `asyncio.Event` flag.
* Directory watchers (`DirectoryInotifyWatcher` watching for creations, modifications, and xattr attribute changes via `IN_MODIFY | IN_CREATE | IN_ATTRIB`) simply set this event.
* The broadcast loop wakes up, clears the event, reads all newly appended records in a single coalesced sweep, and pushes them to client queues concurrently, reducing async scheduling overhead to $O(1)$ saturation.

### Inode Configuration Updates & Immediate GC
* When the Admin Portal submits a configuration update, the REST API invokes `setxattr` to alter `user.SGC.MAX_BLOCKS` or `user.SGC.MAX_AGE_MIN` directly on the stream file's inode.
* The Linux kernel generates an `IN_ATTRIB` inotify signal.
* Sluicegate catches the `IN_ATTRIB` event reactively, triggers a config reload, and instantly runs `evaluate_retention()`. This allows tighter retention bounds to immediately punch sectors and deallocate files with zero CPU polling delay.
