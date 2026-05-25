#!/usr/bin/env python3
import os
import sys
import time
import struct
import ctypes
import ctypes.util
import asyncio
import json
import shutil
import unittest

# Append workspace src directory to import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))
from storage import SequentialLogStream

# Inotify Event Constants
IN_MODIFY = 0x00000002

class InotifyWatcher:
    """
    Zero-dependency, high-performance Linux kernel inotify event watcher.
    Integrates seamlessly with asyncio by registering its fd with the event loop.
    """
    def __init__(self, filepath, callback, mask=IN_MODIFY):
        self.filepath = os.path.abspath(filepath)
        self.callback = callback
        self.mask = mask
        
        # Load C library
        libc_name = ctypes.util.find_library('c')
        if not libc_name:
            raise OSError("Could not find standard C library for inotify bindings")
        self.libc = ctypes.CDLL(libc_name, use_errno=True)

        # Bind inotify_init
        self.libc.inotify_init.argtypes = []
        self.libc.inotify_init.restype = ctypes.c_int

        # Bind inotify_add_watch
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int

        # Initialize inotify instance
        self.fd = self.libc.inotify_init()
        if self.fd < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"inotify_init failed: {os.strerror(errno)}")

        # Register watcher mask
        self.wd = self.libc.inotify_add_watch(self.fd, self.filepath.encode('utf-8'), self.mask)
        if self.wd < 0:
            os.close(self.fd)
            errno = ctypes.get_errno()
            raise OSError(errno, f"inotify_add_watch failed on {self.filepath}: {os.strerror(errno)}")

        # Attach file descriptor to the active asyncio loop
        self.loop = asyncio.get_event_loop()
        self.loop.add_reader(self.fd, self._handle_read)

    def _handle_read(self):
        """Callback executed reactively when inotify events are queued in the descriptor"""
        try:
            # Read all pending events (inotify_event struct size is 16 bytes + len of name)
            buf = os.read(self.fd, 4096)
            if not buf:
                return

            offset = 0
            while offset + 16 <= len(buf):
                wd, mask, cookie, name_len = struct.unpack('iIII', buf[offset:offset+16])
                # Advance past header and name padding
                offset += 16 + name_len
                
                # Execute consumer callback
                self.callback(mask)
        except Exception as e:
            print(f"InotifyWatcher read error: {e}", file=sys.stderr)

    def close(self):
        """Clean up filesystem watches and close file descriptor"""
        try:
            self.loop.remove_reader(self.fd)
            os.close(self.fd)
        except Exception:
            pass


