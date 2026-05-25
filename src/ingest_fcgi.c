#include <sys/types.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <string.h>
#include <syslog.h>
#include <unistd.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <sys/time.h>
#include <sys/uio.h>
#include "fcgi_config.h"
#include "fcgiapp.h"

#define POST_DATA_SIZE_LIMIT (200 * 1024 * 1024)
#define LISTENSOCK_FILENO 0
#define DEFAULT_INGEST_PATH "/tmp/sluicegate/ingest.json"
#define DEFAULT_SOCKET_PATH ":2000"

static int socketId = LISTENSOCK_FILENO;
static const char* ingest_path = DEFAULT_INGEST_PATH;

#define LRU_CACHE_CAPACITY 8

typedef struct {
    char path[256];
    int fd;
    unsigned long last_access_seq;
} FdCacheEntry;

typedef struct {
    char path[256];
    char key[128];
    time_t last_mtime;
    unsigned long last_access_seq;
} KeyCacheEntry;

static FdCacheEntry fd_cache[LRU_CACHE_CAPACITY] = {
    {"", -1, 0}, {"", -1, 0}, {"", -1, 0}, {"", -1, 0},
    {"", -1, 0}, {"", -1, 0}, {"", -1, 0}, {"", -1, 0}
};
static KeyCacheEntry key_cache[LRU_CACHE_CAPACITY] = {
    {"", "", 0, 0}, {"", "", 0, 0}, {"", "", 0, 0}, {"", "", 0, 0},
    {"", "", 0, 0}, {"", "", 0, 0}, {"", "", 0, 0}, {"", "", 0, 0}
};
static unsigned long global_access_seq = 0;

static int get_stream_fd_lru(const char* path) {
    global_access_seq++;
    
    // 1. Search for hit
    for (int i = 0; i < LRU_CACHE_CAPACITY; ++i) {
        if (fd_cache[i].fd >= 0 && strcmp(fd_cache[i].path, path) == 0) {
            fd_cache[i].last_access_seq = global_access_seq;
            return fd_cache[i].fd;
        }
    }

    // 2. Search for empty slot
    int target_idx = -1;
    for (int i = 0; i < LRU_CACHE_CAPACITY; ++i) {
        if (fd_cache[i].fd < 0) {
            target_idx = i;
            break;
        }
    }

    // 3. If full, perform LRU eviction
    if (target_idx == -1) {
        unsigned long min_seq = fd_cache[0].last_access_seq;
        target_idx = 0;
        for (int i = 1; i < LRU_CACHE_CAPACITY; ++i) {
            if (fd_cache[i].last_access_seq < min_seq) {
                min_seq = fd_cache[i].last_access_seq;
                target_idx = i;
            }
        }
        // Close the evicted file descriptor
        if (fd_cache[target_idx].fd >= 0) {
            close(fd_cache[target_idx].fd);
            fd_cache[target_idx].fd = -1;
        }
    }

    // 4. Open the new file and insert into cache
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0664);
    if (fd >= 0) {
        snprintf(fd_cache[target_idx].path, sizeof(fd_cache[target_idx].path), "%s", path);
        fd_cache[target_idx].fd = fd;
        fd_cache[target_idx].last_access_seq = global_access_seq;
    }
    return fd;
}

static void invalidate_stream_fd(int fd) {
    for (int i = 0; i < LRU_CACHE_CAPACITY; ++i) {
        if (fd_cache[i].fd == fd) {
            close(fd_cache[i].fd);
            fd_cache[i].fd = -1;
            fd_cache[i].path[0] = '\0';
            break;
        }
    }
}

