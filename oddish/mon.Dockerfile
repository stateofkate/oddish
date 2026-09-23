# The image an oddish-mon sandbox starts from: Docker + the oddish package.
# The monitor is the entrypoint on the outer VM; the task runs in Docker beneath it.
FROM python:3.13-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git gnupg iptables \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
 && apt-get clean

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /opt/oddish
COPY pyproject.toml uv.lock README.md LICENSE LICENSE-APACHE-2.0 ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra server --extra worker
ENV PATH="/opt/oddish/.venv/bin:$PATH" ODDISH_MON_STATUS_PATH=/run/oddish-mon/status.json

COPY scripts/mon-entrypoint.sh /usr/local/bin/mon-entrypoint.sh
RUN chmod +x /usr/local/bin/mon-entrypoint.sh
# Keep Docker's image store in the sandbox filesystem. A separate Docker
# volume is not included when Daytona creates a filesystem snapshot.
# No ENTRYPOINT on purpose. Each provider's starter runs mon-entrypoint.sh
# itself, exactly once: providers differ in whether they run an image's
# entrypoint (and whether a snapshot boot does), and two monitors in one
# sandbox run the trial twice.