class ServerSentEventsServer:
    """
    High-performance asynchronous Server-Sent Events (SSE) broadcasting server.
    Monitors a sequential log stream reactively and pushes events to concurrent subscribers.
    Supports starting index parameters (`start_idx`) for historical catch-up.
    """
    def __init__(self, filepath, host='127.0.0.1', port=8088):
        self.filepath = filepath
        self.host = host
        self.port = port
        self.subscribers = set()
        self.connection_tasks = set()
        self.server = None
        self.watcher = None
        self.last_read_offset = 0
        self.broadcast_event = None
        self.broadcast_task = None
        
        # Initialize the sequential log reader
        self.stream = SequentialLogStream(self.filepath)

    async def start(self):
        """Starts the async TCP server and registers the reactive inotify file watcher"""
        self.server = await asyncio.start_server(self._handle_client, self.host, self.port)
        
        # Discover current end of log to only stream newly appended events
        _, size = self.stream.get_physical_stats()
        self.last_read_offset = size

        # Start the sequential background coalesced broadcast task
        self.broadcast_event = asyncio.Event()
        self.broadcast_task = asyncio.create_task(self._broadcast_loop())

        # Setup reactive watcher using inotify IN_MODIFY mask
        self.watcher = InotifyWatcher(self.filepath, self._on_file_changed, mask=IN_MODIFY)
        print(f"SSE Server listening on http://{self.host}:{self.port}/stream watching {self.filepath}")

    def _on_file_changed(self, mask):
        """Inotify callback that executes reactively and schedules event broadcasts"""
        if self.broadcast_event:
            self.broadcast_event.set()

    async def _broadcast_loop(self):
        """Background loop that waits for file changes and broadcasts new records sequentially"""
        try:
            while True:
                await self.broadcast_event.wait()
                self.broadcast_event.clear()
                await self._broadcast_new_records()
        except asyncio.CancelledError:
            pass

    async def _broadcast_new_records(self):
        """Reads newly appended records and pushes them into connected subscriber queues"""
        if len(self.subscribers) == 0:
            # Catch up reading pointer even if no clients are connected
            _, size = self.stream.get_physical_stats()
            self.last_read_offset = size
            return

        # Seek & Read new records using succinct format starting from last_read_offset
        new_records = list(self.stream.read_records(start_offset=self.last_read_offset))
        for offset, payload, src, ts in new_records:
            # Format as SSE event: data: {"ts": YY, "src": ZZ, "data": {...}}\n\n
            sse_packet = {
                "ts": ts,
                "src": src,
                "data": payload
            }
            sse_data = f"data: {json.dumps(sse_packet)}\n\n"
            
            # Broadcast to all client buffers concurrently
            for queue in list(self.subscribers):
                await queue.put(sse_data)

            # Update pointer precisely using absolute next event boundary
            self.last_read_offset = self.stream.skip_next_event(offset + 1)

    async def _handle_client(self, reader, writer):
        """Handles a new connected HTTP client stream subscriber with race-free historical catch-up"""
        # 1. Capture the exact file size at the moment of connection for clean catch-up limit
        catchup_limit_offset = os.path.getsize(self.filepath)

        queue = asyncio.Queue()
        self.subscribers.add(queue)
        
        current_task = asyncio.current_task()
        self.connection_tasks.add(current_task)

        try:
            # Parse HTTP request header without over-consuming event stream bytes
            request_data = await reader.read(1024)
            request_str = request_data.decode('utf-8')
            if not request_str.startswith("GET /stream"):
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return

            # Extract start_idx from query parameters (GET /stream?start_idx=X HTTP/1.1)
            start_idx = None
            request_line = request_str.split("\r\n")[0]
            request_parts = request_line.split(" ")
            if len(request_parts) >= 2:
                uri = request_parts[1]
                if "?" in uri:
                    query = uri.split("?")[1]
                    for param in query.split("&"):
                        if "=" in param:
                            key, val = param.split("=")
                            if key == "start_idx":
                                try:
                                    start_idx = int(val)
                                except ValueError:
                                    pass

            # Write standard SSE headers
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\n"
                b"Access-Control-Allow-Origin: *\r\n\r\n"
            )
            await writer.drain()

            # 2. Perform client-specific race-free historical catch-up up to connection timestamp
            if start_idx is not None:
                if start_idx < 0:
                    current_offset = self.stream.locate_records_from_end(abs(start_idx))
                else:
                    current_offset = self.stream.skip_next_event(start_idx)
                while current_offset < catchup_limit_offset:
                    # Read single record
                    records = list(self.stream.read_records(start_offset=current_offset, limit=1))
                    if not records:
                        break
                    offset, payload, src, ts = records[0]
                    
                    # Stream directly to the socket
                    sse_packet = {
                        "ts": ts,
                        "src": src,
                        "data": payload
                    }
                    sse_data = f"data: {json.dumps(sse_packet)}\n\n"
                    writer.write(sse_data.encode('utf-8'))
                    await writer.drain()
                    
                    # Advance to next event boundary
                    current_offset = self.stream.skip_next_event(offset + 1)

            # 3. Enter main real-time queue broadcasting loop
            while True:
                sse_data = await queue.get()
                writer.write(sse_data.encode('utf-8'))
                await writer.drain()
                queue.task_done()
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self.subscribers.remove(queue)
            self.connection_tasks.discard(current_task)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def stop(self):
        """Cleanly stops the server, watcher, and storage descriptor"""
        if self.watcher:
            self.watcher.close()

        if self.broadcast_task:
            self.broadcast_task.cancel()
            try:
                await self.broadcast_task
            except asyncio.CancelledError:
                pass
            self.broadcast_task = None

        # Cancel all active client connection handler tasks to prevent event loop hanging
        for task in list(self.connection_tasks):
            task.cancel()

        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self.stream.close()


