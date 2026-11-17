/**
 * API client for the Point Cloud Detection backend.
 *
 * Every id interpolated into a path goes through encodeURIComponent (seg below).
 * Ids come from the route (the viewer's :jobId) or from server responses, and an
 * unencoded "../" or "?" in one sent the request to a different /api route with
 * the user's session (T56).
 */
import axios from 'axios';

import { api } from './http';

/**
 * One path segment, encoded so it cannot add segments or a query.
 *
 * "." and ".." are refused rather than encoded: encodeURIComponent leaves dots
 * alone, and the URL parser treats "%2e%2e" as ".." too, so no encoding stops them
 * climbing a level. Exported for tests.
 */
export function seg(value: string): string {
  if (value === '' || value === '.' || value === '..') {
    throw new Error(`Not a valid id: ${JSON.stringify(value)}`);
  }
  return encodeURIComponent(value);
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * True for a UUID, the form of every job and training-job id. Checked before a
 * server-returned id is used as a navigation target: a training job's job_id was
 * any string a holder chose, and navigate() moved another user's tab to it (T56).
 */
export function isUuid(value: unknown): value is string {
  return typeof value === 'string' && UUID_RE.test(value);
}

// --- Model Registry ---

export interface RegisteredModel {
  model_id: string;
  name: string;
  type: 'pretrained' | 'finetuned';
  base_model: string;
  mode: string;
  checkpoint_path: string;
  classes: string[];
  description: string;
  created_at: string;
  training_job_id: string | null;
  /**
   * Whether detection can run this model. A pretrained model is unavailable unless
   * the deployment sets ALLOW_NONCOMMERCIAL_WEIGHTS, since its weights were trained
   * on non-commercial data and are never downloaded otherwise. Absent means
   * available, for registries that predate the field.
   */
  available?: boolean;
  /** Set on pretrained models: their training data's terms limit commercial use. */
  noncommercial?: boolean;
}

/** Whether detection can run ``m`` (see RegisteredModel.available). */
export function isAvailable(m: RegisteredModel): boolean {
  return m.available !== false;
}

/** List all available models (pretrained + fine-tuned). */
export async function listModels(): Promise<{ models: RegisteredModel[] }> {
  try {
    const { data } = await api.get('/models');
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) {
      return { models: [] };
    }
    throw err;
  }
}

export interface JobConfig {
  model_mode: string;
  confidence_threshold: number;
  use_color: boolean;
  use_intensity: boolean;
  use_normals: boolean;
  coord_frame: string;
  downsample_voxel_m: number;
  max_points: number | null;
  custom_weights_version: string | null;
  detection_config?: string | null;
  raw_mode: boolean;
  device_preference: 'auto' | 'cpu' | 'gpu';
  // Backend defaults this to 'detection' and always returns it. Optional here so
  // the partial configs callers build stay assignable.
  job_type?: 'detection' | 'annotation';
}

export interface Job {
  job_id: string;
  filename: string;
  s3_input_key: string;
  config: JobConfig;
  status: 'queued' | 'processing' | 'complete' | 'failed';
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  detection_count: number | null;
  tile_count: number | null;
  processing_time_s: number | null;
  error_message: string | null;
}

export interface UploadInitResponse {
  upload_id: string;
  s3_key: string;
  part_size_bytes: number;
  total_parts: number;
  part_urls: { part_number: number; presigned_url: string }[];
}

export type PointFormat = 'xyz' | 'xyzrgb' | 'xyzrgbi';

export interface TileIndex {
  job_id: string;
  total_points: number;
  bounds_min: number[];
  bounds_max: number[];
  levels: number;
  // Binary layout of tile data. Backward compatible: absent => 'xyz'.
  // Coordinate with the backend — enables RGB / intensity color modes.
  point_format?: PointFormat;
  // Optional per-point semantic segmentation. When true, the segmentation
  // endpoint returns a Uint16 label per point (aligned to tile point order).
  has_segmentation?: boolean;
  segmentation_labels?: { id: number; name: string }[];
  tiles: { tile_id: string; level: number; bounds_min: number[]; bounds_max: number[]; point_count: number }[];
}

// Upload APIs
export async function initiateUpload(filename: string, fileSize: number): Promise<UploadInitResponse> {
  const { data } = await api.post('/upload/initiate', {
    filename,
    file_size_bytes: fileSize,
    content_type: 'application/octet-stream',
  });
  return data;
}

export async function completeUpload(uploadId: string, s3Key: string, parts: { part_number: number; etag: string }[]) {
  const { data } = await api.post('/upload/complete', { upload_id: uploadId, s3_key: s3Key, parts });
  return data;
}

/**
 * Abort an incomplete multipart upload, releasing the parts already stored.
 *
 * The backend has exposed DELETE /upload/{upload_id} since before this client existed,
 * but nothing called it, so a failed or cancelled upload left its parts in the bucket
 * accruing storage charges until a lifecycle rule swept them.
 */
