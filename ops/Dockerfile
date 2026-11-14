# docker build -f ops/Dockerfile -t rtrec .
# docker run -d --restart=always --env-file .env -v $PWD/artifacts:/app/artifacts rtrec            # primary
# docker run -d --restart=always --env-file .env -e RTREC_ROLE=standby -v ... rtrec               # standby
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY rtrec ./rtrec
COPY sim ./sim
COPY ops ./ops
RUN pip install --no-cache-dir -e .
ENV RTREC_STORE=rawtree RTREC_ROLE=primary RTREC_STATE_DIR=/app/artifacts/runtime PYTHONUNBUFFERED=1
CMD ["sh", "-c", "python -m rtrec.runtime.supervisor --status-file $RTREC_STATE_DIR/status.json -- python -m rtrec.runtime.serve --role $RTREC_ROLE --state-dir $RTREC_STATE_DIR"]