class TestSseInotifyIntegration(unittest.TestCase):
    def setUp(self):
        self.test_dir = "/tmp/sluicegate_sse_test"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)
        self.log_file = os.path.join(self.test_dir, "sse_stream.json")
        
        # Touch file to allow inotify monitoring immediately
        with open(self.log_file, 'w') as f:
            pass

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    async def _subscriber_client_task(self, client_id, results_list, port, expected_count, start_idx=None):
        """Simulates a concurrent stream client that consumes SSE push events"""
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            
            # Send standard HTTP stream request (optionally with start_idx)
            uri_path = "/stream" if start_idx is None else f"/stream?start_idx={start_idx}"
            request = (
                f"GET {uri_path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Accept: text/event-stream\r\n\r\n"
            )
            writer.write(request.encode('utf-8'))
            await writer.drain()

            # Read standard HTTP headers response using readuntil to prevent over-consuming stream
            await reader.readuntil(b"\r\n\r\n")

            # Consume event chunks defensively to ignore blank frames
            events_received = 0
            while events_received < expected_count:
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode('utf-8').strip()
                if line_str.startswith("data:"):
                    payload_json = line_str[5:].strip()
                    event = json.loads(payload_json)
                    results_list.append((client_id, event))
                    events_received += 1

            writer.close()
            await writer.wait_closed()
        except Exception as e:
            print(f"Client {client_id} encountered error: {e}")

    async def _run_basic_multi_subscriber_test(self):
        """Verify 5 concurrent SSE subscribers reactively receive all log appends in real time"""
        port = 8097
        server = ServerSentEventsServer(self.log_file, port=port)
        await server.start()

        num_subscribers = 5
        client_results = []

        try:
            # 1. Spawn concurrent subscriber tasks
            subscriber_tasks = []
            for i in range(num_subscribers):
                task = asyncio.create_task(self._subscriber_client_task(i, client_results, port, expected_count=5))
                subscriber_tasks.append(task)

            # 2. Wait defensively until ALL clients have successfully registered in the server
            while len(server.subscribers) < num_subscribers:
                await asyncio.sleep(0.05)

            # 3. Append 5 records dynamically to the storage log stream
            appended_payloads = []
            with SequentialLogStream(self.log_file) as stream:
                for i in range(5):
                    payload = {"index": i, "val": 100.0 + i}
                    appended_payloads.append(payload)
                    stream.append(payload=payload, source="tester")
                    # Trigger inotify scan immediately
                    await asyncio.sleep(0.1)

            # 4. Wait for all subscribers to finish consuming events
            await asyncio.gather(*subscriber_tasks, return_exceptions=True)

            # 5. Assertions on results
            self.assertEqual(len(client_results), num_subscribers * 5)

            # Group results by client
            grouped_results = {}
            for client_id, event in client_results:
                grouped_results.setdefault(client_id, []).append(event)

            # Verify that every single client received all 5 events in exact order
            for client_id in range(num_subscribers):
                events = grouped_results.get(client_id, [])
                self.assertEqual(len(events), 5)
                for idx, event in enumerate(events):
                    self.assertEqual(event["data"]["index"], idx)
                    self.assertEqual(event["src"], "tester")
                    self.assertTrue(event["ts"] > 0)
        finally:
            await server.stop()

    async def _run_historical_catchup_test(self):
        """Verify that clients can specify starting indexes for historical catch-up and transition to real-time stream"""
        port = 8098
        
        # 1. Write 3 historical events *prior* to starting server
        historical_offsets = []
        with SequentialLogStream(self.log_file) as stream:
            for i in range(3):
                offset = stream.append(payload={"index": i, "mode": "historical"}, source="pre-ingest")
                historical_offsets.append(offset)

        # 2. Start server
        server = ServerSentEventsServer(self.log_file, port=port)
        await server.start()

        client_results = []
        try:
            # Client A: Request start_idx=0 (Should receive 3 historical + 2 real-time = 5 events)
            task_a = asyncio.create_task(
                self._subscriber_client_task("ClientA", client_results, port, expected_count=5, start_idx=0)
            )

            # Client B: Request start_idx=offset of 2nd record (Should receive 2 historical + 2 real-time = 4 events)
            task_b = asyncio.create_task(
                self._subscriber_client_task("ClientB", client_results, port, expected_count=4, start_idx=historical_offsets[1])
            )

            # Client C: Request no start_idx (Should receive only 2 real-time events)
            task_c = asyncio.create_task(
                self._subscriber_client_task("ClientC", client_results, port, expected_count=2, start_idx=None)
            )

            # Wait for all clients to connect and register in the server
            while len(server.subscribers) < 3:
                await asyncio.sleep(0.05)

            # 3. Append 2 new real-time records
            with SequentialLogStream(self.log_file) as stream:
                for i in range(2):
                    stream.append(payload={"index": i, "mode": "realtime"}, source="live-ingest")
                    await asyncio.sleep(0.1)

            # 4. Wait for all clients to finish consuming
            await asyncio.gather(task_a, task_b, task_c, return_exceptions=True)

            # 5. Group and verify results
            grouped = {}
            for client_id, event in client_results:
                grouped.setdefault(client_id, []).append(event)

            # Verify Client A: 3 historical + 2 realtime
            a_events = grouped.get("ClientA", [])
            self.assertEqual(len(a_events), 5)
            self.assertEqual(a_events[0]["data"]["index"], 0)
            self.assertEqual(a_events[0]["data"]["mode"], "historical")
            self.assertEqual(a_events[4]["data"]["index"], 1)
            self.assertEqual(a_events[4]["data"]["mode"], "realtime")

            # Verify Client B: 2 historical + 2 realtime
            b_events = grouped.get("ClientB", [])
            self.assertEqual(len(b_events), 4)
            self.assertEqual(b_events[0]["data"]["index"], 1)
            self.assertEqual(b_events[0]["data"]["mode"], "historical")
            self.assertEqual(b_events[3]["data"]["index"], 1)
            self.assertEqual(b_events[3]["data"]["mode"], "realtime")

            # Verify Client C: 2 realtime only
            c_events = grouped.get("ClientC", [])
            self.assertEqual(len(c_events), 2)
            self.assertEqual(c_events[0]["data"]["index"], 0)
            self.assertEqual(c_events[0]["data"]["mode"], "realtime")
            self.assertEqual(c_events[1]["data"]["index"], 1)
            self.assertEqual(c_events[1]["data"]["mode"], "realtime")

        finally:
            await server.stop()

    async def _run_relative_backward_catchup_test(self):
        """Verify that clients can specify negative start_idx for relative backward seeks from EOF and transition to live"""
        port = 8099
        
        # 1. Write 5 historical events prior to starting server
        with SequentialLogStream(self.log_file) as stream:
            for i in range(5):
                stream.append(payload={"index": i, "mode": "historical"}, source="pre-ingest")

        # 2. Start server
        server = ServerSentEventsServer(self.log_file, port=port)
        await server.start()

        client_results = []
        try:
            # Client A: Request start_idx=-2 (Should receive last 2 historical [index 3 & 4] + 2 realtime = 4 events)
            task_a = asyncio.create_task(
                self._subscriber_client_task("ClientA", client_results, port, expected_count=4, start_idx=-2)
            )

            # Client B: Request start_idx=-10 (Should receive all 5 historical + 2 realtime = 7 events)
            task_b = asyncio.create_task(
                self._subscriber_client_task("ClientB", client_results, port, expected_count=7, start_idx=-10)
            )

            while len(server.subscribers) < 2:
                await asyncio.sleep(0.05)

            # 3. Append 2 new real-time records
            with SequentialLogStream(self.log_file) as stream:
                for i in range(2):
                    stream.append(payload={"index": i, "mode": "realtime"}, source="live-ingest")
                    await asyncio.sleep(0.1)

            # 4. Wait for all clients to finish consuming
            await asyncio.gather(task_a, task_b, return_exceptions=True)

            # 5. Group and verify results
            grouped = {}
            for client_id, event in client_results:
                grouped.setdefault(client_id, []).append(event)

            # Verify Client A: 2 historical + 2 realtime
            a_events = grouped.get("ClientA", [])
            self.assertEqual(len(a_events), 4)
            self.assertEqual(a_events[0]["data"]["index"], 3)
            self.assertEqual(a_events[0]["data"]["mode"], "historical")
            self.assertEqual(a_events[3]["data"]["index"], 1)
            self.assertEqual(a_events[3]["data"]["mode"], "realtime")

            # Verify Client B: 5 historical + 2 realtime
            b_events = grouped.get("ClientB", [])
            self.assertEqual(len(b_events), 7)
            self.assertEqual(b_events[0]["data"]["index"], 0)
            self.assertEqual(b_events[0]["data"]["mode"], "historical")
            self.assertEqual(b_events[6]["data"]["index"], 1)
            self.assertEqual(b_events[6]["data"]["mode"], "realtime")

        finally:
            await server.stop()

    def test_multiple_concurrent_subscribers(self):
        """Verify 5 concurrent SSE subscribers reactively receive all log appends in real time"""
        asyncio.run(self._run_basic_multi_subscriber_test())

    def test_historical_catchup_and_realtime_transition(self):
        """Verify clients requesting start_idx receive exact replayed payloads and transition cleanly to live streaming"""
        asyncio.run(self._run_historical_catchup_test())

    def test_relative_backward_catchup_and_realtime_transition(self):
        """Verify clients requesting negative start_idx receive relative seeked historical payloads and transition to live"""
        asyncio.run(self._run_relative_backward_catchup_test())


if __name__ == "__main__":
    unittest.main()
