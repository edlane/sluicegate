#!/usr/bin/env python3
import os
import sys
import time
import json
import struct
import ctypes
import ctypes.util
import asyncio
import mimetypes
import base64
import secrets


# Append src directory to import path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from storage import SequentialLogStream

# Inotify Constants
IN_MODIFY = 0x00000002
IN_CREATE = 0x00000100
IN_ATTRIB = 0x00000004


class DirectoryInotifyWatcher:
    """
    High-performance zero-dependency directory inotify watcher.
    Monitors a directory for file creations and modifications, reporting
    the affected file name back to the reactive callback.
    """
    def __init__(self, dirpath, callback, mask=IN_MODIFY | IN_CREATE | IN_ATTRIB):
        self.dirpath = os.path.abspath(dirpath)
        self.callback = callback
        self.mask = mask

        # Load C library
        libc_name = ctypes.util.find_library('c')
        if not libc_name:
            raise OSError("Could not find standard C library for inotify bindings")
        self.libc = ctypes.CDLL(libc_name, use_errno=True)

        self.libc.inotify_init.argtypes = []
        self.libc.inotify_init.restype = ctypes.c_int

        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int

        self.fd = self.libc.inotify_init()
        if self.fd < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"inotify_init failed: {os.strerror(errno)}")

        self.wd = self.libc.inotify_add_watch(self.fd, self.dirpath.encode('utf-8'), self.mask)
        if self.wd < 0:
            os.close(self.fd)
            errno = ctypes.get_errno()
            raise OSError(errno, f"inotify_add_watch failed on {self.dirpath}: {os.strerror(errno)}")

        self.loop = asyncio.get_event_loop()
        self.loop.add_reader(self.fd, self._handle_read)

    def _handle_read(self):
        try:
            buf = os.read(self.fd, 8192)
            if not buf:
                return

            offset = 0
            while offset + 16 <= len(buf):
                wd, mask, cookie, name_len = struct.unpack('iIII', buf[offset:offset+16])
                filename = ""
                if name_len > 0:
                    filename = buf[offset+16 : offset+16+name_len].split(b'\x00')[0].decode('utf-8')
                
                offset += 16 + name_len
                
                if filename:
                    self.callback(mask, filename)
        except Exception as e:
            print(f"DirectoryInotifyWatcher error: {e}", file=sys.stderr)

    def close(self):
        try:
            self.loop.remove_reader(self.fd)
            os.close(self.fd)
        except Exception:
            pass


