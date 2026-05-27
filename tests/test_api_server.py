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
        os.environ["SLUICEGATE_NO_AUTH"] = "1"
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
        os.environ.pop("SLUICEGATE_NO_AUTH", None)
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

    async def _run_auth_test(self):
        # Explicitly turn on authentication for this test
        os.environ.pop("SLUICEGATE_NO_AUTH", None)
        port = 8499
        server = SluicegateApiServer(self.streams_dir, self.static_dir, port=port)
        await server.start()
        
        try:
            # 1. Unauthenticated request to /api/topics should return 401 Unauthorized
            status, headers, body = await self._client_request("GET", "/api/topics", port=port)
            self.assertEqual(status, "HTTP/1.1 401 Unauthorized")
            self.assertIn("www-authenticate", headers)
            self.assertEqual(headers["www-authenticate"], 'Basic realm="Sluicegate Admin Portal"')
            
            # 2. Authenticated request with wrong password should return 401 Unauthorized
            import base64
            wrong_auth = base64.b64encode(b"admin:wrongpassword").decode('utf-8')
            status, headers, body = await self._client_request(
                "GET", "/api/topics", port=port, 
                headers={"Authorization": f"Basic {wrong_auth}"}
            )
            self.assertEqual(status, "HTTP/1.1 401 Unauthorized")
            
            # 3. Authenticated request with correct credentials should return 200 OK
            correct_auth = base64.b64encode(b"admin:sluicegate").decode('utf-8')
            status, headers, body = await self._client_request(
                "GET", "/api/topics", port=port, 
                headers={"Authorization": f"Basic {correct_auth}"}
            )
            self.assertEqual(status, "HTTP/1.1 200 OK")
            
            # 4. Authenticated request to GET /api/system/apikey should return the key
            status, headers, body = await self._client_request(
                "GET", "/api/system/apikey", port=port,
                headers={"Authorization": f"Basic {correct_auth}"}
            )
            self.assertEqual(status, "HTTP/1.1 200 OK")
            res_data = json.loads(body.decode('utf-8'))
            self.assertEqual(res_data["status"], "success")
            original_key = res_data["api_key"]
            self.assertTrue(len(original_key) >= 8)

            # 5. Authenticated request to POST /api/system/apikey to update the key
            new_custom_key = "sg_ingest_custom_telemetry_key_123"
            status, headers, body = await self._client_request(
                "POST", "/api/system/apikey", port=port,
                body=json.dumps({"api_key": new_custom_key}),
                headers={
                    "Authorization": f"Basic {correct_auth}",
                    "Content-Type": "application/json"
                }
            )
            self.assertEqual(status, "HTTP/1.1 200 OK")
            res_data = json.loads(body.decode('utf-8'))
            self.assertEqual(res_data["api_key"], new_custom_key)
            self.assertEqual(server.api_key, new_custom_key)

            # 6. Verify that the C-daemon / .api_key file was written correctly
            with open(server.api_key_path, "r") as f:
                self.assertEqual(f.read().strip(), new_custom_key)

            # 7. Unauthenticated request with correct Ingestion API Key header should bypass basic auth for data inject!
            status, headers, body = await self._client_request(
                "POST", "/api/inject?topic=sensor_auth", port=port,
                body=json.dumps({"telemetry": "test"}),
                headers={
                    "Content-Type": "application/json",
                    "X-Sluicegate-API-Key": new_custom_key
                }
            )
            self.assertEqual(status, "HTTP/1.1 200 OK")

            # 8. Authenticated request to GET /api/system/readkey should return the read key
            status, headers, body = await self._client_request(
                "GET", "/api/system/readkey", port=port,
                headers={"Authorization": f"Basic {correct_auth}"}
            )
            self.assertEqual(status, "HTTP/1.1 200 OK")
            res_data = json.loads(body.decode('utf-8'))
            self.assertEqual(res_data["status"], "success")
            original_read_key = res_data["api_key"]
            self.assertTrue(original_read_key.startswith("sg_read_"))

            # 9. Authenticated request to POST /api/system/readkey to update the read key
            new_custom_read_key = "sg_read_custom_client_key_123"
            status, headers, body = await self._client_request(
                "POST", "/api/system/readkey", port=port,
                body=json.dumps({"api_key": new_custom_read_key}),
                headers={
                    "Authorization": f"Basic {correct_auth}",
                    "Content-Type": "application/json"
                }
            )
            self.assertEqual(status, "HTTP/1.1 200 OK")
            res_data = json.loads(body.decode('utf-8'))
            self.assertEqual(res_data["api_key"], new_custom_read_key)
            self.assertEqual(server.read_key, new_custom_read_key)

            # 10. Unauthenticated request with correct Read API Key header should bypass basic auth for data read!
            status, headers, body = await self._client_request(
                "GET", "/api/events?topic=sensor_auth&start_idx=0", port=port,
                headers={
                    "X-Sluicegate-Read-Key": new_custom_read_key
                }
            )
            self.assertEqual(status, "HTTP/1.1 200 OK")
            
        finally:
            await server.stop()
            os.environ["SLUICEGATE_NO_AUTH"] = "1"



    def test_basic_authentication(self):
        """Verify the server enforces HTTP Basic Authentication correctly when active"""
        asyncio.run(self._run_auth_test())

    async def _run_delete_topic_test(self):
        port = 8599
        server = SluicegateApiServer(self.streams_dir, self.static_dir, port=port)
        await server.start()
        
        try:
            # 1. Inject an event to create a topic
            status, _, body = await self._client_request(
                "POST", "/api/inject?topic=sensor_delete", 
                body=json.dumps({"temp": 24.5}),
                headers={"Content-Type": "application/json"},
                port=port
            )
            self.assertEqual(status, "HTTP/1.1 200 OK")
            
            # Assert file exists
            stream_file = os.path.join(self.streams_dir, "sensor_delete.json")
            self.assertTrue(os.path.exists(stream_file))
            
            # 2. Deleting without topic param should return 400 Bad Request
            status, _, body = await self._client_request(
                "DELETE", "/api/topics", port=port
            )
            self.assertEqual(status, "HTTP/1.1 400 Bad Request")
            
            # 3. Delete the topic
            status, _, body = await self._client_request(
                "DELETE", "/api/topics?topic=sensor_delete", port=port
            )
            self.assertEqual(status, "HTTP/1.1 200 OK")
            res_json = json.loads(body.decode('utf-8'))
            self.assertEqual(res_json["status"], "success")
            
            # Assert file is deleted
            self.assertFalse(os.path.exists(stream_file))
            
            # 4. Verify topics list is now empty
            status, _, body = await self._client_request("GET", "/api/topics", port=port)
            self.assertEqual(status, "HTTP/1.1 200 OK")
            res_json = json.loads(body.decode('utf-8'))
            self.assertEqual(len(res_json["topics"]), 0)
            
        finally:
            await server.stop()

    def test_delete_topic(self):
        """Verify that the DELETE /api/topics endpoint successfully deletes a topic and removes its file from disk"""
        asyncio.run(self._run_delete_topic_test())

    async def _run_url_encoded_topic_test(self):
        port = 18889
        server = SluicegateApiServer(self.streams_dir, self.static_dir, port=port)
        await server.start()
        try:
            # 1. Inject to a URL-encoded topic: fcm%2Ffeedback -> fcm/feedback
            status, _, body = await self._client_request(
                "POST", "/api/inject?topic=fcm%2Ffeedback",
                body=b'{"payload": "test"}', port=port
            )
            self.assertEqual(status, "HTTP/1.1 200 OK")
            
            # 2. Check if the file is created at nested path fcm/feedback.json
            stream_file = os.path.join(self.streams_dir, "fcm", "feedback.json")
            self.assertTrue(os.path.exists(stream_file))
            
        finally:
            await server.stop()

    def test_url_encoded_topic(self):
        """Verify Sluicegate server URL-decodes query parameters (e.g. topic name %2F) correctly"""
        asyncio.run(self._run_url_encoded_topic_test())


if __name__ == "__main__":
    unittest.main()