export async function abortUpload(uploadId: string, s3Key: string) {
  const { data } = await api.delete(`/upload/${seg(uploadId)}`, {
    params: { s3_key: s3Key },
  });
  return data;
}

// Job APIs
export async function createJob(s3Key: string, filename: string, fileSize: number, config: Partial<JobConfig>): Promise<Job> {
  const { data } = await api.post('/jobs', {
    s3_key: s3Key,
    filename,
    file_size_bytes: fileSize,
    config: { model_mode: 'indoor', confidence_threshold: 0.5, use_color: true, use_intensity: false, use_normals: false, coord_frame: 'z-up', downsample_voxel_m: 0.02, max_points: null, custom_weights_version: null, detection_config: null, raw_mode: false, device_preference: 'auto', job_type: 'detection', ...config },
  });
  return data;
}

export async function listJobs(status?: string): Promise<{ jobs: Job[]; total: number }> {
  const { data } = await api.get('/jobs', { params: { status } });
  return data;
}

export async function getJob(jobId: string): Promise<Job> {
  const { data } = await api.get(`/jobs/${seg(jobId)}`);
  return data;
}

/**
 * Delete a job and its stored data: the uploaded scan, results, tiles, saved
 * annotations and the record. The API refuses (409) while the job, or a retry of
 * the same upload, is queued or processing.
 */
export async function deleteJob(jobId: string): Promise<{ deleted_objects: number }> {
  const { data } = await api.delete(`/jobs/${seg(jobId)}`);
  return data;
}

/**
 * The API's error message for a failed request (FastAPI's string "detail"), or
 * `fallback` when there is none, e.g. a network error or a validation list.
 */
export function apiErrorDetail(err: unknown, fallback: string): string {
  if (axios.isAxiosError(err)) {
    const detail = (err.response?.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === 'string' && detail) return detail;
  }
  return fallback;
}

/** Create a training session and add annotation jobs to it (training-mode upload). */
export async function createTrainingSession(req: {
  name: string;
  base_model: string;
  coord_frame: string;
  use_color: boolean;
  use_intensity: boolean;
  classes: string[];
}): Promise<{ session_id: string }> {
  const { data } = await api.post('/training-sessions', req);
  return data;
}

export async function addJobToSession(sessionId: string, jobId: string): Promise<void> {
  await api.post(`/training-sessions/${seg(sessionId)}/jobs`, { job_id: jobId });
}

// Results APIs
export async function getDetections(jobId: string) {
  const { data } = await api.get(`/results/${seg(jobId)}/detections`);
  return data;
}

export async function getTileIndex(jobId: string): Promise<TileIndex> {
  const { data } = await api.get(`/results/${seg(jobId)}/tiles/index`);
  return data;
}

export async function getTileData(jobId: string, tileId: string): Promise<ArrayBuffer> {
  const { data } = await api.get(`/results/${seg(jobId)}/tiles/${seg(tileId)}`, { responseType: 'arraybuffer' });
  return data;
}

// Per-point segmentation labels (Uint16), aligned index-for-index with tile points.
export async function getSegmentation(jobId: string, tileId: string): Promise<ArrayBuffer> {
  const { data } = await api.get(`/results/${seg(jobId)}/tiles/${seg(tileId)}/segmentation`, {
    responseType: 'arraybuffer',
  });
  return data;
}

// ============================================================================
// Annotation + training APIs (annotation tool integration)
//
// Contract implemented in `mock-server.mjs` for local dev; real backend
// endpoints are pending on the backend. The annotation wire format matches the
// unified training format consumed by `training/data_loader.py`.
// ============================================================================

/** A selectable class label served by the backend. */
export interface LabelDef {
  name: string;
  color: string; // hex, e.g. "#00cc66"
  default_size: [number, number, number]; // w, l, h
}

/** One annotation in unified training format. */
export interface AnnotationDTO {
  center_xyz: [number, number, number];
  size_wlh: [number, number, number];
  heading: number; // radians
  class_name: string;
}

/** All annotations for one point cloud (unified training format). */
export interface AnnotationSet {
  scene_id: string;
  point_cloud_path: string;
  num_annotations: number;
  annotations: AnnotationDTO[];
}

export interface TrainingRequest {
  job_id: string; // one point cloud per job (MVP)
  base_model: string;
  epochs: number;
  custom_classes: string[];
}

export interface TrainingJob {
  training_job_id: string;
  status: 'queued' | 'processing' | 'complete' | 'failed';
  model_artifact?: string;
  error_message?: string | null;
  job_id?: string | null;
  source_filename?: string | null;
  base_model?: string;
  epochs?: number;
  // The backend stores and returns this; without it a re-run cannot reproduce
  // the original run's class list and silently produces a 0-class model.
  custom_classes?: string[];
  created_at?: string;
  started_at?: string | null;
  completed_at?: string | null;
}

/** Available labels; optionally seeded from a job's label set. */
export async function getLabels(jobId?: string): Promise<{ labels: LabelDef[] }> {
  try {
    const { data } = await api.get('/labels', { params: jobId ? { job_id: jobId } : undefined });
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) {
      return { labels: [] };
    }
    throw err;
  }
}

