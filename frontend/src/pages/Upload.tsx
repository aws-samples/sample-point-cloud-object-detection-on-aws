import { useState, useCallback, useEffect, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import {
  addJobToSession,
  createJob,
  createTrainingSession,
  defaultModel,
  isAvailable,
  listModels,
  selectableModels,
  RegisteredModel,
} from '../services/api';
import { MAX_CLASS_LIST_LENGTH, MAX_NAME_LENGTH } from '../services/limits';
import { uploadFile, UploadProgress } from '../services/upload';

/**
 * Ceiling for *interactive browser* uploads, which is not the ceiling for what the
 * pipeline processes.
 *
 * The docs quote 50 GB as the largest scene the pipeline handles, and those scenes are
 * staged straight into the input bucket (`aws s3 cp`, see docs/EKS_DEPLOY_RUNBOOK.md),
 * not pushed through a browser. This limit is not that limit, and raising it to match
 * the 50 GB figure would be a mistake: the pre-signed URLs a browser upload starts with
 * expire after Settings.presigned_url_expiry, one hour by default, so a transfer long
 * enough to matter would fail partway with its parts already sent. Lifting this would
 * mean refreshing part URLs mid-flight, and there is no endpoint for that.
 */
const MAX_FILE_SIZE_MB = 500;
const MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1e6;
const ACCEPTED_EXTENSIONS = ['.e57', '.las', '.laz', '.ply', '.bin'];

function formatFileSize(bytes: number): string {
  if (bytes < 1e6) return `${(bytes / 1e3).toFixed(0)} KB`;
  if (bytes < 1e9) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${(bytes / 1e9).toFixed(2)} GB`;
}

/**
 * Dropdown label for a registry model. Fine-tuned entries show their base
 * model plus the originating training job id, since only the newest fine-tuned
 * model is offered here and the id is what distinguishes it from its base.
 */
function modelOptionLabel(m: RegisteredModel): string {
  if (m.type !== 'finetuned') return m.name;
  const shortId = m.training_job_id ? m.training_job_id.slice(0, 8) : m.model_id.slice(-8);
  return `\u2605 ${m.base_model} \u00b7 ft ${shortId} (latest)`;
}

function validateFile(file: File): string | null {
  const ext = '.' + file.name.split('.').pop()?.toLowerCase();
  if (!ACCEPTED_EXTENSIONS.includes(ext)) {
    return `Unsupported file type: ${ext}. Accepted: ${ACCEPTED_EXTENSIONS.join(', ')}`;
  }
  if (file.size > MAX_FILE_SIZE_BYTES) {
    return `File too large: ${formatFileSize(file.size)}. Maximum: ${MAX_FILE_SIZE_MB} MB`;
  }
  if (file.size === 0) {
    return 'File is empty.';
  }
  return null;
}

type Mode = 'detection' | 'training';

export function UploadPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const initialMode = searchParams.get('mode') === 'training' ? 'training' : 'detection';
  const [mode, setMode] = useState<Mode>(initialMode);
  const [files, setFiles] = useState<File[]>([]);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadingIdx, setUploadingIdx] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  // Loaded under the ['models'] key so deleting a training job — which
  // unregisters its model — actually invalidates this list. Previously this was a
  // mount-only useEffect, so TrainingJobs' invalidateQueries({queryKey:['models']})
  // hit a key nothing subscribed to: a deleted model stayed selectable AND stayed
  // the default, and a detection job could be submitted against weights that no
  // longer resolved.
  const { data: modelData } = useQuery({
    queryKey: ['models'],
    queryFn: () => listModels(),
  });
  // Pretrained models and the newest fine-tuned model are offered here; older
  // revisions are managed on the Training Jobs page.
  const models = useMemo(
    () => selectableModels(modelData?.models ?? []),
    [modelData],
  );

  // Detection config
  const [detConfig, setDetConfig] = useState({
    model_mode: 'indoor',
    custom_weights_version: null as string | null,
    // Nothing selected until the registry says what this deployment can run: the
    // pretrained models are unavailable unless it opted in to their non-commercial
    // weights (ALLOW_NONCOMMERCIAL_WEIGHTS).
    detection_config: null as string | null,
    confidence_threshold: 0.5,
    use_color: true,
    use_intensity: false,
    coord_frame: 'z-up',
    device_preference: 'auto' as 'auto' | 'cpu' | 'gpu',
  });
  const [selectedModelId, setSelectedModelId] = useState('');
  // The selected model must be one detection can run; an unavailable pretrained
  // model would be refused by the API (HTTP 400).
  const selectedModel = models.find((m) => m.model_id === selectedModelId);
  const canDetect = selectedModel !== undefined && isAvailable(selectedModel);
  const hasRunnableModel = models.some(isAvailable);

  // Training/annotation config
  const [trainConfig, setTrainConfig] = useState({
    model_name: '',
    base_model: 'votenet_scannet',
    coord_frame: 'z-up',
    use_color: true,
    use_intensity: false,
    classes: '' , // comma-separated, editable
  });

  // Re-apply defaults whenever the selectable set changes, not just on mount, so a
  // model deleted elsewhere stops being the selected default. Keyed on the id list
  // rather than the array identity to avoid resetting the user's choice on every
  // refetch that returns the same models.
  const modelIdKey = models.map((m) => m.model_id).join(',');
  useEffect(() => {
    // Default to the pretrained baseline, never a fine-tuned model: whatever last
    // registered a model must not change which weights the next job runs (T34).
    // A fine-tuned model is still one click away in the dropdown.
    const preferred = defaultModel(models);
    if (!preferred) return;
    setSelectedModelId((current) =>
      models.some((m) => m.model_id === current) ? current : preferred.model_id,
    );
    setDetConfig((prev) => {
      // Keep an explicit, still-valid selection; only reset if it went away.
      if (prev.custom_weights_version
          && models.some((m) => m.model_id === prev.custom_weights_version)) {
        return prev;
      }
      return {
        ...prev,
        model_mode: preferred.mode,
        custom_weights_version:
          preferred.type === 'finetuned' ? preferred.model_id : null,
        // Architecture to run. base_model resolves to the architecture for both
        // pretrained (e.g. fcaf3d_scannet) and fine-tuned models.
        detection_config: preferred.base_model ?? preferred.model_id,
      };
    });
    setTrainConfig((prev) =>
      models.some((m) => m.model_id === prev.base_model)
        ? prev
        : { ...prev, base_model: preferred.model_id },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelIdKey]);

  const handleFiles = useCallback((fileList: FileList | File[]) => {
    setError(null);
    const newFiles: File[] = [];
    for (const f of Array.from(fileList)) {
      const err = validateFile(f);
      if (err) {
        setError(err);
        return;
      }
      newFiles.push(f);
    }
    if (mode === 'detection') {
      // Detection: single file
      setFiles(newFiles.slice(0, 1));
    } else {
      // Training: multi-file (append)
      setFiles((prev) => [...prev, ...newFiles]);
    }
  }, [mode]);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragActive(false);
    handleFiles(e.dataTransfer.files);
  };

  const removeFile = (idx: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleDetectionSubmit = async () => {
    if (files.length === 0 || !canDetect) return;
    setUploading(true);
    setError(null);
    try {
      const { s3Key } = await uploadFile(files[0], setProgress);
      await createJob(s3Key, files[0].name, files[0].size, { ...detConfig, job_type: 'detection' } as any);
      navigate('/dashboard');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed.');
      setProgress(null);
    } finally {
      setUploading(false);
    }
  };

  const handleTrainingSubmit = async () => {
    if (files.length === 0) return;
    if (!trainConfig.model_name.trim()) {
      setError('Please enter a model name for this fine-tuning run.');
      return;
    }
    setUploading(true);
    setError(null);
    try {
      // Create a training session
      const classes = trainConfig.classes.split(',').map(s => s.trim()).filter(Boolean);
      const session = await createTrainingSession({
        name: trainConfig.model_name,
        base_model: trainConfig.base_model,
        coord_frame: trainConfig.coord_frame,
        use_color: trainConfig.use_color,
        use_intensity: trainConfig.use_intensity,
        classes,
      });

      // Upload each file as an annotation job and link to session
      for (let i = 0; i < files.length; i++) {
        setUploadingIdx(i);
        const { s3Key } = await uploadFile(files[i], setProgress);
        const job = await createJob(s3Key, files[i].name, files[i].size, {
          job_type: 'annotation',
          use_color: trainConfig.use_color,
          use_intensity: trainConfig.use_intensity,
          coord_frame: trainConfig.coord_frame,
          model_mode: 'indoor',
          confidence_threshold: 0.5,
        } as any);
        // Link job to session
        await addJobToSession(session.session_id, job.job_id);
      }
      navigate('/dashboard');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed.');
      setProgress(null);
    } finally {
      setUploading(false);
      setUploadingIdx(0);
    }
  };

  const handleClear = () => {
    setFiles([]);
    setProgress(null);
    setError(null);
  };

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <h2 className="text-2xl font-bold">Upload Point Cloud</h2>

      {/* Mode tabs */}
      <div className="flex rounded-lg overflow-hidden border border-gray-700">
        <button
          onClick={() => { setMode('detection'); setFiles([]); setError(null); }}
          className={`flex-1 py-2.5 text-sm font-medium transition ${
            mode === 'detection' ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-300 hover:bg-gray-700'
          }`}
        >
          Detection
        </button>
        <button
          onClick={() => { setMode('training'); setFiles([]); setError(null); }}
          className={`flex-1 py-2.5 text-sm font-medium transition ${
            mode === 'training' ? 'bg-amber-600 text-white' : 'bg-gray-800 text-gray-300 hover:bg-gray-700'
          }`}
        >
          Training / Annotation
        </button>
      </div>

      {/* Error alert */}
      {error && (
        <div className="bg-red-900/50 border border-red-500 rounded-lg px-4 py-3 text-red-200 flex items-start gap-3" role="alert">
          <svg className="w-5 h-5 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
          </svg>
          <p className="flex-1 font-medium text-sm">{error}</p>
          <button onClick={() => setError(null)} className="text-red-300 hover:text-red-100" aria-label="Dismiss">✕</button>
        </div>
      )}

      {/* Drop zone */}
      <div
        onDrop={handleDrop}
        onDragOver={(e) => { e.preventDefault(); setDragActive(true); }}
        onDragLeave={() => setDragActive(false)}
        className={`border-2 border-dashed rounded-lg p-10 text-center cursor-pointer transition ${
          dragActive ? 'border-blue-400 bg-blue-900/20'
            : files.length > 0 ? 'border-green-500 bg-green-900/10'
            : 'border-gray-600 hover:border-blue-500'
        }`}
        onClick={() => document.getElementById('file-input')?.click()}
      >
        {files.length > 0 ? (
          <div className="space-y-2">
            {files.map((f, i) => (
              <div key={i} className="flex items-center justify-between bg-gray-800/50 rounded px-3 py-1.5">
                <span className="text-green-400 text-sm font-mono truncate">{f.name}</span>
                <div className="flex items-center gap-2">
                  <span className="text-gray-400 text-xs">{formatFileSize(f.size)}</span>
                  <button onClick={(e) => { e.stopPropagation(); removeFile(i); }} className="text-gray-500 hover:text-red-400 text-xs">✕</button>
                </div>
              </div>
            ))}
            <button onClick={(e) => { e.stopPropagation(); handleClear(); }} className="text-sm text-gray-400 hover:text-white underline mt-2">
              Clear all
            </button>
          </div>
        ) : (
          <div>
            <svg className="w-12 h-12 mx-auto text-gray-500 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
            </svg>
            <p className="text-gray-300">
              {mode === 'detection'
                ? 'Drag & drop a point cloud file, or click to select'
                : 'Drag & drop point cloud files for annotation (multiple allowed)'}
            </p>
            <p className="text-sm text-gray-500 mt-1">
              Supports: E57, LAS, LAZ, PLY, BIN — max {MAX_FILE_SIZE_MB} MB each
            </p>
          </div>
        )}
        <input
          id="file-input"
          type="file"
          accept={ACCEPTED_EXTENSIONS.join(',')}
          multiple={mode === 'training'}
          className="hidden"
          onChange={(e) => { if (e.target.files) handleFiles(e.target.files); e.target.value = ''; }}
          data-testid="upload-file-input"
        />
      </div>

      {/* Detection Configuration */}
      {mode === 'detection' && (
        <div className="bg-gray-800 rounded-lg p-4 space-y-3">
          <h3 className="font-semibold">Detection Configuration</h3>
          <div className="grid grid-cols-2 gap-4">
            <label className="block">
              <span className="text-sm text-gray-400 flex items-center justify-between">
                <span>Detection Model</span>
                <Link
                  to="/training-jobs"
                  data-testid="model-history-link"
                  className="text-xs text-blue-400 hover:text-blue-300 underline"
                  title="Older fine-tuned revisions are managed on the Training Jobs page"
                >
                  version history
                </Link>
              </span>
              <select
                data-testid="config-model-mode"
                value={selectedModelId}
                onChange={(e) => {
                  const val = e.target.value;
                  setSelectedModelId(val);
                  const model = models.find((m) => m.model_id === val);
                  if (model) {
                    setDetConfig({ ...detConfig, model_mode: model.mode, custom_weights_version: model.type === 'finetuned' ? model.model_id : null, detection_config: model.base_model ?? model.model_id });
                  }
                }}
                className="mt-1 block w-full bg-gray-700 rounded px-3 py-2"
              >
                {!canDetect && (
                  <option value="" disabled>
                    {hasRunnableModel ? 'Select a model' : 'No model available'}
                  </option>
                )}
                {models.map((m) => (
                  <option key={m.model_id} value={m.model_id} disabled={!isAvailable(m)}>
                    {m.type === 'finetuned' ? '\u2605 ' : ''}{m.name}
                    {isAvailable(m) ? '' : ' (non-commercial weights disabled)'}
                  </option>
                ))}
              </select>
              {!hasRunnableModel && (
                <p data-testid="no-model-notice" className="mt-2 text-xs text-amber-400">
                  No detection model is available. The pretrained models were trained on
                  non-commercial data and are disabled in this deployment. Train a model
                  on data you may use commercially and it appears here; see the README,
                  &quot;Commercially clean model&quot;.
                </p>
              )}
            </label>
            <label className="block">
              <span className="text-sm text-gray-400">Confidence Threshold</span>
              <input type="range" min="0.1" max="0.99" step="0.05" value={detConfig.confidence_threshold}
                onChange={(e) => setDetConfig({ ...detConfig, confidence_threshold: parseFloat(e.target.value) })} className="mt-3 block w-full" />
              <span className="text-xs text-gray-400">{detConfig.confidence_threshold.toFixed(2)}</span>
            </label>
            <label className="block">
              <span className="text-sm text-gray-400">Coordinate Frame</span>
              <select value={detConfig.coord_frame} onChange={(e) => setDetConfig({ ...detConfig, coord_frame: e.target.value })} className="mt-1 block w-full bg-gray-700 rounded px-3 py-2">
                <option value="z-up">Z-up (default)</option>
                <option value="y-up">Y-up</option>
                <option value="livox">Livox Mid-360</option>
                <option value="leica">Leica RTC360</option>
                <option value="ros">ROS</option>
              </select>
            </label>
            <label className="block">
              <span className="text-sm text-gray-400">Compute Device</span>
              <select value={detConfig.device_preference} onChange={(e) => setDetConfig({ ...detConfig, device_preference: e.target.value as any })} className="mt-1 block w-full bg-gray-700 rounded px-3 py-2">
                <option value="auto">Auto (GPU if available)</option>
                <option value="cpu">CPU (slower, always available)</option>
                <option value="gpu">GPU (fast, requires capacity)</option>
              </select>
            </label>
            <div className="flex items-center gap-4 pt-5">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={detConfig.use_color} onChange={(e) => setDetConfig({ ...detConfig, use_color: e.target.checked })} className="rounded" />
                <span className="text-sm">RGB Color</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={detConfig.use_intensity} onChange={(e) => setDetConfig({ ...detConfig, use_intensity: e.target.checked })} className="rounded" />
                <span className="text-sm">Intensity</span>
              </label>
            </div>
          </div>
        </div>
      )}

      {/* Training/Annotation Configuration */}
      {mode === 'training' && (
        <div className="bg-gray-800 rounded-lg p-4 space-y-3">
          <h3 className="font-semibold">Training Job Configuration</h3>
          <p className="text-xs text-gray-400">Upload point clouds, annotate them in the viewer, then start fine-tuning.</p>
          <div className="grid grid-cols-2 gap-4">
            <label className="block col-span-2">
              <span className="text-sm text-gray-400">Model Name</span>
              <input
                type="text"
                placeholder="e.g. doors-v2, construction-site-barriers"
                maxLength={MAX_NAME_LENGTH}
                value={trainConfig.model_name}
                onChange={(e) => setTrainConfig({ ...trainConfig, model_name: e.target.value })}
                className="mt-1 block w-full bg-gray-700 rounded px-3 py-2 text-sm"
              />
            </label>
            <label className="block">
              <span className="text-sm text-gray-400 flex items-center justify-between">
                <span>Base Model</span>
                <Link
                  to="/training-jobs"
                  data-testid="base-model-history-link"
                  className="text-xs text-blue-400 hover:text-blue-300 underline"
                  title="Older fine-tuned revisions are managed on the Training Jobs page"
                >
                  version history
                </Link>
              </span>
              <select
                data-testid="config-base-model"
                value={trainConfig.base_model}
                onChange={(e) => setTrainConfig({ ...trainConfig, base_model: e.target.value })}
                className="mt-1 block w-full bg-gray-700 rounded px-3 py-2"
              >
                {models.length > 0 ? (
                  models.map((m) => (
                    <option key={m.model_id} value={m.model_id}>
                      {modelOptionLabel(m)}
                    </option>
                  ))
                ) : (
                  <>
                    <option value="votenet_scannet">VoteNet (Indoor)</option>
                    <option value="fcaf3d_scannet">FCAF3D (Indoor)</option>
                    <option value="pointpillars_kitti">PointPillars (Outdoor)</option>
                    <option value="centerpoint_nuscenes">CenterPoint (Outdoor)</option>
                  </>
                )}
              </select>
            </label>
            <label className="block">
              <span className="text-sm text-gray-400">Coordinate Frame</span>
              <select value={trainConfig.coord_frame} onChange={(e) => setTrainConfig({ ...trainConfig, coord_frame: e.target.value })} className="mt-1 block w-full bg-gray-700 rounded px-3 py-2">
                <option value="z-up">Z-up (default)</option>
                <option value="y-up">Y-up</option>
                <option value="livox">Livox Mid-360</option>
                <option value="leica">Leica RTC360</option>
                <option value="ros">ROS</option>
              </select>
            </label>
            <label className="block col-span-2">
              <span className="text-sm text-gray-400">Classes (comma-separated, or assign in viewer)</span>
              <input
                type="text"
                placeholder="e.g. door, window, wall, column"
                maxLength={MAX_CLASS_LIST_LENGTH}
                value={trainConfig.classes}
                onChange={(e) => setTrainConfig({ ...trainConfig, classes: e.target.value })}
                className="mt-1 block w-full bg-gray-700 rounded px-3 py-2 text-sm"
              />
              <span className="text-xs text-gray-500 mt-0.5">Leave empty to define classes during annotation</span>
            </label>
            <div className="flex items-center gap-4 pt-2">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={trainConfig.use_color} onChange={(e) => setTrainConfig({ ...trainConfig, use_color: e.target.checked })} className="rounded" />
                <span className="text-sm">RGB Color</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={trainConfig.use_intensity} onChange={(e) => setTrainConfig({ ...trainConfig, use_intensity: e.target.checked })} className="rounded" />
                <span className="text-sm">Intensity</span>
              </label>
            </div>
          </div>
        </div>
      )}

      {/* Progress */}
      {progress && (
        <div className="bg-gray-800 rounded-lg p-4">
          <div className="flex justify-between text-sm mb-1">
            <span>
              {mode === 'training' && files.length > 1
                ? `Uploading file ${uploadingIdx + 1}/${files.length}...`
                : `Uploading... ${progress.partsCompleted}/${progress.totalParts} parts`}
            </span>
            <span>{progress.percent}%</span>
          </div>
          <div className="w-full bg-gray-700 rounded-full h-2">
            <div className="bg-blue-500 h-2 rounded-full transition-all duration-300" style={{ width: `${progress.percent}%` }} />
          </div>
          <p className="text-xs text-gray-500 mt-1">{formatFileSize(progress.loaded)} / {formatFileSize(progress.total)}</p>
        </div>
      )}

      {/* Submit */}
      {mode === 'detection' ? (
        <button
          data-testid="upload-submit-button"
          onClick={handleDetectionSubmit}
          disabled={files.length === 0 || uploading || !canDetect}
          className="w-full bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 disabled:cursor-not-allowed rounded-lg py-3 font-semibold transition"
        >
          {uploading ? 'Uploading...' : 'Upload & Detect'}
        </button>
      ) : (
        <button
          data-testid="upload-annotate-button"
          onClick={handleTrainingSubmit}
          disabled={files.length === 0 || uploading}
          className="w-full bg-amber-600 hover:bg-amber-500 disabled:bg-gray-600 disabled:cursor-not-allowed rounded-lg py-3 font-semibold transition"
        >
          {uploading ? `Uploading ${uploadingIdx + 1}/${files.length}...` : `Upload ${files.length} file${files.length !== 1 ? 's' : ''} for Annotation`}
        </button>
      )}
    </div>
  );
}
