# DisguiseBrowser runtime: real Chrome, a virtual desktop, and the library.
#
# Build:  docker build -t disguisebrowser .
# Check:  docker run --rm --shm-size=1g disguisebrowser doctor
# Score:  docker run --rm --shm-size=1g disguisebrowser test --display virtual
#
# Chrome's sandbox needs user namespaces. If it exits with "No usable sandbox",
# rerun with --security-opt seccomp=unconfined. Do not pass --no-sandbox.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8 \
    HOME=/home/disguise \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        locales \
        python3 \
        python3-venv \
        xvfb \
        x11-xkb-utils \
        xauth \
        libgl1-mesa-dri \
        mesa-utils \
        openbox \
        tint2 \
        fonts-liberation \
        fonts-dejavu-core \
        fonts-noto-core \
        fonts-noto-color-emoji \
        fonts-noto-cjk \
        fonts-unifont \
    && sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen en_US.UTF-8 \
    && curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -rf /tmp/chrome.deb /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

RUN python3 -m venv /opt/venv
WORKDIR /opt/disguisebrowser
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-cache-dir .

# Ubuntu 24.04 already has uid 1000 (user ubuntu). Any non-root uid keeps
# Chrome's stock sandbox; 1000 specifically is not required.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin disguise \
    && chown -R disguise:disguise /home/disguise
USER disguise
WORKDIR /home/disguise

ENTRYPOINT ["disguise"]
CMD ["doctor"]
