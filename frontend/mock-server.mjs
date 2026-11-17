/**
 * Mock API server for frontend development.
 * Run: node mock-server.mjs
 * Serves on port 8000 (matches Vite proxy target).
 *
 * Tile data is served as interleaved Float32 XYZRGBI (stride 7):
 *   [x, y, z, r, g, b, intensity]  — rgb & intensity in 0..1
 * The tiles/index response advertises point_format: 'xyzrgbi'.
 * Detections are generated once per job and stored, and tile points are
 * clustered inside each detection box so class-coloring is meaningful.
 */
import express from 'express';
import cors from 'cors';
import crypto from 'crypto';

const app = express();
app.use(cors());
app.use(express.json());

const jobs = new Map();

const INDOOR_LABELS = ['chair', 'table', 'desk', 'sofa', 'cabinet', 'door', 'shelf', 'monitor'];
const OUTDOOR_LABELS = ['car', 'truck', 'pedestrian', 'cyclist', 'barrier', 'cone'];
const INDUSTRIAL_LABELS = ['column', 'pipe', 'duct', 'beam', 'pallet'];

function labelsForMode(mode) {
  if (mode === 'outdoor' || mode === 'kitti') return OUTDOOR_LABELS;
  if (mode === 'custom') return INDUSTRIAL_LABELS;
  return INDOOR_LABELS;
}

// Deterministic per-label color (hue), matches spirit of frontend palette.
function labelRGB(label) {
  let h = 0;
  for (let i = 0; i < label.length; i++) h = (h * 31 + label.charCodeAt(i)) | 0;
  const hue = (Math.abs(h) % 360) / 360;
  return hslToRgb(hue, 0.65, 0.55);
}

function hslToRgb(h, s, l) {
  let r, g, b;
  if (s === 0) {
    r = g = b = l;
  } else {
    const hue2rgb = (p, q, t) => {
      if (t < 0) t += 1;
      if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    };
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    r = hue2rgb(p, q, h + 1 / 3);
    g = hue2rgb(p, q, h);
    b = hue2rgb(p, q, h - 1 / 3);
  }
  return [r, g, b];
}

function generateDetections(mode, count) {
  const labels = labelsForMode(mode);
  return Array.from({ length: count }, () => {
    const label = labels[Math.floor(Math.random() * labels.length)];
    const cx = Math.random() * 24 - 12;
    const cy = Math.random() * 24 - 12;
    const sz = [Math.random() * 1.8 + 0.6, Math.random() * 1.8 + 0.6, Math.random() * 1.6 + 0.6];
    const cz = sz[2] / 2 + Math.random() * 0.3;
    return {
      id: crypto.randomUUID(),
      label,
      confidence: +(Math.random() * 0.5 + 0.5).toFixed(3),
      bbox_obb: { center: [cx, cy, cz], size: sz, heading: Math.random() * Math.PI * 2 },
      bbox_aabb: {
        min: [cx - sz[0] / 2, cy - sz[1] / 2, cz - sz[2] / 2],
        max: [cx + sz[0] / 2, cy + sz[1] / 2, cz + sz[2] / 2],
      },
    };
  });
}

// Build a scene: interleaved XYZRGBI points (gray floor + colored cluster per
// detection) plus an aligned Uint16 per-point segmentation label array.
// Floor points are segment 0; each detection class maps to its own segment id.
function buildScene(detections) {
  const pts = [];
  const segs = [];
  const labelToId = new Map([['floor', 0]]);
  const labels = [{ id: 0, name: 'floor' }];
  const idFor = (name) => {
    if (!labelToId.has(name)) {
      const id = labels.length;
      labelToId.set(name, id);
      labels.push({ id, name });
    }
    return labelToId.get(name);
  };

  // Floor
  const floorCount = 4000;
  for (let i = 0; i < floorCount; i++) {
    const x = Math.random() * 30 - 15;
    const y = Math.random() * 30 - 15;
    const z = Math.random() * 0.08;
    const shade = 0.32 + Math.random() * 0.08;
    pts.push(x, y, z, shade, shade, shade * 1.05, 0.15 + Math.random() * 0.1);
    segs.push(0);
  }

  // Clusters inside each detection box
  for (const det of detections) {
    const { center, size, heading } = det.bbox_obb;
    const [r, g, b] = labelRGB(det.label);
    const sid = idFor(det.label);
    const n = 220;
    const cos = Math.cos(heading);
    const sin = Math.sin(heading);
    for (let i = 0; i < n; i++) {
      const lx = (Math.random() - 0.5) * size[0];
      const ly = (Math.random() - 0.5) * size[1];
      const lz = (Math.random() - 0.5) * size[2];
      const x = center[0] + lx * cos - ly * sin;
      const y = center[1] + lx * sin + ly * cos;
      const z = center[2] + lz;
      const jitter = 0.85 + Math.random() * 0.3;
      pts.push(x, y, z, r * jitter, g * jitter, b * jitter, 0.5 + Math.random() * 0.5);
      segs.push(sid);
    }
  }

  return { points: new Float32Array(pts), segments: new Uint16Array(segs), labels };
}

