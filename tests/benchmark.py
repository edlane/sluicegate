#!/usr/bin/env python3
import os
import sys
import time
import json
import asyncio
import threading
import shutil
import statistics

# Append workspace src directory to import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))
from storage import SequentialLogStream

# Also import ServerSentEventsServer and InotifyWatcher from tests
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from test_sse_inotify import ServerSentEventsServer


def ingest_worker(filepath, count, source, start_event):
    """
    Ingestion worker thread that appends events to the storage stream as fast as possible.
    Wait for start_event before initiating ingestion.
    """
    start_event.wait()
    
    with SequentialLogStream(filepath) as stream:
        for seq in range(count):
            payload = {
                "seq": seq,
                "send_t": time.time(),
                "data": "x" * 128  # 128 bytes of dummy payload to simulate realistic telemetry
            }
            stream.append(payload=payload, source=source)


async def subscriber_client_task(topic_id, client_id, port, expected_count, results_list, latencies_list):
    """
    High-performance concurrent subscriber client.
    Connects to the SSE server and consumes exactly expected_count events.
    Computes precise high-precision telemetry latency.
    """
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        
        # Send standard HTTP stream request
        request = (
            f"GET /stream HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Accept: text/event-stream\r\n\r\n"
        )
        writer.write(request.encode('utf-8'))
        await writer.drain()

        # Read standard HTTP headers response
        await reader.readuntil(b"\r\n\r\n")

        # Consume event chunks
        events_received = 0
        while events_received < expected_count:
            line = await reader.readline()
            if not line:
                break
            line_str = line.decode('utf-8').strip()
            if line_str.startswith("data:"):
                payload_json = line_str[5:].strip()
                event = json.loads(payload_json)
                recv_time = time.time()
                
                # Extract telemetry metrics
                data = event.get("data", {})
                seq = data.get("seq")
                send_time = data.get("send_t")
                
                if send_time is not None:
                    latencies_list.append(recv_time - send_time)
                
                results_list.append(seq)
                events_received += 1

        writer.close()
        await writer.wait_closed()
    except Exception as e:
        print(f"\n[Client {topic_id}-{client_id}] Connection error: {e}", file=sys.stderr)


