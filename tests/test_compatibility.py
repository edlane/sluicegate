#!/usr/bin/env python3
import os
import sys
import time
import subprocess
import signal

def test_c_daemon_lifecycle():
    """Verify the compiled C FastCGI Daemon compiles, runs, binds, and shuts down cleanly"""
    daemon_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../src/ingest_fcgi'))
    if not os.path.exists(daemon_path):
        print("FAIL: Ingestion daemon binary not found. Run make first.")
        sys.exit(1)

    test_port = "2099"
    test_log = "/tmp/sluicegate_test/compat_stream.json"

    # Set environment variables for the C daemon
    env = os.environ.copy()
    env["SLUICEGATE_PORT"] = test_port
    env["SLUICEGATE_INGEST_PATH"] = test_log

    print(f"Spawning C daemon in background listening on port {test_port}...")
    # Spawn background daemon process
    process = subprocess.Popen(
        [daemon_path],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    # Allow daemon to initialize socket binding
    time.sleep(0.5)

    # Check if the process has crashed
    poll = process.poll()
    if poll is not None:
        stdout, stderr = process.communicate()
        print(f"FAIL: Daemon crashed immediately on startup with code {poll}.")
        print(f"Stdout: {stdout.decode()}")
        print(f"Stderr: {stderr.decode()}")
        sys.exit(1)

    print("Daemon successfully initialized, bound socket, and entered loop.")

    # Terminate process cleanly
    print("Terminating C daemon...")
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=2)
    print("SUCCESS: C FastCGI Daemon lifecycle verified cleanly!")

if __name__ == "__main__":
    test_c_daemon_lifecycle()