class SluicegateApiServer:
    """
    Unified asynchronous HTTP server that handles REST API endpoints,
    SSE multi-topic streaming, and static assets serving for the React admin portal.
    """
    def __init__(self, streams_dir, static_dir, host='0.0.0.0', port=8088):
        self.streams_dir = os.path.abspath(streams_dir)
        self.static_dir = os.path.abspath(static_dir)
        self.host = host
        self.port = port
        self.server = None
        self.watcher = None

        # Authentication Configuration
        self.auth_username = os.environ.get("SLUICEGATE_USER", "admin")
        self.auth_password = os.environ.get("SLUICEGATE_PASSWORD", "sluicegate")
        self.require_auth = os.environ.get("SLUICEGATE_NO_AUTH", "0") != "1"


        # Map: topic_name -> set(asyncio.Queue)
        self.topic_subscribers = {}
        
        # Map: topic_name -> float/int last offset
        self.topic_offsets = {}

        # Map: topic_name -> SequentialLogStream
        self.active_streams = {}

        # Tracking set for active connection handler tasks
        self.connection_tasks = set()

        # Asyncio synchronization event per topic
        self.topic_events = {}
        self.topic_broadcast_tasks = {}

        # Ensure directory structures exist
        os.makedirs(self.streams_dir, exist_ok=True)
        if not os.path.exists(self.static_dir):
            os.makedirs(self.static_dir, exist_ok=True)

        # Initialize Pre-Shared Ingestion API Key (Part B, Method 3)
        self.api_key_path = os.path.join(self.streams_dir, ".api_key")
        self.api_key = self._load_or_create_api_key()

        # Initialize Ingestion Read API Key
        self.read_key_path = os.path.join(self.streams_dir, ".read_key")
        self.read_key = self._load_or_create_read_key()

    def _load_or_create_api_key(self):
        if os.path.exists(self.api_key_path):
            try:
                with open(self.api_key_path, "r") as f:
                    key = f.read().strip()
                    if key and key.startswith("sg_ingest_"):
                        return key
            except Exception as e:
                print(f"Error reading .api_key file: {e}", file=sys.stderr)
        
        key = os.environ.get("SLUICEGATE_API_KEY")
        if not key or not key.startswith("sg_ingest_"):
            key = "sg_ingest_" + secrets.token_hex(16)
        
        try:
            with open(self.api_key_path, "w") as f:
                f.write(key)
        except Exception as e:
            print(f"Error writing .api_key file: {e}", file=sys.stderr)
        
        return key

    def _load_or_create_read_key(self):
        if os.path.exists(self.read_key_path):
            try:
                with open(self.read_key_path, "r") as f:
                    key = f.read().strip()
                    if key and key.startswith("sg_read_"):
                        return key
            except Exception as e:
                print(f"Error reading .read_key file: {e}", file=sys.stderr)
        
        key = os.environ.get("SLUICEGATE_READ_KEY")
        if not key or not key.startswith("sg_read_"):
            key = "sg_read_" + secrets.token_hex(16)
        
        try:
            with open(self.read_key_path, "w") as f:
                f.write(key)
        except Exception as e:
            print(f"Error writing .read_key file: {e}", file=sys.stderr)
        
        return key


    def _get_stream_path(self, topic):

        # Prevent path traversal attacks
        safe_topic = os.path.basename(topic).replace(".json", "")
        return os.path.join(self.streams_dir, f"{safe_topic}.json")

    def _init_topic(self, topic):
        if topic not in self.topic_events:
            path = self._get_stream_path(topic)
            if not os.path.exists(path):
                with open(path, 'w') as f:
                    pass
            if topic not in self.active_streams:
                self.active_streams[topic] = SequentialLogStream(path)
            
            stream = self.active_streams[topic]
            _, size = stream.get_physical_stats()
            self.topic_offsets[topic] = size
            self.topic_events[topic] = asyncio.Event()
            self.topic_broadcast_tasks[topic] = asyncio.create_task(self._broadcast_loop(topic))

    def _get_stream(self, topic):
        if topic not in self.active_streams:
            self._init_topic(topic)
        return self.active_streams[topic]

    async def start(self):
        # Start TCP HTTP server
        self.server = await asyncio.start_server(self._handle_request, self.host, self.port)
        
        # Initialize directory watcher for reactive event streaming
        self.watcher = DirectoryInotifyWatcher(self.streams_dir, self._on_file_changed)
        
        # Populate initial topics and offsets
        self._rescan_topics()

        print(f"[+] Sluicegate REST + SSE API Server online at http://{self.host}:{self.port}")
        print(f"    - Monitoring streams under: {self.streams_dir}")
        print(f"    - Serving web assets from:  {self.static_dir}")

    def _rescan_topics(self):
        for f in os.listdir(self.streams_dir):
            if f.endswith('.json'):
                topic = f[:-5]
                # Initialize offset tracker and tasks
                stream = self._get_stream(topic)
                _, size = stream.get_physical_stats()
                if topic not in self.topic_offsets:
                    self.topic_offsets[topic] = size
                if topic not in self.topic_events:
                    self.topic_events[topic] = asyncio.Event()
                    self.topic_broadcast_tasks[topic] = asyncio.create_task(self._broadcast_loop(topic))

    def _on_file_changed(self, mask, filename):
        if filename.endswith('.json'):
            topic = filename[:-5]
            # Ensure topic initialized
            self._rescan_topics()
            
            # If metadata/extended attributes changed, reload configs and evaluate retention reactively
            if mask & IN_ATTRIB:
                try:
                    stream = self._get_stream(topic)
                    stream.reload_attributes()
                    stream.evaluate_retention()
                except Exception as e:
                    print(f"Error in reactive GC enforcement for {topic}: {e}", file=sys.stderr)

            if topic in self.topic_events:
                self.topic_events[topic].set()

    async def _broadcast_loop(self, topic):
        try:
            while True:
                await self.topic_events[topic].wait()
                self.topic_events[topic].clear()
                await self._broadcast_new_records(topic)
        except asyncio.CancelledError:
            pass

    async def _broadcast_new_records(self, topic):
        subscribers = self.topic_subscribers.get(topic, set())
        if not subscribers:
            # Advance offset pointer even if no subscribers, to avoid backlog buildup
            stream = self._get_stream(topic)
            _, size = stream.get_physical_stats()
            self.topic_offsets[topic] = size
            return

        stream = self._get_stream(topic)
        last_offset = self.topic_offsets.get(topic, 0)
        
        # Seek and broadcast all newly appended records in a single coalesced pass
        new_records = list(stream.read_records(start_offset=last_offset))
        for offset, payload, src, ts in new_records:
            sse_packet = {
                "ts": ts,
                "src": src,
                "data": payload
            }
            sse_data = f"data: {json.dumps(sse_packet)}\n\n"
            
            # Broadcast concurrently to all queues
            for queue in list(subscribers):
                await queue.put(sse_data)
            
            last_offset = stream.skip_next_event(offset + 1)
        
        self.topic_offsets[topic] = last_offset

    async def _handle_request(self, reader, writer):
        current_task = asyncio.current_task()
        self.connection_tasks.add(current_task)
        try:
            try:
                # Read first line: e.g. GET /api/topics HTTP/1.1
                request_line = await reader.readline()
                if not request_line:
                    writer.close()
                    return

                req_str = request_line.decode('utf-8').strip()
                parts = req_str.split(" ")
                if len(parts) < 2:
                    writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return

                method, uri = parts[0], parts[1]

                # Read headers
                headers = {}
                while True:
                    line = await reader.readline()
                    if line == b'\r\n' or line == b'\n':
                        break
                    line_str = line.decode('utf-8').strip()
                    if ":" in line_str:
                        k, v = line_str.split(":", 1)
                        headers[k.strip().lower()] = v.strip()

                # Handle CORS preflight
                if method == "OPTIONS":
                    await self._send_cors_response(writer)
                    return

                # Route requests
                path = uri.split("?")[0]
                query_params = {}
                if "?" in uri:
                    query = uri.split("?")[1]
                    for p in query.split("&"):
                        if "=" in p:
                            k, v = p.split("=", 1)
                            query_params[k] = v

                is_api = path.startswith("/api/") or path == "/stream"
                print(f"[Request] {method} {path} (is_api={is_api})", file=sys.stderr)
                # Authenticate request
                if self.require_auth and is_api:
                    auth_header = headers.get("authorization")
                    client_api_key = headers.get("x-sluicegate-api-key")
                    client_read_key = headers.get("x-sluicegate-read-key")
                    query_api_key = query_params.get("api_key")
                    query_read_key = query_params.get("read_key")
                    
                    authenticated = False
                    print(f"[Auth Debug] path={path} auth_header={repr(auth_header)} api_key={repr(client_api_key or query_api_key)} read_key={repr(client_read_key or query_read_key)}", file=sys.stderr)
                    
                    if auth_header and auth_header.startswith("Basic "):
                        try:
                            encoded = auth_header.split(" ", 1)[1]
                            decoded = base64.b64decode(encoded).decode('utf-8')
                            username, password = decoded.split(":", 1)
                            if username == self.auth_username and password == self.auth_password:
                                authenticated = True
                            else:
                                print(f"[Auth Failure] Credentials mismatch. Received '{username}' / '{password}'. Expected '{self.auth_username}' / '{self.auth_password}'", file=sys.stderr)
                        except Exception as e:
                            print(f"[Auth Error] Exception parsing Basic Auth header: {e}", file=sys.stderr)
                    
                    if not authenticated:
                        if path == "/api/inject":
                            key_to_check = client_api_key or query_api_key
                            if key_to_check and key_to_check == self.api_key:
                                authenticated = True
                        elif path in ("/stream", "/api/events"):
                            key_to_check = client_read_key or query_read_key or client_api_key or query_api_key
                            if key_to_check and key_to_check in (self.read_key, self.api_key):
                                authenticated = True
                            
                    if not authenticated:
                        user_agent = headers.get("user-agent", "").lower()
                        is_browser = any(b in user_agent for b in ("mozilla", "chrome", "safari", "webkit", "edge", "opera"))
                        await self._send_unauthorized(writer, suppress_prompt=is_browser)
                        return


                if path == "/api/topics":
                    await self._handle_get_topics(writer)
                elif path == "/api/events":
                    await self._handle_get_events(query_params, writer)
                elif path == "/api/inject":
                    content_len = int(headers.get("content-length", 0))
                    body = await reader.readexactly(content_len) if content_len > 0 else b''
                    await self._handle_post_inject(query_params, body, writer)
                elif path == "/api/config":
                    content_len = int(headers.get("content-length", 0))
                    body = await reader.readexactly(content_len) if content_len > 0 else b''
                    await self._handle_post_config(query_params, body, writer)
                elif path == "/api/system/apikey":
                    if method == "GET":
                        await self._handle_get_system_apikey(writer)
                    elif method == "POST":
                        content_len = int(headers.get("content-length", 0))
                        body = await reader.readexactly(content_len) if content_len > 0 else b''
                        await self._handle_post_system_apikey(body, writer)
                elif path == "/api/system/readkey":
                    if method == "GET":
                        await self._handle_get_system_readkey(writer)
                    elif method == "POST":
                        content_len = int(headers.get("content-length", 0))
                        body = await reader.readexactly(content_len) if content_len > 0 else b''
                        await self._handle_post_system_readkey(body, writer)
                elif path == "/stream":


                    await self._handle_sse_stream(query_params, writer, reader)
                else:
                    # Fallback to serving static frontend asset files
                    await self._handle_static_files(path, writer)

            except Exception as e:
                print(f"Error handling request: {e}", file=sys.stderr)
                try:
                    writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    writer.close()
                except Exception:
                    pass
        finally:
            self.connection_tasks.discard(current_task)

    async def _send_unauthorized(self, writer, suppress_prompt=False):
        scheme = b"x-Basic" if suppress_prompt else b"Basic"
        response = (
            b"HTTP/1.1 401 Unauthorized\r\n"
            b"WWW-Authenticate: " + scheme + b" realm=\"Sluicegate Admin Portal\"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 29\r\n"
            b"Access-Control-Allow-Origin: *\r\n"
            b"Connection: close\r\n\r\n"
            b"{\"error\":\"Unauthorized Access\"}"
        )
        writer.write(response)
        await writer.drain()
        writer.close()

    async def _send_cors_response(self, writer):
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Access-Control-Allow-Origin: *\r\n"
            b"Access-Control-Allow-Methods: GET, POST, OPTIONS, PUT, DELETE\r\n"
            b"Access-Control-Allow-Headers: Content-Type, Authorization\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )
        writer.write(response)
        await writer.drain()
        writer.close()


    async def _handle_get_topics(self, writer):
        # Rescan to detect new topic files
        self._rescan_topics()
        
        topics_info = []
        for f in os.listdir(self.streams_dir):
            if f.endswith('.json'):
                topic = f[:-5]
                stream = self._get_stream(topic)
                blocks, size = stream.get_physical_stats()
                topics_info.append({
                    "name": topic,
                    "size_bytes": size,
                    "allocated_blocks": blocks,
                    "max_blocks": stream.max_blocks,
                    "max_age_min": stream.max_age_minutes
                })

        body = json.dumps({"topics": topics_info}).encode('utf-8')
        response = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n\r\n"
        ).encode('utf-8') + body

        writer.write(response)
        await writer.drain()
        writer.close()

    async def _handle_get_events(self, query_params, writer):
        topic = query_params.get("topic")
        if not topic:
            body = b'{"error":"Missing topic parameter"}'
            self._send_json_error(400, body, writer)
            return

        start_idx = int(query_params.get("start_idx", 0))
        limit = int(query_params.get("limit", 100))

        try:
            stream = self._get_stream(topic)
        except Exception as e:
            body = json.dumps({"error": f"Failed to open topic: {e}"}).encode('utf-8')
            self._send_json_error(404, body, writer)
            return

        # Calculate exact start offset
        if start_idx < 0:
            current_offset = stream.locate_records_from_end(abs(start_idx))
        else:
            current_offset = stream.skip_next_event(start_idx)

        events = []
        records = list(stream.read_records(start_offset=current_offset, limit=limit))
        for offset, payload, src, ts in records:
            events.append({
                "offset": offset,
                "ts": ts,
                "src": src,
                "data": payload
            })

        body = json.dumps({"events": events}).encode('utf-8')
        response = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n\r\n"
        ).encode('utf-8') + body

        writer.write(response)
        await writer.drain()
        writer.close()

    async def _handle_post_inject(self, query_params, body_bytes, writer):
        topic = query_params.get("topic")
        if not topic:
            body = b'{"error":"Missing topic parameter"}'
            self._send_json_error(400, body, writer)
            return

        try:
            payload = json.loads(body_bytes.decode('utf-8'))
        except Exception:
            body = b'{"error":"Invalid JSON payload"}'
            self._send_json_error(400, body, writer)
            return

        try:
            stream = self._get_stream(topic)
            offset = stream.append(payload=payload, source="admin-portal")
            
            # Immediately trigger in-memory broadcast sweep
            if topic in self.topic_events:
                self.topic_events[topic].set()

            res_body = json.dumps({"status": "success", "offset": offset}).encode('utf-8')
            response = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(res_body)}\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"Connection: close\r\n\r\n"
            ).encode('utf-8') + res_body
            writer.write(response)
            await writer.drain()
            writer.close()
        except Exception as e:
            res_body = json.dumps({"error": f"Injection failed: {e}"}).encode('utf-8')
            self._send_json_error(500, res_body, writer)

    async def _handle_post_config(self, query_params, body_bytes, writer):
        topic = query_params.get("topic")
        if not topic:
            body = b'{"error":"Missing topic parameter"}'
            self._send_json_error(400, body, writer)
            return

        try:
            config = json.loads(body_bytes.decode('utf-8'))
        except Exception:
            body = b'{"error":"Invalid JSON configuration"}'
            self._send_json_error(400, body, writer)
            return

        max_blocks = config.get("max_blocks")
        max_age_min = config.get("max_age_min")

        try:
            stream = self._get_stream(topic)
            stream.update_attributes(max_blocks=max_blocks, max_age_min=max_age_min)
            
            res_body = json.dumps({
                "status": "success",
                "max_blocks": stream.max_blocks,
                "max_age_min": stream.max_age_minutes
            }).encode('utf-8')

            response = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(res_body)}\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"Connection: close\r\n\r\n"
            ).encode('utf-8') + res_body
            writer.write(response)
            await writer.drain()
            writer.close()
        except Exception as e:
            res_body = json.dumps({"error": f"Configuration update failed: {e}"}).encode('utf-8')
            self._send_json_error(500, res_body, writer)

    async def _handle_get_system_apikey(self, writer):
        res_body = json.dumps({
            "status": "success",
            "api_key": self.api_key
        }).encode('utf-8')
        response = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(res_body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n\r\n"
        ).encode('utf-8') + res_body
        writer.write(response)
        await writer.drain()
        writer.close()

    async def _handle_post_system_apikey(self, body_bytes, writer):
        try:
            req_data = json.loads(body_bytes.decode('utf-8'))
        except Exception:
            body = b'{"error":"Invalid JSON payload"}'
            self._send_json_error(400, body, writer)
            return

        new_key = req_data.get("api_key")
        regenerate = req_data.get("regenerate", False)

        if regenerate:
            new_key = "sg_ingest_" + secrets.token_hex(16)
        elif not new_key or len(new_key) < 8:
            body = b'{"error":"API key must be at least 8 characters long"}'
            self._send_json_error(400, body, writer)
            return
        else:
            if not new_key.startswith("sg_ingest_"):
                new_key = "sg_ingest_" + new_key

        try:
            with open(self.api_key_path, "w") as f:
                f.write(new_key)
            self.api_key = new_key

            res_body = json.dumps({
                "status": "success",
                "api_key": self.api_key
            }).encode('utf-8')

            response = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(res_body)}\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"Connection: close\r\n\r\n"
            ).encode('utf-8') + res_body
            writer.write(response)
            await writer.drain()
            writer.close()
        except Exception as e:
            res_body = json.dumps({"error": f"Failed to save API key: {e}"}).encode('utf-8')
            self._send_json_error(500, res_body, writer)

    async def _handle_get_system_readkey(self, writer):
        res_body = json.dumps({
            "status": "success",
            "api_key": self.read_key
        }).encode('utf-8')
        response = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(res_body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n\r\n"
        ).encode('utf-8') + res_body
        writer.write(response)
        await writer.drain()
        writer.close()

    async def _handle_post_system_readkey(self, body_bytes, writer):
        try:
            req_data = json.loads(body_bytes.decode('utf-8'))
        except Exception:
            body = b'{"error":"Invalid JSON payload"}'
            self._send_json_error(400, body, writer)
            return

        new_key = req_data.get("api_key")
        regenerate = req_data.get("regenerate", False)

        if regenerate:
            new_key = "sg_read_" + secrets.token_hex(16)
        elif not new_key or len(new_key) < 8:
            body = b'{"error":"API key must be at least 8 characters long"}'
            self._send_json_error(400, body, writer)
            return
        else:
            if not new_key.startswith("sg_read_"):
                new_key = "sg_read_" + new_key

        try:
            with open(self.read_key_path, "w") as f:
                f.write(new_key)
            self.read_key = new_key

            res_body = json.dumps({
                "status": "success",
                "api_key": self.read_key
            }).encode('utf-8')

            response = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(res_body)}\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"Connection: close\r\n\r\n"
            ).encode('utf-8') + res_body
            writer.write(response)
            await writer.drain()
            writer.close()
        except Exception as e:
            res_body = json.dumps({"error": f"Failed to save Read Key: {e}"}).encode('utf-8')
            self._send_json_error(500, res_body, writer)



    def _send_json_error(self, code, body_bytes, writer):
        status_msg = "Bad Request" if code == 400 else "Not Found" if code == 404 else "Internal Server Error"
        response = (
            f"HTTP/1.1 {code} {status_msg}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n\r\n"
        ).encode('utf-8') + body_bytes
        writer.write(response)
        writer.close()

    async def _handle_sse_stream(self, query_params, writer, reader):
        topic = query_params.get("topic")
        if not topic:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        start_idx = query_params.get("start_idx")
        start_idx_val = None
        if start_idx is not None:
            try:
                start_idx_val = int(start_idx)
            except ValueError:
                pass

        stream = self._get_stream(topic)
        catchup_limit_offset = os.path.getsize(stream.filename)

        queue = asyncio.Queue()
        self.topic_subscribers.setdefault(topic, set()).add(queue)

        try:
            # Write SSE headers
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\n"
                b"Access-Control-Allow-Origin: *\r\n\r\n"
            )
            await writer.drain()

            # 1. Historical catch-up phase
            if start_idx_val is not None:
                if start_idx_val < 0:
                    current_offset = stream.locate_records_from_end(abs(start_idx_val))
                else:
                    current_offset = stream.skip_next_event(start_idx_val)
                
                while current_offset < catchup_limit_offset:
                    records = list(stream.read_records(start_offset=current_offset, limit=1))
                    if not records:
                        break
                    offset, payload, src, ts = records[0]
                    
                    sse_packet = {
                        "ts": ts,
                        "src": src,
                        "data": payload
                    }
                    sse_data = f"data: {json.dumps(sse_packet)}\n\n"
                    writer.write(sse_data.encode('utf-8'))
                    await writer.drain()
                    
                    current_offset = stream.skip_next_event(offset + 1)

            # 2. Live streaming loop
            while True:
                sse_data = await queue.get()
                writer.write(sse_data.encode('utf-8'))
                await writer.drain()
                queue.task_done()

        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            if topic in self.topic_subscribers:
                self.topic_subscribers[topic].discard(queue)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_static_files(self, path, writer):
        # Clean path to prevent path traversal
        clean_path = path.strip("/")
        if not clean_path:
            clean_path = "index.html"
            
        full_path = os.path.join(self.static_dir, clean_path)
        
        # Direct match check, or default back to index.html for SPA client-side routing support
        if not os.path.exists(full_path) or os.path.isdir(full_path):
            full_path = os.path.join(self.static_dir, "index.html")

        if not os.path.exists(full_path):
            # No front-end built yet, serve standard placeholder index
            body = (
                b"<html><head><title>Sluicegate API</title></head>"
                b"<body style='font-family:sans-serif;text-align:center;padding:40px;background:#121214;color:#eee;'>"
                b"<h1>Sluicegate API Server</h1>"
                b"<p>Admin portal is not yet compiled. Run <code>npm run build</code> in the admin directory.</p>"
                b"</body></html>"
            )
            response = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: text/html\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode('utf-8') + body
            writer.write(response)
            await writer.drain()
            writer.close()
            return

        # Serve requested file
        mtype, _ = mimetypes.guess_type(full_path)
        if not mtype:
            mtype = "application/octet-stream"

        try:
            with open(full_path, 'rb') as f:
                content = f.read()
            
            cache_control = ""
            if full_path.endswith("index.html"):
                cache_control = "Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\n"

            response = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: {mtype}\r\n"
                f"Content-Length: {len(content)}\r\n"
                f"{cache_control}"
                f"Access-Control-Allow-Origin: *\r\n"
                f"Connection: close\r\n\r\n"
            ).encode('utf-8') + content
            writer.write(response)
            await writer.drain()
        except Exception as e:
            print(f"Error reading file {full_path}: {e}", file=sys.stderr)
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()

    async def stop(self):
        if self.watcher:
            self.watcher.close()

        # Cancel all background broadcasting tasks
        for task in self.topic_broadcast_tasks.values():
            task.cancel()
        for task in self.topic_broadcast_tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Cancel all active client connection tasks to prevent event loop hanging
        for task in list(self.connection_tasks):
            task.cancel()
        
        # Close active streams
        for stream in self.active_streams.values():
            stream.close()

        if self.server:
            self.server.close()
            await self.server.wait_closed()


if __name__ == "__main__":
    # Launch main API server
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    streams = os.environ.get("SLUICEGATE_STREAMS_DIR", os.path.join(base_dir, "streams"))
    static = os.environ.get("SLUICEGATE_STATIC_DIR", os.path.join(base_dir, "admin", "dist"))
    host = os.environ.get("SLUICEGATE_HOST", "0.0.0.0")
    port = int(os.environ.get("SLUICEGATE_PORT", 8088))
    
    server = SluicegateApiServer(streams, static, host=host, port=port)
    
    loop = asyncio.get_event_loop()
    loop.run_until_complete(server.start())
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(server.stop())
