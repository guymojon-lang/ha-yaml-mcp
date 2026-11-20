ARG BUILD_FROM
FROM ${BUILD_FROM}
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY ha_yaml_mcp /app/ha_yaml_mcp
COPY run.py /app/run.py
CMD ["python", "/app/run.py"]
