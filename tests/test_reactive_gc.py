#!/usr/bin/env python3
import os
import sys
import time
import json
import shutil
import asyncio
import unittest

# Append workspace src directory to import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))
from server import SluicegateApiServer
from storage import SequentialLogStream


class TestReactiveGarbageCollection(unittest.TestCase):
    def setUp(self):
        self.test_dir = "/tmp/sluicegate_gc_test"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)

        self.streams_dir = os.path.join(self.test_dir, "streams")
        self.static_dir = os.path.join(self.test_dir, "static")
        os.makedirs(self.streams_dir)
        os.makedirs(self.static_dir)

        # touch a blank index.html
        with open(os.path.join(self.static_dir, "index.html"), "w") as f:
            f.write("<html></html>")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    async def _post_config(self, topic, max_blocks, max_age_min, port=8399):
        """Helper to POST config changes to the REST endpoint"""
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        payload = json.dumps({"max_blocks": max_blocks, "max_age_min": max_age_min})
        payload_bytes = payload.encode('utf-8')

        req = (
            f"POST /api/config?topic={topic} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload_bytes)}\r\n\r\n"
        )
        writer.write(req.encode('utf-8') + payload_bytes)
        await writer.drain()

        # Read response headers
        status_line = await reader.readline()
        status_str = status_line.decode('utf-8').strip()

        # Skip remaining headers and read body
        res_headers = {}
        while True:
            line = await reader.readline()
            if line == b'\r\n' or line == b'\n':
                break
            line_str = line.decode('utf-8').strip()
            if ":" in line_str:
                k, v = line_str.split(":", 1)
                res_headers[k.strip().lower()] = v.strip()

        content_len = int(res_headers.get("content-length", 0))
        body_data = b''
        if content_len > 0:
            body_data = await reader.readexactly(content_len)

        writer.close()
        await writer.wait_closed()
        return status_str, json.loads(body_data.decode('utf-8'))

    async def _run_gc_test(self):
        port = 8399
        topic = "sensor_gc"
        
        # 1. Pre-populate topic stream with plenty of large events to allocate physical sectors
        topic_path = os.path.join(self.streams_dir, f"{topic}.json")
        
        # Write large events so we cross physical sector boundaries
        large_payload = {"telemetry": "X" * 380} # ~450 bytes each serialized
        
        # Setup initial configuration overrides: high max blocks limit
        init_overrides = {
            "user.SGC.MAX_BLOCKS": "1000",       # allow ~500KB
            "user.SGC.SAFETY_HEADROOM": "0"
        }
        
        print("[GC Test] Pre-populating stream with large events...")
        with SequentialLogStream(topic_path, init_overrides) as stream:
            for i in range(40):
                stream.append(payload=large_payload, source="producer_a")

            blocks_before, size_before = stream.get_physical_stats()
            print(f"[GC Test] Initial Stats - Blocks: {blocks_before}, File Size: {size_before} bytes")
            self.assertTrue(blocks_before > 0)

        # 2. Boot up ApiServer
        print("[GC Test] Starting ApiServer...")
        server = SluicegateApiServer(self.streams_dir, self.static_dir, port=port)
        await server.start()

        try:
            # 3. Request a config update to tighten the max blocks limit reactively down to 2 sectors!
            print("[GC Test] POSTing tightened max_blocks limit = 2...")
            status, res = await self._post_config(topic, max_blocks=2, max_age_min=1440, port=port)
            self.assertEqual(status, "HTTP/1.1 200 OK")
            self.assertEqual(res["max_blocks"], 2)

            # 4. Wait for the reactive inotify IN_ATTRIB metadata update event and immediate GC to run
            print("[GC Test] Waiting for reactive GC execution...")
            await asyncio.sleep(1.0)

            # 5. Check physical blocks of the file - it should have decreased!
            with SequentialLogStream(topic_path) as stream:
                blocks_after, size_after = stream.get_physical_stats()
                print(f"[GC Test] Reclaimed Stats - Blocks: {blocks_after}, File Size: {size_after} bytes")
                
                # Verify physical blocks decreased reactively
                self.assertTrue(blocks_after < blocks_before)
                # Verify keep size flag is respected (logical file size matches, sparse holes punched)
                self.assertEqual(size_after, size_before)

                # 6. Verify that surviving records can still be read successfully
                records = list(stream.read_records())
                self.assertTrue(len(records) > 0)
                self.assertTrue(len(records) < 40) # leading records pruned
                print(f"[GC Test] Verified {len(records)} surviving records read cleanly.")

        finally:
            await server.stop()

    def test_reactive_metadata_gc(self):
        """Verify configuration edits reactively trigger immediate disk sector head deallocations"""
        asyncio.run(self._run_gc_test())


if __name__ == "__main__":
    unittest.main()
