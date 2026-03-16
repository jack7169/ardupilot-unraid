FROM ardupilot/ardupilot-dev-chibios:v0.1.5

USER root

# Install SITL and autotest dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-dev \
    python3-pip \
    procps \
    psmisc \
    xterm \
    sudo \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
    pymavlink \
    MAVProxy \
    numpy \
    pyparsing \
    psutil \
    geocoder \
    dronecan \
    wsproto \
    tabulate \
    fastapi \
    uvicorn \
    httpx

# Create user and directories
RUN groupadd -g 999 ardupilot 2>/dev/null || true && \
    useradd -u 999 -g 999 -m ardupilot --shell /bin/bash 2>/dev/null || true && \
    mkdir -p /app /results /workdir && \
    chown -R ardupilot:ardupilot /app /results /workdir && \
    echo 'ardupilot ALL=(root) NOPASSWD: /bin/mount, /bin/umount' > /etc/sudoers.d/ardupilot-overlay

COPY --chown=ardupilot:ardupilot app.py /app/app.py

WORKDIR /workdir
ENV PYTHONPATH=/workdir/ardupilot/Tools/autotest
ENV PATH="/workdir/ardupilot/Tools/autotest:${PATH}"

USER ardupilot
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8091", "--app-dir", "/app"]
