#!/usr/bin/env python3
import os
import sys
import time
import shutil
import unittest

# Append workspace src directory to import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))
from storage import SequentialLogStream


class TestSequentialStorage(unittest.TestCase):
    def setUp(self):
        self.test_dir = "/tmp/sluicegate_test"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)
        self.log_file = os.path.join(self.test_dir, "test_stream.json")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_basic_append_and_read(self):
        """Verify appending and reading succinct records sequentially works"""
        payloads = [
            {"sensor": "temp", "val": 22.4},
            {"sensor": "humidity", "val": 45.2},
            {"sensor": "lux", "val": 350}
        ]

        with SequentialLogStream(self.log_file) as stream:
            # Append events
            offsets = []
            for i, p in enumerate(payloads):
                offset = stream.append(payload=p, source=f"node_{i}")
                offsets.append(offset)

            # Check file size
            _, size = stream.get_physical_stats()
            self.assertTrue(size > 0)

            # Read back sequentially
            records = list(stream.read_records())
            self.assertEqual(len(records), 3)

            for idx, (offset, payload, src, ts) in enumerate(records):
                self.assertEqual(payload, payloads[idx])
                self.assertEqual(src, f"node_{idx}")
                self.assertTrue(ts > 0)
                self.assertEqual(offset, offsets[idx])

    def test_size_based_trimming(self):
        """Verify stream limits physical block counts via POSIX hole punching"""
        # Initialize stream with max retention of 2 blocks (1024 bytes) and safety headroom of 0
        attribute_overrides = {
            "user.SGC.MAX_BLOCKS": "2",       # Max 1024 bytes
            "user.SGC.SAFETY_HEADROOM": "0"   # Trigger immediately
        }

        with SequentialLogStream(self.log_file, attribute_overrides) as stream:
            # Append 30 large payloads to ensure we cross physical 4096-byte boundaries
            large_payload = {"msg": "A" * 300} # ~380 bytes serialized
            for i in range(30):
                stream.append(payload=large_payload, source=f"node_{i}")

            blocks_before, size_before = stream.get_physical_stats()
            
            # Trigger manual retention policy check
            stream.evaluate_retention()

            blocks_after, size_after = stream.get_physical_stats()
            
            # The physical blocks allocated should have decreased due to fallocate PUNCH_HOLE
            self.assertTrue(blocks_after < blocks_before)
            # The file size itself should remain the same (KEEP_SIZE flag)
            self.assertEqual(size_after, size_before)

            # Verify that we can still read the surviving records safely
            records = list(stream.read_records())
            self.assertTrue(len(records) > 0)
            self.assertTrue(len(records) < 30) # some leading events were pruned

    def test_time_based_trimming(self):
        """Verify stream reclaims blocks older than age threshold using binary search"""
        attribute_overrides = {
            "user.SGC.MAX_AGE_MIN": "1",       # 1 minute age limit
            "user.SGC.SAFETY_HEADROOM": "0"
        }

        with SequentialLogStream(self.log_file, attribute_overrides) as stream:
            # Append 5 events
            for i in range(5):
                stream.append(payload={"temp": 20.0 + i}, source="sensor")
            
            # Monkeypatch time to be 10 minutes in the future
            orig_time = time.time
            try:
                time.time = lambda: orig_time() + 600
                stream.evaluate_retention()
            finally:
                time.time = orig_time
            
            # The stream should deallocate all events but preserve the last one
            records = list(stream.read_records())
            self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
