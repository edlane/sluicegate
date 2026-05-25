# ==============================================================================
# STAGE 1: Build the React Admin Portal Frontend
# ==============================================================================
FROM node:20-alpine AS frontend-builder
WORKDIR /app/admin

# Copy package descriptors and lockfiles
COPY admin/package*.json ./
RUN npm install

# Copy admin source code
COPY admin/ ./

# Compile/Build static production assets (produces /app/admin/dist)
RUN npm run build

# ==============================================================================
# STAGE 2: Build C Daemon, Ingest Core Engine, and Runtime Server
# ==============================================================================
FROM python:3.12-slim-bookworm AS runner
WORKDIR /app

# Install system dependencies for C compilation, libfcgi, and standard libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    make \
    libfcgi-dev \
    libfcgi0ldbl \
    && rm -rf /var/lib/apt/lists/*

# Copy workspace source files
COPY src/ ./src/
COPY Makefile ./

# Compile the high-performance C FastCGI Ingestion Daemon
RUN make

# Copy the compiled React static frontend bundle from Stage 1
COPY --from=frontend-builder /app/admin/dist ./admin/dist/

# Create default directories for topics stream storage files
RUN mkdir -p /app/streams

# Configure environment variables for runtime
ENV SLUICEGATE_PORT=2099
ENV SLUICEGATE_INGEST_PATH=/app/streams/ingest.json
ENV PYTHONUNBUFFERED=1

# Expose API Server port (8088) and FastCGI socket port (2099)
EXPOSE 8088
EXPOSE 2099

# Set entrypoint to unified REST + SSE Python server
CMD ["python3", "src/server.py"]