static int load_api_key_lru(const char* key_path, char* key_buf, size_t max_len) {
    global_access_seq++;
    struct stat st;
    int has_file = (stat(key_path, &st) == 0);
    time_t current_mtime = has_file ? st.st_mtime : 0;

    // 1. Search for hit
    for (int i = 0; i < LRU_CACHE_CAPACITY; ++i) {
        if (key_cache[i].path[0] != '\0' && strcmp(key_cache[i].path, key_path) == 0) {
            key_cache[i].last_access_seq = global_access_seq;
            // If mtime matches, return cached key
            if (has_file && key_cache[i].last_mtime == current_mtime) {
                snprintf(key_buf, max_len, "%s", key_cache[i].key);
                return 1;
            }
            // If mtime changed or stat failed, fall through to reload it
            break;
        }
    }

    // If file exists, read it
    char fresh_key[128] = {0};
    int read_success = 0;
    if (has_file) {
        int fd = open(key_path, O_RDONLY);
        if (fd >= 0) {
            ssize_t bytes_read = read(fd, fresh_key, sizeof(fresh_key) - 1);
            close(fd);
            if (bytes_read > 0) {
                fresh_key[bytes_read] = '\0';
                // Strip trailing newlines or whitespace
                char* end = fresh_key + strlen(fresh_key) - 1;
                while (end >= fresh_key && (*end == '\r' || *end == '\n' || *end == ' ' || *end == '\t')) {
                    *end = '\0';
                    end--;
                }
                read_success = 1;
            }
        }
    }

    // Fallback to environment variable if read failed
    if (!read_success) {
        char* env_key = getenv("SLUICEGATE_API_KEY");
        if (env_key && strlen(env_key) > 0) {
            snprintf(fresh_key, sizeof(fresh_key), "%s", env_key);
            read_success = 1;
            current_mtime = 0; // No backing file
        }
    }

    if (read_success) {
        // Find existing slot or empty slot or LRU evict
        int target_idx = -1;
        // Check if already in cache
        for (int i = 0; i < LRU_CACHE_CAPACITY; ++i) {
            if (key_cache[i].path[0] != '\0' && strcmp(key_cache[i].path, key_path) == 0) {
                target_idx = i;
                break;
            }
        }
        // Search for empty slot
        if (target_idx == -1) {
            for (int i = 0; i < LRU_CACHE_CAPACITY; ++i) {
                if (key_cache[i].path[0] == '\0') {
                    target_idx = i;
                    break;
                }
            }
        }
        // LRU evict if still not found
        if (target_idx == -1) {
            unsigned long min_seq = key_cache[0].last_access_seq;
            target_idx = 0;
            for (int i = 1; i < LRU_CACHE_CAPACITY; ++i) {
                if (key_cache[i].last_access_seq < min_seq) {
                    min_seq = key_cache[i].last_access_seq;
                    target_idx = i;
                }
            }
        }

        // Store in cache
        snprintf(key_cache[target_idx].path, sizeof(key_cache[target_idx].path), "%s", key_path);
        snprintf(key_cache[target_idx].key, sizeof(key_cache[target_idx].key), "%s", fresh_key);
        key_cache[target_idx].last_mtime = current_mtime;
        key_cache[target_idx].last_access_seq = global_access_seq;

        snprintf(key_buf, max_len, "%s", fresh_key);
        return 1;
    }

    return 0;
}

/* Succinct, decoupled JSON stream keys:
 * {"nxt":<size>,"ts":<float>,"src":"<ip:port>","data":<payload>,"prv":<size>}
 */
static const char* const fields[] = {
    "{\"nxt\":",
    ",\"ts\":",
    ",\"src\":\"",
    "\",\"data\":",
    ",\"prv\":"
};
#define FIELDS_COUNT (sizeof(fields) / sizeof(fields[0]))
#define IOV_COUNT (FIELDS_COUNT * 2)
static struct iovec iov[IOV_COUNT];

static inline int getFieldsSize() {
    int size = 0;
    for (unsigned int i = 0; i < FIELDS_COUNT; ++i) {
        size += strlen(fields[i]);
    }
    return size;
}

static inline int setToIov(int fieldNum, char* buf, int len) {
    if (len > 0) {
        int idx = fieldNum * 2;
        iov[idx].iov_base = (char*)fields[fieldNum];
        iov[idx].iov_len = strlen(fields[fieldNum]);
        iov[idx + 1].iov_base = buf;
        iov[idx + 1].iov_len = len;
        return len;
    }
    return 0;
}

