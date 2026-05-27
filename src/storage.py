#!/usr/bin/env python3
import os
import time
import re
import json
import ctypes
import ctypes.util
import calendar
import datetime

# Cleanroom POSIX fallocate wrapper via libc
try:
    import fallocate as pyfallocate
    FALLOC_FL_KEEP_SIZE = pyfallocate.FALLOC_FL_KEEP_SIZE
    FALLOC_FL_PUNCH_HOLE = pyfallocate.FALLOC_FL_PUNCH_HOLE
    fallocate_call = pyfallocate.fallocate
except ImportError:
    FALLOC_FL_KEEP_SIZE = 0x01
    FALLOC_FL_PUNCH_HOLE = 0x02
    def fallocate_call(fd, offset, length, mode=0):
        libc_name = ctypes.util.find_library('c')
        if not libc_name:
            raise OSError("Could not find standard C library for fallocate system call")
        libc = ctypes.CDLL(libc_name, use_errno=True)
        _falloc = libc.fallocate
        # int fallocate(int fd, int mode, off_t offset, off_t len);
        _falloc.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64]
        _falloc.restype = ctypes.c_int
        res = _falloc(fd, mode, offset, length)
        if res != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))

# Optional xattr module support
try:
    import xattr
except ImportError:
    # Minimal pure ctypes or mock fallback for systems without xattr package
    class xattr:
        @staticmethod
        def setxattr(path, name, value):
            libc_name = ctypes.util.find_library('c')
            if not libc_name: return
            libc = ctypes.CDLL(libc_name, use_errno=True)
            res = libc.setxattr(path.encode(), name.encode(), value, len(value), 0)
            if res != 0:
                errno = ctypes.get_errno()
                raise OSError(errno, os.strerror(errno))
        
        @staticmethod
        def getxattr(path, name):
            libc_name = ctypes.util.find_library('c')
            if not libc_name: raise OSError("No C library found")
            libc = ctypes.CDLL(libc_name, use_errno=True)
            # query size first
            size = libc.getxattr(path.encode(), name.encode(), None, 0)
            if size < 0:
                errno = ctypes.get_errno()
                raise OSError(errno, os.strerror(errno))
            buf = ctypes.create_string_buffer(size)
            res = libc.getxattr(path.encode(), name.encode(), buf, size)
            if res < 0:
                errno = ctypes.get_errno()
                raise OSError(errno, os.strerror(errno))
            return buf.value

        @staticmethod
        def listxattr(path):
            libc_name = ctypes.util.find_library('c')
            if not libc_name: return []
            libc = ctypes.CDLL(libc_name, use_errno=True)
            size = libc.listxattr(path.encode(), None, 0)
            if size < 0:
                return []
            buf = ctypes.create_string_buffer(size)
            res = libc.listxattr(path.encode(), buf, size)
            if res < 0:
                return []
            # Split null-terminated string list
            return [x.decode() for x in buf.raw[:size].split(b'\x00') if x]


