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


class TestSluicegateApiServer(unittest.TestCase):
    def setUp(self):
        self.test_dir = "/tmp/sluicegate_api_test"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)

        self.streams_dir = os.path.join(self.test_dir, "streams")
        self.static_dir = os.path.join(self.test_dir, "static")
        os.makedirs(self.streams_dir)
        os.makedirs(self.static_dir)

        # Write dummy index.html in static dir
        self.dummy_index = "<html><body>Placeholder</body></html>"
        with open(os.path.join(self.static_dir, "index.html"), "w") as f:
            f.write(self.dummy_index)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    async def _client_request(self, method, path, body=None, port=8299, headers=None):
        """Simulate client sending HTTP request directly to ApiServer port"""
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        
        req = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        if headers:
            for k, v in headers.items():
                req += f"{k}: {v}\r\n"
        if body is not None:
            body_bytes = body.encode('utf-8') if isinstance(body, str) else body
            req += f"Content-Length: {len(body_bytes)}\r\n\r\n"
            writer.write(req.encode('utf-8') + body_bytes)
        else:
            req += "\r\n"
            writer.write(req.encode('utf-8'))
        await writer.drain()

        # Read status line
        status_line = await reader.readline()
        status_str = status_line.decode('utf-8').strip()

        # Read headers
        res_headers = {}
        while True:
            line = await reader.readline()
            if line == b'\r\n' or line == b'\n':
                break
            line_str = line.decode('utf-8').strip()
            if ":" in line_str:
                k, v = line_str.split(":", 1)
                res_headers[k.strip().lower()] = v.strip()

        # Read body if present
        content_len = int(res_headers.get("content-length", 0))
        body_data = b''
        if content_len > 0:
            body_data = await reader.readexactly(content_len)

        writer.close()
        await writer.wait_closed()
        return status_str, res_headers, body_data

    async def _subscriber_sse_task(self, topic, port, events_received_list, expected_count):
        """Streams events reactively from the SSE endpoint"""
        try:
            print("[SSE Client] Connecting...")
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            req = f"GET /stream?topic={topic} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: text/event-stream\r\n\r\n"
            writer.write(req.encode('utf-8'))
            await writer.drain()

            print("[SSE Client] Request sent. Waiting for headers...")
            # Skip HTTP header blocks
            await reader.readuntil(b"\r\n\r\n")
            print("[SSE Client] Headers received! Reading events...")

            count = 0
            while count < expected_count:
                line = await reader.readline()
                if not line:
                    print("[SSE Client] Connection closed by server.")
                    break
                line_str = line.decode('utf-8').strip()
                print(f"[SSE Client] Received line: {line_str}")
                if line_str.startswith("data:"):
                    payload_json = line_str[5:].strip()
                    event = json.loads(payload_json)
                    events_received_list.append(event)
                    count += 1
                    print(f"[SSE Client] Extracted event: {event}. Total: {count}/{expected_count}")
            writer.close()
            await writer.wait_closed()
        except Exception as e:
            print(f"[SSE Client FAIL] Error: {e}")

    async def _run_e2e_test(self):
        port = 8299
        print("[Test] Starting ApiServer...")
        server = SluicegateApiServer(self.streams_dir, self.static_dir, port=port)
        await server.start()

        try:
            print("[Test] 1. Requesting topics...")
            status, _, body = await self._client_request("GET", "/api/topics", port=port)
            self.assertEqual(status, "HTTP/1.1 200 OK")
            res_json = json.loads(body.decode('utf-8'))
            self.assertEqual(len(res_json["topics"]), 0)

            print("[Test] 2. Injecting initial event...")
            payload = {"temp": 24.5, "unit": "C"}
            status, _, body = await self._client_request(
                "POST", "/api/inject?topic=sensor_a", 
                body=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                port=port
            )
            self.assertEqual(status, "HTTP/1.1 200 OK")
            inject_res = json.loads(body.decode('utf-8'))
            self.assertEqual(inject_res["status"], "success")

            print("[Test] 3. Requesting topics again...")
            status, _, body = await self._client_request("GET", "/api/topics", port=port)
            self.assertEqual(status, "HTTP/1.1 200 OK")
            res_json = json.loads(body.decode('utf-8'))
            self.assertEqual(len(res_json["topics"]), 1)

            print("[Test] 4. Getting events historical...")
            status, _, body = await self._client_request("GET", "/api/events?topic=sensor_a&limit=1", port=port)
            self.assertEqual(status, "HTTP/1.1 200 OK")
            events_res = json.loads(body.decode('utf-8'))
            self.assertEqual(len(events_res["events"]), 1)

            print("[Test] 5. Subscribing to SSE...")
            sse_events = []
            sse_task = asyncio.create_task(self._subscriber_sse_task("sensor_a", port, sse_events, expected_count=2))
            
            print("[Test] Waiting for subscriber task to start...")
            await asyncio.sleep(0.5)

            print("[Test] Injecting live event 1...")
            await self._client_request(
                "POST", f"/api/inject?topic=sensor_a", 
                body=json.dumps({"seq": 0, "val": 100}),
                headers={"Content-Type": "application/json"},
                port=port
            )
            print("[Test] Injected live event 1.")
            await asyncio.sleep(0.2)

            print("[Test] Injecting live event 2...")
            await self._client_request(
                "POST", f"/api/inject?topic=sensor_a", 
                body=json.dumps({"seq": 1, "val": 101}),
                headers={"Content-Type": "application/json"},
                port=port
            )
            print("[Test] Injected live event 2.")
            await asyncio.sleep(0.2)

            print("[Test] Waiting for sse_task to consume 2 events...")
            await asyncio.wait_for(sse_task, timeout=3.0)
            print("[Test] SSE task finished successfully!")
            
            self.assertEqual(len(sse_events), 2)

            print("[Test] 6. Getting static root...")
            status, _, body = await self._client_request("GET", "/", port=port)
            self.assertEqual(status, "HTTP/1.1 200 OK")
            self.assertEqual(body.decode('utf-8'), self.dummy_index)

        except Exception as e:
            print(f"[Test FAIL] Exception: {e}")
            raise
        finally:
            print("[Test] Stopping server...")
            await server.stop()

    def test_api_server_e2e(self):
        """Execute full end-to-end integration flows asynchronously"""
        asyncio.run(self._run_e2e_test())


if __name__ == "__main__":
    unittest.main()