// Build + cache the scene on the job so tile points and segmentation stay aligned.
function getScene(job) {
  if (!job.detections) attachScene(job);
  if (!job.scene) job.scene = buildScene(job.detections);
  return job.scene;
}

function attachScene(job) {
  const count = job.detection_count ?? Math.floor(Math.random() * 30) + 10;
  job.detections = generateDetections(job.config?.model_mode || 'indoor', count);
  job.detection_count = job.detections.length;
  job.scene = null; // rebuilt lazily via getScene
}

// --- Seed jobs ---
const seedDefs = [
  { filename: 'warehouse_scan_001.e57', mode: 'indoor', status: 'complete', detections: 47, time: 28.4, ageMs: 3600_000 },
  { filename: 'parking_garage_floor2.las', mode: 'outdoor', status: 'complete', detections: 23, time: 15.7, ageMs: 7200_000 },
  { filename: 'construction_site_lidar.laz', mode: 'outdoor', status: 'processing', ageMs: 120_000 },
  { filename: 'office_scan_corrupted.ply', mode: 'indoor', status: 'failed', ageMs: 10800_000, error: 'Invalid point cloud format: header checksum mismatch' },
  { filename: 'factory_floor_kitti.bin', mode: 'kitti', status: 'queued', ageMs: 30_000 },
];

for (const def of seedDefs) {
  const job = {
    job_id: crypto.randomUUID(),
    filename: def.filename,
    s3_input_key: `uploads/${def.filename}`,
    config: { model_mode: def.mode, confidence_threshold: 0.5, use_color: true, use_intensity: def.mode !== 'indoor', use_normals: false, coord_frame: 'z-up', downsample_voxel_m: 0.02, max_points: null, custom_weights_version: null, raw_mode: false },
    status: def.status,
    created_at: new Date(Date.now() - def.ageMs).toISOString(),
    started_at: def.status === 'queued' ? null : new Date(Date.now() - def.ageMs + 60_000).toISOString(),
    completed_at: def.status === 'complete' || def.status === 'failed' ? new Date(Date.now() - def.ageMs + 300_000).toISOString() : null,
    detection_count: def.detections ?? null,
    tile_count: def.status === 'complete' ? 12 : null,
    processing_time_s: def.time ?? null,
    error_message: def.error ?? null,
    detections: null,
  };
  if (def.status === 'complete') attachScene(job);
  jobs.set(job.job_id, job);
}

// --- Upload APIs ---
app.post('/api/upload/initiate', (req, res) => {
  const { filename, file_size_bytes } = req.body;
  const partSize = 5 * 1024 * 1024;
  const totalParts = Math.max(1, Math.ceil(file_size_bytes / partSize));
  const uploadId = crypto.randomUUID();
  const s3Key = `uploads/${Date.now()}-${filename}`;
  const partUrls = Array.from({ length: totalParts }, (_, i) => ({
    part_number: i + 1,
    presigned_url: `http://localhost:8000/api/mock-s3-upload/${uploadId}/${i + 1}`,
  }));
  res.json({ upload_id: uploadId, s3_key: s3Key, part_size_bytes: partSize, total_parts: totalParts, part_urls: partUrls });
});

app.put('/api/mock-s3-upload/:uploadId/:partNumber', (req, res) => {
  const etag = crypto.randomUUID().replace(/-/g, '').slice(0, 32);
  res.set('ETag', `"${etag}"`);
  req.on('data', () => {});
  req.on('end', () => res.status(200).send(''));
});

app.post('/api/upload/complete', (_req, res) => res.json({ status: 'complete', message: 'Upload finalized' }));