static int load_api_key(char* key_buf, size_t max_len) {
    char key_path[256];
    strncpy(key_path, ingest_path, sizeof(key_path) - 1);
    key_path[sizeof(key_path) - 1] = '\0';
    char* last_slash = strrchr(key_path, '/');
    if (last_slash) {
        *last_slash = '\0';
        strncat(key_path, "/.api_key", sizeof(key_path) - strlen(key_path) - 1);
    } else {
        strncpy(key_path, ".api_key", sizeof(key_path) - 1);
    }

    return load_api_key_lru(key_path, key_buf, max_len);
}

int ingestLoop() {
    int rc = 0;
    int nSize = 0;
    char wrap_buf[128] = {0};
    FCGX_Request request;

    if ((rc = FCGX_InitRequest(&request, socketId, 0))) {
        syslog(LOG_ERR, "FCGX_InitRequest failed: %d", rc);
        return 2;
    }

    while (1) {
        rc = FCGX_Accept_r(&request);
        if (rc < 0) {
            syslog(LOG_ERR, "FCGX_Accept_r could not accept new request: %d", rc);
            break;
        }

        char* sMethod = FCGX_GetParam("REQUEST_METHOD", request.envp);
        char* sContentLen = FCGX_GetParam("HTTP_CONTENT_LENGTH", request.envp);
        int nPostLen = sContentLen ? atoi(sContentLen) : 0;

        if (sMethod && strcmp(sMethod, "POST") == 0 && nPostLen > 0 && nPostLen <= POST_DATA_SIZE_LIMIT) {
            char expected_key[128] = {0};
            if (load_api_key(expected_key, sizeof(expected_key))) {
                char* client_key = FCGX_GetParam("HTTP_X_SLUICEGATE_API_KEY", request.envp);
                if (!client_key || strcmp(client_key, expected_key) != 0) {
                    FCGX_PutS("Status: 401 Unauthorized\r\nContent-Type: application/json\r\n\r\n{\"error\":\"Unauthorized: Invalid API Key\"}", request.out);
                    FCGX_Finish_r(&request);
                    continue;
                }
            }

            memset(iov, 0, sizeof(iov));

            memset(wrap_buf, 0, sizeof(wrap_buf));
            char* bufP = &wrap_buf[0];

            char* dataBuf = malloc(nPostLen);
            if (!dataBuf) {
                syslog(LOG_ERR, "FCGI: OOM allocating data buffer of size %d", nPostLen);
                FCGX_PutS("Status: 500 Internal Server Error\r\n\r\n", request.out);
                FCGX_Finish_r(&request);
                continue;
            }

            // Read the POST payload
            nSize = FCGX_GetStr(dataBuf, nPostLen, request.in);
            if (nSize < nPostLen) {
                // Read incomplete post body
                syslog(LOG_WARNING, "FCGI: Read size %d less than Content-Length %d", nSize, nPostLen);
                nPostLen = nSize;
            }

            // 1. Add Timestamp (Unix Epoch Float: e.g. 1779821040.123456)
            struct timeval tv;
            gettimeofday(&tv, NULL);
            nSize = snprintf(bufP, 32, "%ld.%06ld", (long)tv.tv_sec, (long)tv.tv_usec);
            bufP += setToIov(1, bufP, nSize);

            // 2. Add Sender IP and Port ("ip:port")
            char* send_addr = FCGX_GetParam("REMOTE_ADDR", request.envp);
            char* send_port = FCGX_GetParam("REMOTE_PORT", request.envp);
            if (!send_addr) send_addr = "127.0.0.1";
            if (!send_port) send_port = "0";
            nSize = snprintf(bufP, 32, "%s:%s", send_addr, send_port);
            bufP += setToIov(2, bufP, nSize);

            // 3. Add telemetry payload
            setToIov(3, dataBuf, nPostLen);

            // 4. Calculate total record size with dynamic nxt/prv digits
            // Base size = parsed wrapper strings + post body size + static fields template size
            int base_size = (bufP - &wrap_buf[0]) + nPostLen + getFieldsSize();
            int num_digits = snprintf(NULL, 0, "%d", base_size);
            int final_size = base_size + num_digits * 2;
            if (snprintf(NULL, 0, "%d", final_size) != num_digits) {
                num_digits = snprintf(NULL, 0, "%d", final_size);
                final_size = base_size + num_digits * 2;
            }
            // Add 1 extra byte for the trailing comma separator: "},"
            final_size += 1;

            // 5. Add 'nxt' size field (at index 0)
            nSize = snprintf(bufP, 16, "%d", final_size);
            setToIov(0, bufP, nSize);
            bufP += nSize;

            // 6. Add 'prv' size field + closing "}," (at index 4)
            nSize = snprintf(bufP, 16, "%d},", final_size);
            setToIov(4, bufP, nSize);

            // Ensure parent directory exists for the ingest file
            char dir_path[256];
            strncpy(dir_path, ingest_path, sizeof(dir_path) - 1);
            char* last_slash = strrchr(dir_path, '/');
            if (last_slash) {
                *last_slash = '\0';
                struct stat st = {0};
                if (stat(dir_path, &st) == -1) {
                    mkdir(dir_path, 0775);
                }
            }

            int ingestFd = get_stream_fd_lru(ingest_path);
            if (ingestFd < 0) {
                syslog(LOG_ERR, "FCGI: Could not open ingest file: %s", ingest_path);
                FCGX_PutS("Status: 500 Internal Server Error\r\n\r\n", request.out);
            } else {
                int written = writev(ingestFd, iov, IOV_COUNT);
                if (written < 0) {
                    syslog(LOG_ERR, "FCGI: Failed to write event to %s", ingest_path);
                    FCGX_PutS("Status: 500 Internal Server Error\r\n\r\n", request.out);
                    invalidate_stream_fd(ingestFd);
                } else if (written != final_size) {
                    syslog(LOG_WARNING, "FCGI: Size mismatch. Calculated %d, wrote %d", final_size, written);
                    FCGX_PutS("Status: 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\":\"warning\",\"detail\":\"size_mismatch\"}", request.out);
                } else {
                    FCGX_PutS("Status: 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\":\"success\"}", request.out);
                }
            }

            free(dataBuf);
        } else {
            if (nPostLen > POST_DATA_SIZE_LIMIT) {
                syslog(LOG_WARNING, "FCGI: POST length limit exceeded: %d", nPostLen);
                FCGX_PutS("Status: 413 Payload Too Large\r\n\r\n", request.out);
            } else {
                FCGX_PutS("Status: 405 Method Not Allowed\r\nAllow: POST\r\nContent-Type: application/json\r\n\r\n{\"error\":\"POST required\"}", request.out);
            }
        }

        FCGX_Finish_r(&request);
    }
    return 0;
}