/** Existing annotation set for a job, or null if none saved yet (404). */
export async function getAnnotations(jobId: string): Promise<AnnotationSet | null> {
  try {
    const { data } = await api.get(`/annotations/${seg(jobId)}`);
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return null;
    throw err;
  }
}

/** Persist the full annotation set for a job. */
export async function saveAnnotations(jobId: string, set: AnnotationSet): Promise<void> {
  await api.post(`/annotations/${seg(jobId)}`, set);
}

/** Submit a fine-tuning job; returns the training job id + initial status. */
export async function submitTraining(req: TrainingRequest): Promise<TrainingJob> {
  const { data } = await api.post('/training', req);
  return data;
}

/** Poll a training job's status. */
export async function getTrainingJob(id: string): Promise<TrainingJob> {
  const { data } = await api.get(`/training/${seg(id)}`);
  return data;
}

/** List all fine-tuning jobs (newest first) for the dashboard. */
export async function listTrainingJobs(): Promise<{ training_jobs: TrainingJob[]; total: number }> {
  try {
    const { data } = await api.get('/training');
    return data;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) {
      return { training_jobs: [], total: 0 };
    }
    throw err;
  }
}

/**
 * Delete a fine-tuning job. Also unregisters the fine-tuned model it produced,
 * so a deleted job's model stops appearing in the detection model dropdown.
 */
export async function deleteTrainingJob(id: string): Promise<void> {
  await api.delete(`/training/${seg(id)}`);
}

/** Delete several fine-tuning jobs. Returns the ids actually deleted. */
export async function deleteTrainingJobs(ids: string[]): Promise<{ deleted: string[]; failed: string[] }> {
  const deleted: string[] = [];
  const failed: string[] = [];
  for (const id of ids) {
    try {
      await deleteTrainingJob(id);
      deleted.push(id);
    } catch {
      failed.push(id);
    }
  }
  return { deleted, failed };
}

/**
 * Re-run fine-tuning for the given source jobs.
 *
 * Submitted one at a time so a failure is attributable to a specific job, but
 * note the backend spawns a thread per request, so the runs themselves execute
 * concurrently — submission order only determines `created_at`, not completion
 * order. Do not rely on this to sequence runs.
 *
 * `skipped` and `failed` are kept apart deliberately: a job with no source
 * detection job can never be re-run, whereas a request that errored is worth
 * retrying, and reporting both as the former misleads whoever is diagnosing a
 * batch that did not run.
 */
export async function rerunTrainingJobs(
  jobs: TrainingJob[],
): Promise<{
  submitted: TrainingJob[];
  skipped: TrainingJob[];
  failed: TrainingJob[];
}> {
  const submitted: TrainingJob[] = [];
  const skipped: TrainingJob[] = [];
  const failed: TrainingJob[] = [];
  for (const t of jobs) {
    // A re-run needs the originating detection job to pull annotations from.
    if (!t.job_id) {
      skipped.push(t);
      continue;
    }
    try {
      const fresh = await submitTraining({
        job_id: t.job_id,
        base_model: t.base_model ?? 'votenet_scannet',
        epochs: t.epochs ?? 30,
        // Carry the original class list. Sending [] produced a 0-class model
        // that was not a reproduction of the run being re-run.
        custom_classes: t.custom_classes ?? [],
      });
      submitted.push(fresh);
    } catch {
      failed.push(t);
    }
  }
  return { submitted, skipped, failed };
}

/** The pretrained model selected by default, for detection and for fine-tuning. */
export const DEFAULT_MODEL_ID = 'votenet_scannet';

/**
 * The models to offer for detection and fine-tuning: every pretrained model, in
 * registry order, then the newest fine-tuned model. Older fine-tuned revisions live
 * on the Training Jobs page.
 *
 * A fine-tuned model is offered but never the default (defaultModel below). The
 * newest one used to be put first so that it became everyone's default, and the
 * pretrained baseline it came from was hidden. Anything that registered a model
 * therefore changed which weights every user's next job ran (T34).
 */
export function selectableModels(models: RegisteredModel[]): RegisteredModel[] {
  const latestFinetuned = models
    .filter((m) => m.type === 'finetuned')
    .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))[0];
  const pretrained = models.filter((m) => m.type === 'pretrained');
  return latestFinetuned ? [...pretrained, latestFinetuned] : pretrained;
}

/**
 * The option to select by default: DEFAULT_MODEL_ID if offered and available, else
 * the first available pretrained model. Never a fine-tuned model (T34), and never an
 * unavailable pretrained one, so undefined when the deployment has not opted in to
 * the non-commercial pretrained weights: the user then picks a model they trained.
 */
export function defaultModel(options: RegisteredModel[]): RegisteredModel | undefined {
  const usable = options.filter((m) => m.type === 'pretrained' && isAvailable(m));
  return usable.find((m) => m.model_id === DEFAULT_MODEL_ID) ?? usable[0];
}