async def run_stress_test(events_per_topic, num_subscribers):
    """
    Orchestrates the 100k events stress test.
    Spawns 2 SSE servers, connects 3 subscribers to each, runs 2 parallel ingestion threads,
    monitors metrics, verifies integrity, and prints a comprehensive high-fidelity report.
    """
    test_dir = "/tmp/sluicegate_stress_test"
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)
    os.makedirs(test_dir)

    topic_1_path = os.path.join(test_dir, "topic_1.json")
    topic_2_path = os.path.join(test_dir, "topic_2.json")

    # Touch stream files
    with open(topic_1_path, 'w') as f: pass
    with open(topic_2_path, 'w') as f: pass

    port_1 = 8191
    port_2 = 8192

    # Instantiate and start SSE servers
    server_1 = ServerSentEventsServer(topic_1_path, port=port_1)
    server_2 = ServerSentEventsServer(topic_2_path, port=port_2)

    await server_1.start()
    await server_2.start()

    # Shared telemetry storage
    t1_client_results = [[] for _ in range(num_subscribers)]
    t2_client_results = [[] for _ in range(num_subscribers)]
    
    latencies_1 = []
    latencies_2 = []

    # 1. Start subscriber tasks
    sub_tasks = []
    for i in range(num_subscribers):
        sub_tasks.append(
            asyncio.create_task(
                subscriber_client_task(
                    "Topic1", i, port_1, events_per_topic, t1_client_results[i], latencies_1
                )
            )
        )
        sub_tasks.append(
            asyncio.create_task(
                subscriber_client_task(
                    "Topic2", i, port_2, events_per_topic, t2_client_results[i], latencies_2
                )
            )
        )

    # Allow clients to connect and register with servers
    while len(server_1.subscribers) < num_subscribers or len(server_2.subscribers) < num_subscribers:
        await asyncio.sleep(0.1)

    print("\n" + "="*80)
    print("                      SLUICEGATE HIGH-STRESS BENCHMARK")
    print("="*80)
    print(f"[-] Topics: 2 ('{topic_1_path}', '{topic_2_path}')")
    print(f"[-] Subscribers: {num_subscribers} per Topic (Total: {num_subscribers * 2})")
    print(f"[-] Ingestion Goal: {events_per_topic} events per Topic (Total: {events_per_topic * 2} events)")
    print(f"[-] Expecting: {events_per_topic * num_subscribers * 2} total events received")
    print("[-] Starting ingestion threads concurrently...")

    # Shared thread start synchronizer
    start_event = threading.Event()

    t1_thread = threading.Thread(target=ingest_worker, args=(topic_1_path, events_per_topic, "producer_1", start_event))
    t2_thread = threading.Thread(target=ingest_worker, args=(topic_2_path, events_per_topic, "producer_2", start_event))

    t1_thread.start()
    t2_thread.start()

    benchmark_start_time = time.time()
    
    # Unleash ingestion threads simultaneously
    start_event.set()

    # 2. Live progress monitoring loop
    total_expected_recv = events_per_topic * num_subscribers * 2
    
    while True:
        # Sum total received events across all subscribers
        t1_recv = sum(len(r) for r in t1_client_results)
        t2_recv = sum(len(r) for r in t2_client_results)
        total_recv = t1_recv + t2_recv
        
        elapsed = time.time() - benchmark_start_time
        pct = (total_recv / total_expected_recv) * 100
        
        # Calculate instant throughput
        recv_rate = total_recv / max(0.01, elapsed)
        
        # Print status updates cleanly in-place
        sys.stdout.write(
            f"\r[Progress] Received: {total_recv}/{total_expected_recv} ({pct:.1f}%) | "
            f"Elapsed: {elapsed:.2f}s | Throughput: {recv_rate:.1f} events/sec (broadcasted)"
        )
        sys.stdout.flush()

        if total_recv >= total_expected_recv:
            break
        
        await asyncio.sleep(0.2)

    t1_thread.join()
    t2_thread.join()
    
    # Wait for all client subscription tasks to cleanly shutdown
    await asyncio.gather(*sub_tasks, return_exceptions=True)
    
    benchmark_end_time = time.time()
    total_duration = benchmark_end_time - benchmark_start_time

    # 3. Shutdown servers cleanly
    print("\n[-] Shutting down servers and cleaning up...")
    await server_1.stop()
    await server_2.stop()

    # 4. Perform Data Integrity Verifications
    print("\n[+] Performing data integrity verifications...")
    integrity_failed = False

    # Check Topic 1
    for i in range(num_subscribers):
        results = t1_client_results[i]
        if len(results) != events_per_topic:
            print(f"    [FAIL] Topic 1 Subscriber {i} received {len(results)}/{events_per_topic} events!")
            integrity_failed = True
            continue
        
        # Verify perfect sequence: 0 to expected-1
        is_ordered = all(results[idx] == idx for idx in range(events_per_topic))
        if not is_ordered:
            print(f"    [FAIL] Topic 1 Subscriber {i} sequence is out of order or contains duplicates!")
            integrity_failed = True

    # Check Topic 2
    for i in range(num_subscribers):
        results = t2_client_results[i]
        if len(results) != events_per_topic:
            print(f"    [FAIL] Topic 2 Subscriber {i} received {len(results)}/{events_per_topic} events!")
            integrity_failed = True
            continue
        
        is_ordered = all(results[idx] == idx for idx in range(events_per_topic))
        if not is_ordered:
            print(f"    [FAIL] Topic 2 Subscriber {i} sequence is out of order or contains duplicates!")
            integrity_failed = True

    if not integrity_failed:
        print("    [PASS] 100% data integrity verified! Zero events lost, zero duplicates, perfect sequence order.")
    else:
        print("    [FAIL] Data integrity violations detected. Check logs.")

    # 5. Compile Statistics
    combined_latencies = latencies_1 + latencies_2
    
    ingest_throughput = (events_per_topic * 2) / total_duration
    broadcast_throughput = total_expected_recv / total_duration

    mean_lat = statistics.mean(combined_latencies) if combined_latencies else 0
    min_lat = min(combined_latencies) if combined_latencies else 0
    max_lat = max(combined_latencies) if combined_latencies else 0
    
    combined_latencies.sort()
    p95_lat = combined_latencies[int(len(combined_latencies) * 0.95)] if combined_latencies else 0
    p99_lat = combined_latencies[int(len(combined_latencies) * 0.99)] if combined_latencies else 0

    # Clean up test directories
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)

    # Print Gorgeous Performance Report
    print("\n" + "="*80)
    print("                          BENCHMARK PERFORMANCE REPORT")
    print("="*80)
    print(f"  Execution Time:         {total_duration:.3f} seconds")
    print(f"  Total Ingested Events:  {events_per_topic * 2:,}")
    print(f"  Total Broadcasted:      {total_expected_recv:,} events")
    print(f"  Ingestion Rate:         {ingest_throughput:.2f} events/sec")
    print(f"  Broadcasting Rate:      {broadcast_throughput:.2f} events/sec")
    print("-"*80)
    print("  LATENCY METRICS (Ingest-to-Subscriber Delivery):")
    print(f"    Min Latency:          {min_lat * 1000:.3f} ms")
    print(f"    Mean Latency:         {mean_lat * 1000:.3f} ms")
    print(f"    P95 Latency:          {p95_lat * 1000:.3f} ms")
    print(f"    P99 Latency:          {p99_lat * 1000:.3f} ms")
    print(f"    Max Latency:          {max_lat * 1000:.3f} ms")
    print("="*80)
    
    return not integrity_failed


if __name__ == "__main__":
    # Stress test configuration: 50,000 events per topic, 3 subscribers per topic
    events_per_topic = 50000
    num_subscribers = 3
    
    # Accept command line overrides
    if len(sys.argv) > 1:
        try:
            total_events = int(sys.argv[1])
            events_per_topic = total_events // 2
        except ValueError:
            print("Usage: python3 benchmark.py [total_events] [subscribers_per_topic]")
            sys.exit(1)
            
    if len(sys.argv) > 2:
        try:
            num_subscribers = int(sys.argv[2])
        except ValueError:
            pass

    success = asyncio.run(run_stress_test(events_per_topic, num_subscribers))
    sys.exit(0 if success else 1)