int main(void) {
    openlog("sluicegate_ingest_fcgi", LOG_CONS | LOG_NDELAY, LOG_USER);
    syslog(LOG_INFO, "Starting sluicegate compiled C FastCGI Daemon...");

    int res = FCGX_Init();
    if (res) {
        syslog(LOG_ERR, "FCGX_Init failed: %d", res);
        return 1;
    }

    char* env_path = getenv("SLUICEGATE_INGEST_PATH");
    if (env_path && strlen(env_path) > 0) {
        ingest_path = env_path;
    }

    char* env_port = getenv("SLUICEGATE_PORT");
    char local_socket_path[256] = DEFAULT_SOCKET_PATH;
    if (env_port && strlen(env_port) > 0) {
        if (env_port[0] == '/' || strchr(env_port, ':') != NULL) {
            snprintf(local_socket_path, sizeof(local_socket_path), "%s", env_port);
        } else {
            snprintf(local_socket_path, sizeof(local_socket_path), ":%s", env_port);
        }
    }

    socketId = FCGX_OpenSocket(local_socket_path, 512);
    if (socketId < 0) {
        syslog(LOG_ERR, "FCGX_OpenSocket failed for %s", local_socket_path);
        return 1;
    }
    syslog(LOG_INFO, "FCGX listening socket successfully bound to %s", local_socket_path);

    umask(002);
    res = ingestLoop();
    return res;
}
