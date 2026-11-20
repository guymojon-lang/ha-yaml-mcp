
ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

RUN apk add --no-cache python3 py3-pip py3-virtualenv

COPY requirements.txt /app/requirements.txt

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY ha_yaml_mcp /app/ha_yaml_mcp
COPY run.py /app/run.py

ENV PATH="/opt/venv/bin:$PATH"

CMD ["python", "/app/run.py"]