// --- Job APIs ---
app.post('/api/jobs', (req, res) => {
  const { s3_key, filename, file_size_bytes, config } = req.body;
  const job = {
    job_id: crypto.randomUUID(),
    filename,
    s3_input_key: s3_key,
    config,
    status: 'queued',
    created_at: new Date().toISOString(),
    started_at: null,
    completed_at: null,
    detection_count: null,
    tile_count: null,
    processing_time_s: null,
    error_message: null,
    detections: null,
  };
  jobs.set(job.job_id, job);

  setTimeout(() => {
    const j = jobs.get(job.job_id);
    if (j) { j.status = 'processing'; j.started_at = new Date().toISOString(); }
  }, 3000);

  setTimeout(() => {
    const j = jobs.get(job.job_id);
    if (j && j.status === 'processing') {
      j.status = 'complete';
      j.completed_at = new Date().toISOString();
      j.tile_count = Math.floor(Math.random() * 12) + 4;
      j.processing_time_s = +(Math.random() * 40 + 5).toFixed(1);
      attachScene(j);
    }
  }, 10000);

  res.status(201).json(job);
});

app.get('/api/jobs', (_req, res) => {
  const all = Array.from(jobs.values())
    .map(({ detections, ...rest }) => rest)
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  res.json({ jobs: all, total: all.length });
});

app.get('/api/jobs/:jobId', (req, res) => {
  const job = jobs.get(req.params.jobId);
  if (!job) return res.status(404).json({ error: 'Job not found' });
  const { detections, ...rest } = job;
  res.json(rest);
});

// Delete a finished job. Mirrors the real API (T60): refused while queued or
// processing, and the saved annotations go with it.
app.delete('/api/jobs/:jobId', (req, res) => {
  const job = jobs.get(req.params.jobId);
  if (!job) return res.status(404).json({ detail: 'Job not found' });
  if (job.status === 'queued' || job.status === 'processing') {
    return res.status(409).json({
      detail: `Job is ${job.status}. Delete it once it has completed or failed.`,
    });
  }
  jobs.delete(req.params.jobId);
  annotationsByJob.delete(req.params.jobId);
  res.json({ status: 'deleted', job_id: req.params.jobId, deleted_objects: 0 });
});

// --- Results APIs ---
app.get('/api/results/:jobId/detections', (req, res) => {
  const job = jobs.get(req.params.jobId);
  if (!job) return res.status(404).json({ error: 'Job not found' });
  if (job.status !== 'complete') return res.status(400).json({ error: 'Job not complete' });
  if (!job.detections) attachScene(job);
  res.json({ job_id: job.job_id, detections: job.detections, total: job.detections.length });
});

app.get('/api/results/:jobId/tiles/index', (req, res) => {
  const job = jobs.get(req.params.jobId);
  if (!job) return res.status(404).json({ error: 'Job not found' });
  const scene = getScene(job);

  const clusterPoints = job.detections.length * 220;
  const totalPoints = 4000 + clusterPoints;
  const tileCount = job.tile_count || 8;

  const tiles = [
    { tile_id: 'overview', level: 0, bounds_min: [-15, -15, -1], bounds_max: [15, 15, 8], point_count: totalPoints },
    ...Array.from({ length: tileCount - 1 }, (_, i) => ({
      tile_id: `tile_L1_${i}`,
      level: 1,
      bounds_min: [(i % 4) * 7.5 - 15, Math.floor(i / 4) * 7.5 - 15, -1],
      bounds_max: [(i % 4 + 1) * 7.5 - 15, Math.floor(i / 4 + 1) * 7.5 - 15, 8],
      point_count: Math.floor(totalPoints / tileCount),
    })),
  ];

  res.json({
    job_id: job.job_id,
    total_points: totalPoints,
    bounds_min: [-15, -15, -1],
    bounds_max: [15, 15, 8],
    levels: 2,
    point_format: 'xyzrgbi',
    has_segmentation: true,
    segmentation_labels: scene.labels,
    tiles,
  });
});

// Per-point segmentation labels (Uint16), aligned index-for-index with tile points.
// Defined before the generic tile route so it isn't shadowed.
app.get('/api/results/:jobId/tiles/:tileId/segmentation', (req, res) => {
  const job = jobs.get(req.params.jobId);
  if (!job) return res.status(404).json({ error: 'Job not found' });
  const scene = getScene(job);
  res.set('Content-Type', 'application/octet-stream');
  res.send(Buffer.from(scene.segments.buffer));
});

