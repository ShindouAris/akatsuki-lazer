FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl https://sh.rustup.rs -sSf | sh -s -- -y

ENV PATH="/root/.cargo/bin:$PATH"

RUN pip install uv

WORKDIR /app
COPY . .

RUN uv sync --no-dev

EXPOSE 8000

ENTRYPOINT ["bash", "script_start.sh"]