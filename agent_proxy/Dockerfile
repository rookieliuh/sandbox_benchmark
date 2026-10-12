FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git pkg-config ca-certificates python3 \
    libssl-dev libgflags-dev libprotobuf-dev libprotoc-dev protobuf-compiler \
    libleveldb-dev zlib1g-dev libcurl4-openssl-dev nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace/agent-proxy
COPY . .
RUN bash scripts/build_and_test.sh
CMD ["ctest", "--test-dir", "build", "--output-on-failure"]