app.get('/api/results/:jobId/tiles/:tileId', (req, res) => {
  const job = jobs.get(req.params.jobId);
  if (!job) return res.status(404).json({ error: 'Job not found' });
  const scene = getScene(job);
  res.set('Content-Type', 'application/octet-stream');
  res.send(Buffer.from(scene.points.buffer));
});

// --- Annotation + training APIs (annotation tool integration) ---

// Default box sizes per label (mock has color via labelRGB but no sizes).
const DEFAULT_SIZES = {
  door: [1.0, 0.2, 2.1],
  window: [1.2, 0.2, 1.2],
  wall: [3.0, 0.3, 2.8],
  column: [0.6, 0.6, 3.0],
};

function rgbToHex([r, g, b]) {
  const h = (v) => Math.max(0, Math.min(255, Math.round(v * 255))).toString(16).padStart(2, '0');
  return `#${h(r)}${h(g)}${h(b)}`;
}

// Annotation sets keyed by job id. Latest full set per job (UI always sends the full set).
const annotationsByJob = new Map();
// Training jobs keyed by training job id.
const trainingJobs = new Map();

// Seed one finished training job so the dashboard shows history on first load.
{
  const src = Array.from(jobs.values()).find((j) => j.status === 'complete');
  const tid = crypto.randomUUID();
  const t0 = Date.now() - 5400_000;
  trainingJobs.set(tid, {
    training_job_id: tid,
    status: 'complete',
    model_artifact: 'models/votenet_scannet/finetuned-seed0001.pth',
    error_message: null,
    job_id: src?.job_id ?? null,
    source_filename: src?.filename ?? 'warehouse_scan_001.e57',
    base_model: 'votenet_scannet',
    epochs: 30,
    custom_classes: ['door'],
    created_at: new Date(t0).toISOString(),
    started_at: new Date(t0 + 60_000).toISOString(),
    completed_at: new Date(t0 + 1800_000).toISOString(),
  });
}

app.get('/api/labels', (req, res) => {
  const job = req.query.job_id ? jobs.get(req.query.job_id) : null;
  const names = job ? labelsForMode(job.config?.model_mode || 'indoor') : Object.keys(DEFAULT_SIZES);
  const labels = names.map((name) => ({
    name,
    color: rgbToHex(labelRGB(name)),
    default_size: DEFAULT_SIZES[name] || [1.0, 1.0, 1.0],
  }));
  res.json({ labels });
});

app.get('/api/annotations/:jobId', (req, res) => {
  const set = annotationsByJob.get(req.params.jobId);
  if (!set) return res.status(404).json({ error: 'No annotations for this job' });
  res.json(set);
});

app.post('/api/annotations/:jobId', (req, res) => {
  const set = req.body || {};
  const anns = Array.isArray(set.annotations) ? set.annotations : null;
  if (!anns) return res.status(400).json({ error: 'annotations[] required' });
  for (const a of anns) {
    const okArr = (v, n) => Array.isArray(v) && v.length === n && v.every((x) => typeof x === 'number');
    if (!okArr(a.center_xyz, 3) || !okArr(a.size_wlh, 3) || typeof a.heading !== 'number' || !a.class_name) {
      return res.status(400).json({ error: 'invalid annotation shape' });
    }
  }
  const stored = {
    scene_id: set.scene_id || req.params.jobId,
    point_cloud_path: set.point_cloud_path || '',
    num_annotations: anns.length,
    annotations: anns,
  };
  annotationsByJob.set(req.params.jobId, stored);
  res.status(201).json({ status: 'saved', num_annotations: anns.length });
});

app.post('/api/training', (req, res) => {
  const { job_id, base_model = 'votenet_scannet', epochs = 30, custom_classes = [] } = req.body || {};
  const srcJob = job_id ? jobs.get(job_id) : null;
  const id = crypto.randomUUID();
  const tj = {
    training_job_id: id,
    status: 'queued',
    model_artifact: undefined,
    error_message: null,
    job_id,
    source_filename: srcJob?.filename ?? null,
    base_model,
    epochs,
    custom_classes,
    created_at: new Date().toISOString(),
    started_at: null,
    completed_at: null,
  };
  trainingJobs.set(id, tj);

  setTimeout(() => {
    const t = trainingJobs.get(id);
    if (t) {
      t.status = 'processing';
      t.started_at = new Date().toISOString();
    }
  }, 3000);
  setTimeout(() => {
    const t = trainingJobs.get(id);
    if (t && t.status === 'processing') {
      t.status = 'complete';
      t.completed_at = new Date().toISOString();
      t.model_artifact = `models/${base_model}/finetuned-${id.slice(0, 8)}.pth`;
    }
  }, 10000);

  res.status(201).json({ training_job_id: id, status: 'queued' });
});

