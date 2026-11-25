# API + dashboard image: builds the React app, then serves it from FastAPI.
# Stage 1 needs dashboard/package.json (and a `build` script that outputs to dist/).

FROM node:20-alpine AS dashboard
WORKDIR /dashboard
COPY dashboard/package*.json ./
RUN npm ci
COPY dashboard ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRANSIT_CONFIG_DIR=/app/config \
    DASHBOARD_DIST=/app/dashboard_dist
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY config ./config
COPY transit ./transit
COPY --from=dashboard /dashboard/dist ./dashboard_dist

EXPOSE 8000
CMD ["uvicorn", "transit.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
