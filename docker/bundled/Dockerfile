FROM ardupilot/ardupilot-dev-chibios:v0.1.5

USER root

# Create ardupilot user before package installs (redis-server claims GID 999 otherwise)
RUN groupadd -g 999 ardupilot && \
    useradd -u 999 -g 999 -m ardupilot --shell /bin/bash

# Install system dependencies (redis, supervisor, process tools, SITL X11 deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-dev \
    python3-pip \
    procps \
    psmisc \
    xterm \
    redis-server \
    supervisor \
    gosu \
    sudo \
    curl \
    debian-keyring \
    debian-archive-keyring \
    apt-transport-https \
    && rm -rf /var/lib/apt/lists/*

# Install Caddy
RUN curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
    && curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list \
    && apt-get update \
    && apt-get install -y caddy \
    && rm -rf /var/lib/apt/lists/*

# Install all Python dependencies (union of all services)
# CustomBuild App: fastapi, uvicorn, pydantic, redis, requests, jsonschema, dill, packaging, jinja2, python-multipart, slowapi
# CustomBuild Builder: jsonschema, redis, dill
# Admin: fastapi, uvicorn, httpx, jinja2
# Autotest: pymavlink, MAVProxy, numpy, pyparsing, psutil, geocoder, dronecan, wsproto, tabulate, fastapi, uvicorn, httpx
RUN pip3 install --no-cache-dir --break-system-packages \
    fastapi==0.104.1 \
    uvicorn==0.40.0 \
    pydantic==2.5.0 \
    redis==5.2.1 \
    requests==2.31.0 \
    jsonschema==4.20.0 \
    dill==0.3.8 \
    packaging==25.0 \
    jinja2==3.1.2 \
    python-multipart==0.0.6 \
    slowapi==0.1.9 \
    httpx \
    pymavlink \
    MAVProxy \
    numpy \
    pyparsing \
    psutil \
    geocoder \
    dronecan \
    wsproto \
    tabulate \
    pyyaml

# Create application directories
RUN mkdir -p \
    /app/custombuild \
    /app/admin \
    /app/autotest \
    /workdir \
    /data/custombuild-base/configs \
    /data/autotest-workdir \
    /data/autotest-results \
    /data/buildlogs \
    /var/log/supervisor \
    /etc/ardupilot

# Copy config files (paths relative to build context = repo root)
COPY docker/bundled/config/supervisord.conf /etc/ardupilot/supervisord.conf
COPY docker/bundled/config/Caddyfile /etc/ardupilot/Caddyfile
COPY docker/bundled/config/config.yaml /etc/ardupilot/config.yaml
COPY docker/bundled/config/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Copy custombuild app + builder (from submodule)
COPY --chown=ardupilot:ardupilot custombuild/ /app/custombuild/

# Override custombuild templates with custom versions
COPY --chown=ardupilot:ardupilot docker/templates/ /app/custombuild/web/templates/

# Copy admin service (app.py expects /app/templates and /app/static)
COPY --chown=ardupilot:ardupilot docker/admin/app.py /app/admin/
COPY --chown=ardupilot:ardupilot docker/admin/templates/ /app/admin/templates/
COPY --chown=ardupilot:ardupilot docker/admin/static/ /app/admin/static/
# Admin app.py has hardcoded /app/templates and /app/static paths
RUN ln -s /app/admin/templates /app/templates && \
    ln -s /app/admin/static /app/static

# Copy autotest service
COPY --chown=ardupilot:ardupilot docker/autotest/app.py /app/autotest/

# Allow ardupilot user to run mount/umount for overlay fs (autotest worktrees)
RUN echo 'ardupilot ALL=(root) NOPASSWD: /bin/mount, /bin/umount' > /etc/sudoers.d/ardupilot-overlay

# Mark data dirs as safe for git (ownership may differ between host and container)
RUN git config --global --add safe.directory /data/autotest-workdir/ardupilot && \
    git config --global --add safe.directory /data/custombuild-base/ardupilot && \
    git config --global --add safe.directory /data/shared-ardupilot && \
    git config --global --add safe.directory /workdir/ardupilot
# Copy global gitconfig to ardupilot user too
RUN cp /root/.gitconfig /home/ardupilot/.gitconfig && chown ardupilot:ardupilot /home/ardupilot/.gitconfig

# Set ownership on data directories
RUN chown -R ardupilot:ardupilot /data /app /workdir /var/log/supervisor

EXPOSE 8000

VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["supervisord", "-c", "/etc/ardupilot/supervisord.conf"]
