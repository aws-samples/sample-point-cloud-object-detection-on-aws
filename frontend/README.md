# Point Cloud Detection — Frontend

React + Vite + TypeScript single-page app for uploading point clouds, tracking
detection jobs, and reviewing results in an interactive 3D viewer.

## Stack

- **React 18** + **TypeScript** + **Vite 5**
- **Tailwind CSS 3** for styling
- **React Router** for routing, **TanStack Query** for server state
- **Three.js** via **@react-three/fiber** + **@react-three/drei** for 3D rendering

## Quick start

```bash
npm ci   # installs exactly the versions package-lock.json pins

# Run the app against the local mock API (two processes: mock + Vite)
npm run dev:mock

# ...or run them separately
npm run mock-api   # mock backend on :8000
npm run dev        # Vite dev server on :5173
```

Open http://localhost:5173. Vite proxies `/api` → `http://localhost:8000`
(see `vite.config.ts`), so the app talks to the mock server (or the real
backend if you run one on that port).

## Scripts

| Script | Purpose |
|--------|---------|
| `npm run dev` | Vite dev server |
| `npm run mock-api` | Mock backend API (`mock-server.mjs`) |
| `npm run dev:mock` | Mock API + Vite together |
| `npm run build` | Type-check (`tsc`) + production build (`vite build`) |
| `npm run preview` | Serve the production build locally |
| `npm run lint` | ESLint (flat config, `eslint.config.js`) |
| `npm run lint:fix` | ESLint with autofix |

## Project layout

```
src/
  App.tsx              Router + layout (Viewer route is lazy-loaded)
  pages/
    Upload.tsx         Drag-drop upload + detection config
    Dashboard.tsx      Job list with 5s auto-refresh
    Viewer.tsx         3D detection-review viewer
  services/
    api.ts             Typed API client + response types
    upload.ts          Multipart upload with progress
  viewer/
    EDL.tsx            Eye-Dome Lighting post-process
    pointCloud.ts      Tile parsing + color-mode computation
    classColors.ts     Stable per-class color palette
```

## The viewer

A detection-review tool modeled on AV-annotation workflows:

- **Point color modes** — height, RGB, intensity, by-detection-class, and
  semantic segmentation
- **Eye-Dome Lighting (EDL)** — screen-space depth shading (adjustable strength)
- **Detection review** — class + confidence filter rail, inspector panel,
  click-to-focus
- **Camera presets** (iso / top / front / side) + bird's-eye (BEV) overlay
- **Keyboard nav** — `↑↓` cycle detections, `f` focus, `b` toggle BEV, `Esc` deselect

Performance: the Three.js stack is isolated in its own chunk and lazy-loaded only on
the `/viewer` route. As `npm run build` (Vite 8.3.5) reported when this was written, that
chunk is 852 kB (225 kB gzipped) and the initial script 306 kB (97 kB gzipped).

## API contract

The frontend expects these endpoints (all under `/api`). See `src/services/api.ts`
for exact request/response types. The mock server implements all of them.

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/upload/initiate` | Start multipart upload, get pre-signed part URLs |
| POST | `/upload/complete` | Finalize multipart upload |
| POST | `/jobs` | Create a detection job |
| GET | `/jobs` | List jobs |
| GET | `/jobs/{id}` | Get a job |
| GET | `/results/{id}/detections` | Detection list (boxes + labels) |
| GET | `/results/{id}/tiles/index` | Tile index + metadata |
| GET | `/results/{id}/tiles/{tileId}` | Point tile (binary) |
| GET | `/results/{id}/tiles/{tileId}/segmentation` | Per-point labels (binary) |

### Tile binary formats

Tile point data is interleaved `Float32`. The `tiles/index` response advertises
the layout via `point_format` (absent ⇒ `xyz`, backward compatible):

- `xyz` → `[x, y, z]` (stride 3)
- `xyzrgb` → `[x, y, z, r, g, b]` (stride 6), rgb in 0..1
- `xyzrgbi` → `[x, y, z, r, g, b, i]` (stride 7), intensity in 0..1

RGB and Intensity color modes are enabled only when the backend emits those
channels.

### Segmentation (optional)

When `tiles/index` returns `has_segmentation: true` with
`segmentation_labels: [{ id, name }]`, the viewer enables the **Segment** color
mode. The segmentation endpoint returns a raw `Uint16` array — one label id per
point, aligned index-for-index with that tile's point data.

## Mock server

`mock-server.mjs` (Express) implements the full API contract for local
development — seed jobs in various states, an upload → process simulation, and
tiles served as `xyzrgbi` with points clustered inside detection boxes plus
aligned segmentation labels. It's a dev tool only and is excluded from the
production image.

## Production build (Docker / Finch)

Multi-stage build (Node build → nginx serve):

```bash
finch build -t pointcloud-ui .   # or: docker build -t pointcloud-ui .
finch run -d -p 8080:80 pointcloud-ui
```

nginx (`nginx.conf`) serves the SPA with client-side-route fallback and sets
basic security headers. In Kubernetes, `/api` routing is handled by the Ingress;
for standalone Docker use, uncomment the `/api` proxy block in `nginx.conf`.