class SequentialLogStream:
    """
    High-performance, JSON-oriented sequential edge storage log stream.
    Acts as a context manager and handles Seek & Read block traversals,
    $O(log N)$ time-based trimming, and dynamic metadata attributes.
    """

    # Decoupled succinct regex patterns
    pattern_nxt = b'{"nxt":(\\d+),"ts":(\\d+\\.\\d+)'
    pattern_prv = b'.*"prv":(\\d+)},$'
    
    BOD = re.compile(pattern_nxt)
    EOD = re.compile(pattern_prv)

    # Inode attribute names
    ATTR_DATA_START = "user.SGC.DATA_START"
    ATTR_MAX_BLOCKS = "user.SGC.MAX_BLOCKS"
    ATTR_MAX_AGE_MIN = "user.SGC.MAX_AGE_MIN"
    ATTR_SAFETY_HEADROOM = "user.SGC.SAFETY_HEADROOM"
    ATTR_POLL_DELAY_SEC = "user.SGC.POLL_DELAY_SEC"

    DEFAULT_METADATA = {
        ATTR_MAX_BLOCKS: "204800",    # ~100MB default max allocated blocks (512 bytes each)
        ATTR_MAX_AGE_MIN: "1440",        # 24 hours retention default
        ATTR_SAFETY_HEADROOM: "100",     # Trigger buffer blocks
        ATTR_POLL_DELAY_SEC: "10.0"      # Poll interval
    }

    PEEK_WINDOW_SIZE = 128
    PEEK_LOOKAHEAD_SIZE = 256

    def __init__(self, filename, attribute_overrides=None):
        self.filename = os.path.abspath(filename)
        self.attributes = {}
        
        # Open/create directory structure
        dirname = os.path.dirname(self.filename)
        if dirname and not os.path.exists(dirname):
            os.makedirs(dirname, exist_ok=True)

        self.flow_fd = open(self.filename, mode='a+b')
        self.flow_fno = self.flow_fd.fileno()
        
        # Retrieve OS sector/block size for transaction alignment
        self.io_blk_size = os.fstat(self.flow_fno).st_blksize
        if self.io_blk_size <= 0:
            self.io_blk_size = 4096 # fallback

        # Load / write extended attributes
        attribute_overrides = attribute_overrides or {}
        for key, default_val in self.DEFAULT_METADATA.items():
            try:
                val = xattr.getxattr(self.filename, key).decode().strip()
            except OSError:
                val = str(attribute_overrides.get(key, default_val))
                try:
                    xattr.setxattr(self.filename, key, val.encode())
                except OSError:
                    pass # attributes unsupported or read-only filesystem
            self.attributes[key] = val

        self.max_blocks = int(self.attributes[self.ATTR_MAX_BLOCKS])
        self.max_age_minutes = int(self.attributes[self.ATTR_MAX_AGE_MIN])
        self.safety_headroom = int(self.attributes[self.ATTR_SAFETY_HEADROOM])
        self.gc_poll_delay = float(self.attributes[self.ATTR_POLL_DELAY_SEC])

        # Discover active logical stream start offset
        try:
            self.first_data_byte = int(xattr.getxattr(self.filename, self.ATTR_DATA_START).decode().strip())
        except OSError:
            self.first_data_byte = self.skip_next_event(0)
            try:
                xattr.setxattr(self.filename, self.ATTR_DATA_START, str(self.first_data_byte).encode())
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        if not self.flow_fd.closed:
            self.flow_fd.close()

    def reload_attributes(self):
        """Synchronize metadata limits and flags in real-time from the filesystem inode"""
        for key in self.DEFAULT_METADATA.keys():
            try:
                val = xattr.getxattr(self.filename, key).decode().strip()
                self.attributes[key] = val
            except OSError:
                pass
        self.max_blocks = int(self.attributes[self.ATTR_MAX_BLOCKS])
        self.max_age_minutes = int(self.attributes[self.ATTR_MAX_AGE_MIN])
        self.safety_headroom = int(self.attributes[self.ATTR_SAFETY_HEADROOM])
        self.gc_poll_delay = float(self.attributes[self.ATTR_POLL_DELAY_SEC])

    def update_attributes(self, max_blocks=None, max_age_min=None):
        """Update xattr config parameters on the inode and sync them"""
        if max_blocks is not None:
            self.max_blocks = int(max_blocks)
            self.attributes[self.ATTR_MAX_BLOCKS] = str(self.max_blocks)
            try:
                xattr.setxattr(self.filename, self.ATTR_MAX_BLOCKS, str(self.max_blocks).encode())
            except OSError:
                pass
        if max_age_min is not None:
            self.max_age_minutes = int(max_age_min)
            self.attributes[self.ATTR_MAX_AGE_MIN] = str(self.max_age_minutes)
            try:
                xattr.setxattr(self.filename, self.ATTR_MAX_AGE_MIN, str(self.max_age_minutes).encode())
            except OSError:
                pass

    def get_physical_stats(self):
        """Query physical file statistics directly from the filesystem"""
        stat_buf = os.stat(self.filename)
        return stat_buf.st_blocks, stat_buf.st_size

    def append(self, payload: dict, source: str) -> int:
        """
        JSON-oriented stream append.
        Calculates exact bidirectional pointer jumps on the fly and appends to disk.
        """
        # Format payload and source info
        payload_str = json.dumps(payload, separators=(',', ':'))
        ts_val = time.time()
        
        # Template construction:
        # {"nxt":<nxt_val>,"ts":<ts_val>,"src":"<source>","data":<payload_str>,"prv":<prv_val>},
        fields_part = f',\"ts\":{ts_val},\"src\":\"{source}\",\"data\":{payload_str},\"prv\":'
        
        # Calculate dynamic size:
        base_size = len('{"nxt":') + len(fields_part) + len('},')
        # Digits count of final size itself
        num_digits = len(str(base_size))
        final_size = base_size + num_digits * 2
        if len(str(final_size)) != num_digits:
            num_digits = len(str(final_size))
            final_size = base_size + num_digits * 2

        # Format complete record
        record = f'{{"nxt":{final_size}{fields_part}{final_size}}},'
        record_bytes = record.encode('utf-8')
        
        # Ensure we write strictly to the end of the stream file
        self.flow_fd.seek(0, os.SEEK_END)
        offset = self.flow_fd.tell()
        self.flow_fd.write(record_bytes)
        self.flow_fd.flush()
        return offset

    def skip_next_event(self, start_offset: int) -> int:
        """
        Seek & Read Sweep: Discovers the exact next valid record starting at or after start_offset,
        safely bypassing any zero-filled holes. Correctly returns file_size if no next event is found.
        """
        file_size = os.path.getsize(self.filename)
        if start_offset >= file_size:
            return file_size

        self.flow_fd.seek(start_offset, os.SEEK_SET)
        block_offset = start_offset
        peek_size = self.PEEK_LOOKAHEAD_SIZE + self.io_blk_size
        buf = bytearray(peek_size)

        while True:
            self.flow_fd.seek(block_offset, os.SEEK_SET)
            bytes_read = self.flow_fd.readinto(buf)
            if bytes_read == 0:
                # Reached EOF
                return file_size

            # Search signature in buffer
            match = self.BOD.search(buf[:bytes_read])
            if match:
                return block_offset + match.start()
            
            # Step forward by physical block boundary
            block_offset += self.io_blk_size
            if block_offset >= file_size:
                return file_size

    def _trim_leading_bytes(self, size: int):
        """Zero-copy sparse file head reclamation using block-aligned POSIX fallocate PUNCH_HOLE"""
        if size <= 0:
            return

        file_size = os.path.getsize(self.filename)
        # Find safe record boundary where active data starts after size offset
        hole_end = self.skip_next_event(self.first_data_byte + size)
        
        # Prevent deallocating the entire file (must leave at least 1 record)
        if hole_end >= file_size:
            # We reached or exceeded EOF. Seek backward from file end to locate the last record.
            self.flow_fd.seek(max(0, file_size - 48), os.SEEK_SET)
            last_bytes = self.flow_fd.read(48)
            match = self.EOD.search(last_bytes)
            if match:
                last_record_size = int(match.group(1).decode('utf-8'))
                hole_end = file_size - last_record_size
            else:
                # Cannot parse last record, skip deallocation to be robust
                return

        # Align hole_start backwards to physical block boundary
        block_offset = self.first_data_byte % self.io_blk_size
        hole_start = self.first_data_byte - block_offset

        # Perform kernel deallocation
        punch_length = hole_end - hole_start
        if punch_length > 0:
            fallocate_call(self.flow_fno, hole_start, punch_length, mode=FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE)
            
        self.first_data_byte = hole_end
        try:
            xattr.setxattr(self.filename, self.ATTR_DATA_START, str(self.first_data_byte).encode())
        except OSError:
            pass

    def evaluate_retention(self):
        """Evaluates size limits and age bounds and reclaims disk blocks on overflow"""
        blocks, file_size = self.get_physical_stats()
        
        # 1. Size-based trim check
        if blocks > self.max_blocks:
            overflow_blocks = blocks - self.max_blocks
            if overflow_blocks > self.safety_headroom:
                trim_size = overflow_blocks * 512
                # Cap the trim size so we don't try to deallocate past active file bounds
                active_size = file_size - self.first_data_byte
                trim_size = min(trim_size, active_size)
                self._trim_leading_bytes(trim_size)

        # 2. Time-based trim check
        blocks, file_size = self.get_physical_stats()
        if self.max_age_minutes > 0 and self.first_data_byte < file_size:
            cutoff_epoch = time.time() - (self.max_age_minutes * 60)
            
            low = self.first_data_byte
            high = file_size
            target_trim_byte = low

            # O(log N) block-aligned binary search to isolate cutoff record
            while low < high:
                mid = (low + high) // 2
                # Only align mid to block boundaries if we have substantial search range
                if (high - low) >= self.io_blk_size:
                    mid = (mid // self.io_blk_size) * self.io_blk_size
                
                event_offset = self.skip_next_event(mid)
                if event_offset >= file_size:
                    high = mid
                    continue

                self.flow_fd.seek(event_offset, os.SEEK_SET)
                peek_buf = self.flow_fd.read(self.PEEK_WINDOW_SIZE)
                match = self.BOD.search(peek_buf)
                if match:
                    try:
                        ts_val = float(match.group(2).decode('utf-8'))
                        if ts_val < cutoff_epoch:
                            target_trim_byte = event_offset
                            low = event_offset + 1
                        else:
                            high = mid
                    except Exception:
                        break
                else:
                    break

            if target_trim_byte > self.first_data_byte:
                trim_bytes = target_trim_byte - self.first_data_byte
                self._trim_leading_bytes(trim_bytes)

    def read_records(self, start_offset=None, limit=None):
        """
        Generates/traverses serialized records sequentially forward.
        Yields tuple of (absolute_offset, data_dict, source, timestamp).
        """
        file_size = os.path.getsize(self.filename)
        current = self.first_data_byte if start_offset is None else start_offset
        current = self.skip_next_event(current)
        
        count = 0
        while current < file_size:
            if limit is not None and count >= limit:
                break
                
            self.flow_fd.seek(current, os.SEEK_SET)
            peek_buf = self.flow_fd.read(self.PEEK_WINDOW_SIZE)
            if not peek_buf:
                break

            match = self.BOD.search(peek_buf)
            if not match:
                # Seek to next block boundary or break
                current = self.skip_next_event(current + len(peek_buf))
                continue

            nxt_val = int(match.group(1).decode('utf-8'))
            
            # Read complete record payload
            self.flow_fd.seek(current, os.SEEK_SET)
            full_record_bytes = self.flow_fd.read(nxt_val)
            if len(full_record_bytes) < nxt_val:
                break # partial record

            # Extract fields via regex or json parsing
            try:
                record_str = full_record_bytes.decode('utf-8')
                # Strict JSON parse by trimming wrap outer details
                # {"nxt":XX,"ts":YY,"src":"ZZ","data":{...},"prv":XX},
                data_start_idx = record_str.find(',"data":') + len(',"data":')
                prv_idx = record_str.find(',"prv":')
                
                data_part = record_str[data_start_idx:prv_idx]
                payload = json.loads(data_part)

                ts_start = record_str.find('"ts":') + 5
                ts_end = record_str.find(',"src"')
                ts_val = float(record_str[ts_start:ts_end])

                src_start = record_str.find('"src":"') + 7
                src_end = record_str.find('","data"')
                src_val = record_str[src_start:src_end]

                yield current, payload, src_val, ts_val
                count += 1
            except Exception:
                pass

            current += nxt_val

    def locate_records_from_end(self, count: int) -> int:
        """
        Seeks backwards from EOF by `count` records using relative `prv` pointers.
        Returns the absolute start offset of the N-th record from the end.
        """
        file_size = os.path.getsize(self.filename)
        if count <= 0 or file_size <= self.first_data_byte:
            return self.first_data_byte

        current = file_size
        for _ in range(count):
            if current <= self.first_data_byte:
                break
            # Read the trailing segment of the current record
            seek_pos = max(self.first_data_byte, current - 48)
            self.flow_fd.seek(seek_pos, os.SEEK_SET)
            last_bytes = self.flow_fd.read(current - seek_pos)
            
            match = self.EOD.search(last_bytes)
            if match:
                record_size = int(match.group(1).decode('utf-8'))
                current -= record_size
            else:
                break
        return max(self.first_data_byte, current)
