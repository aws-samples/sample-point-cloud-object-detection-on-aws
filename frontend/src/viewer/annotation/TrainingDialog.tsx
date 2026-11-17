/**
 * Fine-tuning submission + status modal. Submits a TrainingRequest for the
 * current job's saved annotations, then polls the training job status (React
 * Query, ~5s, matching the Dashboard) until it reaches a terminal state.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  submitTraining,
  getTrainingJob,
  listModels,
  selectableModels,
  defaultModel,
} from '../../services/api';
import { MAX_CLASS_LIST_LENGTH, MAX_EPOCHS } from '../../services/limits';

// Fallback only, for when the registry cannot be reached. The real list comes from
// GET /api/models below. This dialog previously hardcoded two pretrained ids and
// never asked the registry, which meant the one path that can actually start a
// training run could not select a fine-tuned model — so "continue from your latest
// fine-tuned model" was implemented on the Upload page and absent here.
const FALLBACK_BASE_MODELS = ['votenet_scannet', 'fcaf3d_scannet'];

interface Props {
  jobId: string;
  usedClasses: string[];
  onClose: () => void;
}

export function TrainingDialog({ jobId, usedClasses, onClose }: Props) {
  // Shares the ['models'] key with Upload, so unregistering a model on the
  // Training Jobs page invalidates this list too.
  const { data: modelData } = useQuery({
    queryKey: ['models'],
    queryFn: () => listModels(),
  });
  const baseOptions = useMemo(() => {
    const options = selectableModels(modelData?.models ?? []);
    return options.length > 0 ? options : null;
  }, [modelData]);

  const [baseModel, setBaseModel] = useState(FALLBACK_BASE_MODELS[0]);

  // Once the registry loads, default to the pretrained baseline, never a fine-tuned
  // model (T34); the newest fine-tuned model stays selectable. Only overrides a
  // value the user has not chosen.
  const defaultBaseId = baseOptions ? defaultModel(baseOptions)?.model_id : undefined;
  useEffect(() => {
    if (defaultBaseId) setBaseModel((cur) =>
      cur === FALLBACK_BASE_MODELS[0] ? defaultBaseId : cur,
    );
  }, [defaultBaseId]);
  const [epochs, setEpochs] = useState(30);
  const [classes, setClasses] = useState(usedClasses.join(', '));
  const [trainingJobId, setTrainingJobId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  // Focus trap + Escape to close
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
        return;
      }
      if (e.key === 'Tab' && dialogRef.current) {
        const focusable = dialogRef.current.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    // Focus the dialog panel on mount
    dialogRef.current?.focus();
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const { data: job } = useQuery({
    queryKey: ['training', trainingJobId],
    queryFn: () => getTrainingJob(trainingJobId as string),
    enabled: !!trainingJobId,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === 'complete' || s === 'failed' ? false : 5000;
    },
  });

  const submit = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const res = await submitTraining({
        job_id: jobId,
        base_model: baseModel,
        epochs,
        custom_classes: classes
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
      });
      setTrainingJobId(res.training_job_id);
    } catch {
      setSubmitError('Failed to submit training job. Try again.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" data-testid="training-dialog" role="dialog" aria-modal="true" aria-label="Start fine-tuning">
      <div ref={dialogRef} tabIndex={-1} className="bg-gray-800 rounded-lg p-6 w-96 space-y-4 outline-none">
        <div className="flex items-center justify-between">
          <h3 className="font-bold">Start fine-tuning</h3>
          <button onClick={onClose} className="text-gray-500 hover:text-white">
            ✕
          </button>
        </div>

        {!trainingJobId ? (
          <>
            <div>
              <label className="block text-xs text-gray-400 mb-1">Base model</label>
              <select
                value={baseModel}
                onChange={(e) => setBaseModel(e.target.value)}
                className="w-full bg-gray-700 rounded px-2 py-1.5 text-sm"
              >
                {baseOptions
                  ? baseOptions.map((m) => (
                      <option key={m.model_id} value={m.model_id}>
                        {m.name}
                        {m.type === 'finetuned' ? ' (latest fine-tuned)' : ''}
                      </option>
                    ))
                  : FALLBACK_BASE_MODELS.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">Epochs</label>
              <input
                type="number"
                min={1}
                max={MAX_EPOCHS}
                value={epochs}
                onChange={(e) =>
                  setEpochs(Math.min(MAX_EPOCHS, Math.max(1, parseInt(e.target.value || '1', 10))))
                }
                className="w-full bg-gray-700 rounded px-2 py-1.5 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">Classes (comma-separated)</label>
              <input
                maxLength={MAX_CLASS_LIST_LENGTH}
                value={classes}
                onChange={(e) => setClasses(e.target.value)}
                className="w-full bg-gray-700 rounded px-2 py-1.5 text-sm"
              />
            </div>
            {submitError && <p className="text-xs text-red-400">{submitError}</p>}
            <button
              disabled={submitting}
              onClick={submit}
              className={`w-full px-3 py-2 rounded text-sm font-medium ${
                submitting ? 'bg-gray-700 text-gray-500' : 'bg-green-600 hover:bg-green-500 text-white'
              }`}
            >
              {submitting ? 'Submitting…' : 'Submit'}
            </button>
          </>
        ) : (
          <div className="space-y-2" data-testid="training-status">
            <p className="text-sm">
              Training job: <span className="font-mono text-xs">{trainingJobId.slice(0, 8)}</span>
            </p>
            <p className="text-sm">
              Status: <span className="font-medium">{job?.status ?? 'queued'}</span>
            </p>
            {job?.status === 'complete' && job.model_artifact && (
              <p className="text-xs text-green-300 break-all">Model: {job.model_artifact}</p>
            )}
            {job?.status === 'failed' && (
              <p className="text-xs text-red-400">{job.error_message || 'Training failed.'}</p>
            )}
            <button onClick={onClose} className="w-full px-3 py-2 rounded text-sm bg-gray-700 hover:bg-gray-600">
              Close
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
