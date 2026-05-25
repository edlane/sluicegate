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

            int ingestFd = open(ingest_path, O_WRONLY | O_CREAT | O_APPEND, 0664);
            if (ingestFd < 0) {
                syslog(LOG_ERR, "FCGI: Could not open ingest file: %s", ingest_path);
                FCGX_PutS("Status: 500 Internal Server Error\r\n\r\n", request.out);
            } else {
                int written = writev(ingestFd, iov, IOV_COUNT);
                if (written < 0) {
                    syslog(LOG_ERR, "FCGI: Failed to write event to %s", ingest_path);
                    FCGX_PutS("Status: 500 Internal Server Error\r\n\r\n", request.out);
                } else if (written != final_size) {
                    syslog(LOG_WARNING, "FCGI: Size mismatch. Calculated %d, wrote %d", final_size, written);
                    FCGX_PutS("Status: 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\":\"warning\",\"detail\":\"size_mismatch\"}", request.out);
                } else {
                    FCGX_PutS("Status: 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\":\"success\"}", request.out);
                }
                close(ingestFd);
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