// Full training-job view used by both the list and detail endpoints.
function trainingView(t) {
  return {
    training_job_id: t.training_job_id,
    status: t.status,
    job_id: t.job_id,
    source_filename: t.source_filename,
    base_model: t.base_model,
    epochs: t.epochs,
    model_artifact: t.model_artifact,
    error_message: t.error_message,
    created_at: t.created_at,
    started_at: t.started_at,
    completed_at: t.completed_at,
  };
}

app.get('/api/training', (_req, res) => {
  const all = Array.from(trainingJobs.values())
    .map(trainingView)
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  res.json({ training_jobs: all, total: all.length });
});

app.get('/api/training/:id', (req, res) => {
  const t = trainingJobs.get(req.params.id);
  if (!t) return res.status(404).json({ error: 'Training job not found' });
  res.json(trainingView(t));
});

// Delete a training job. Mirrors the real API: the fine-tuned model it produced
// is derived from the job, so deleting the job also drops it from /api/models.
app.delete('/api/training/:id', (req, res) => {
  const t = trainingJobs.get(req.params.id);
  if (!t) return res.status(404).json({ error: 'Training job not found' });
  trainingJobs.delete(req.params.id);
  res.json({
    status: 'deleted',
    training_job_id: req.params.id,
    unregistered_models: t.model_artifact ? [`${t.base_model}_ft_${req.params.id.slice(0, 8)}`] : [],
  });
});

// --- Model registry ---
// Pretrained baselines, ordered to match the real registry.
const PRETRAINED_MODELS = [
  { model_id: 'fcaf3d_scannet', name: 'FCAF3D (Indoor / ScanNet)', mode: 'indoor', classes: ['door', 'window', 'chair', 'table'] },
  { model_id: 'votenet_scannet', name: 'VoteNet (Indoor / ScanNet)', mode: 'indoor', classes: ['door', 'window', 'chair', 'table'] },
  { model_id: 'centerpoint_nuscenes', name: 'CenterPoint (Outdoor / nuScenes)', mode: 'outdoor', classes: ['car', 'truck', 'pedestrian'] },
  { model_id: 'pointpillars_kitti', name: 'PointPillars (Outdoor / KITTI)', mode: 'kitti', classes: ['car', 'pedestrian', 'cyclist'] },
].map((m) => ({
  ...m,
  type: 'pretrained',
  base_model: m.model_id,
  checkpoint_path: 'pretrained',
  description: `${m.classes.length}-class ${m.mode} detection`,
  created_at: '2024-01-01T00:00:00Z',
  training_job_id: null,
}));

// Fine-tuned models are derived from completed training jobs, so deleting a
// training job removes its model without any extra bookkeeping.
app.get('/api/models', (_req, res) => {
  const finetuned = Array.from(trainingJobs.values())
    .filter((t) => t.status === 'complete' && t.model_artifact)
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    .map((t) => ({
      model_id: `${t.base_model}_ft_${t.training_job_id.slice(0, 8)}`,
      name: `${t.base_model} (fine-tuned ${t.training_job_id.slice(0, 8)})`,
      type: 'finetuned',
      base_model: t.base_model,
      mode: 'custom',
      checkpoint_path: t.model_artifact,
      classes: t.custom_classes ?? [],
      description: `Fine-tuned from ${t.base_model}`,
      created_at: t.created_at,
      training_job_id: t.training_job_id,
    }));
  res.json({ models: [...PRETRAINED_MODELS, ...finetuned] });
});

const PORT = 8000;
app.listen(PORT, () => {
  console.log(`\n🟢 Mock API server running at http://localhost:${PORT}`);
  console.log(`   ${jobs.size} seed jobs loaded`);
  console.log(`   Tiles served as XYZRGBI (rgb + intensity) with clustered detections`);
  console.log(`\n   Start frontend: npm run dev\n`);
});
