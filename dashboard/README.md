# Operator dashboard (React + TypeScript)

The operator's view of the live system. It's built into the API image
(`docker/api.Dockerfile`) and served by FastAPI at http://localhost:8000.

## What's on the page

Everything refreshes every 1.5 s from `GET /api/overview`.

- **Header:** simulated clock, run status, scenario and seed, and how far through the day it is.
- **Totals:** passengers arrived, served, waiting, average and 95th-percentile wait, and how
  many were left behind by a full bus.
- **System health:** worker count, queue backlog (with a chart), dead letters, events per
  second, time since the last event was stored, and whether the simulator and scheduler are
  alive. When the worker stops, this turns red and the map marks bus positions as out of date.
- **Network:** each route as a loop of stops, with buses (coloured by how full they are) and
  the number of people waiting at each stop.
- **Routes and Buses tables:** interval, buses, demand label, waits and schedule version per
  route; position, occupancy and last report per bus.
- **Recommendations:** pending suggestions with Approve / Decline, warnings when no buses are
  free, and a history of decisions.

Not in scope (see the proposal): passenger trip planning, fares, passenger feedback.

## Development

```bash
npm install
npm run dev      # http://localhost:5173, proxies /api to http://localhost:8000
npm run build    # type-checks and writes dist/ (what the Docker image serves)
```

Run the backend with `docker compose up` (or `uvicorn transit.api.main:app` locally).
`src/api/types.ts` must match `transit/api/schemas.py`.
