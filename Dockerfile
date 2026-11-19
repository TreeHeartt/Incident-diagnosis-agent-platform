FROM registry-1.docker.io/library/node:22-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM registry-1.docker.io/library/python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.lock.txt ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir .
COPY --from=frontend /build/src/incident_simulator/web /usr/local/lib/python3.12/site-packages/incident_simulator/web
CMD ["uvicorn", "incident_simulator.api:app", "--host", "0.0.0.0", "--port", "8080"]
