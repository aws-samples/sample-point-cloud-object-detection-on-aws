import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  listTrainingJobs,
  deleteTrainingJobs,
  rerunTrainingJobs,
  listModels,
  selectableModels,
  isUuid,
} from '../services/api';

const STATUS_COLORS: Record<string, string> = {
  queued: 'bg-gray-500',
  processing: 'bg-blue-500',
  complete: 'bg-green-500',
  failed: 'bg-red-500',
};

const fmt = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : '—');

/**
 * Fine-tuning history. Every entry is a fine-tuning run against a pre-trained
 * base model. Rows are multi-selectable so a batch can be deleted, or
 * resubmitted to retrain in the order shown.
 */
export function TrainingJobsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ['training-jobs'],
    queryFn: () => listTrainingJobs(),
    refetchInterval: 5000,
    placeholderData: (prev) => prev,
  });

  // Newest first by SUBMISSION time. The backend already sorts, but don't rely on it.
  const jobs = useMemo(() => {
    const list = data?.training_jobs ?? [];
    return [...list].sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));
  }, [data]);

  // Which run is "latest" has to come from the registry, not from row order.
  // Rows are ordered by submission time; the detection dropdown offers the newest
  // REGISTERED model, i.e. by completion time. Concurrent re-runs with different
  // epoch counts finish out of submission order, so badging row 0 could mark a
  // different model as latest than the one actually used for inference.
  const { data: modelData } = useQuery({
    queryKey: ['models'],
    queryFn: () => listModels(),
  });
  const latestTrainingJobId = useMemo(() => {
    const newest = selectableModels(modelData?.models ?? []).find(
      (m) => m.type === 'finetuned',
    );
    return newest?.training_job_id ?? null;
  }, [modelData]);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);

  // Drop selections whose rows no longer exist so stale ids are never submitted.
  const liveIds = new Set(jobs.map((t) => t.training_job_id));
  const selectedLive = [...selected].filter((id) => liveIds.has(id));
  const selectedJobs = jobs.filter((t) => selectedLive.includes(t.training_job_id));
  const allSelected = jobs.length > 0 && selectedLive.length === jobs.length;

  const toggleOne = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(jobs.map((t) => t.training_job_id)));


  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['training-jobs'] });
    // Deleting a job unregisters its model, so the detection dropdown is stale too.
    queryClient.invalidateQueries({ queryKey: ['models'] });
  };

  const del = useMutation({
    mutationFn: () => deleteTrainingJobs(selectedLive),
    // No onError: deleteTrainingJobs catches per-id and always resolves, so an
    // onError handler here would be unreachable — and its copy ("nothing was
    // removed") would be false after a partial batch.
    onSuccess: ({ deleted, failed }) => {
      setNotice(
        failed.length === 0
          ? `Deleted ${deleted.length} fine-tuning job${deleted.length === 1 ? '' : 's'}.`
          : `Deleted ${deleted.length}, failed to delete ${failed.length}. ` +
            `The failed job${failed.length === 1 ? '' : 's'} remain selected so you can retry.`,
      );
      // Keep failures selected rather than clearing everything: clearing forces
      // the user to re-find them in an unpaginated list to retry.
      setSelected(new Set(failed));
      refresh();
    },
  });

  const rerun = useMutation({
    mutationFn: () => rerunTrainingJobs(selectedJobs),
    // No onError, for the same reason as delete above.
    onSuccess: ({ submitted, skipped, failed }) => {
      const parts = [`Resubmitted ${submitted.length} job${submitted.length === 1 ? '' : 's'}.`];
      if (skipped.length > 0) {
        parts.push(
          `${skipped.length} skipped (no source detection job to retrain from).`,
        );
      }
      if (failed.length > 0) {
        // Distinct from skipped: these can be retried.
        parts.push(
          `${failed.length} failed to submit; ${failed.length === 1 ? 'it remains' : 'they remain'} selected.`,
        );
      }
      setNotice(parts.join(' '));
      setSelected(new Set(failed.map((t) => t.training_job_id)));
      refresh();
    },
  });

  const busy = del.isPending || rerun.isPending;

  const confirmDelete = () => {
    const n = selectedLive.length;
    const msg =
      `Delete ${n} fine-tuning job${n === 1 ? '' : 's'}?\n\n` +
      `This also unregisters the fine-tuned model each job produced, so those ` +
      `models will no longer be selectable for detection.\n\n` +
      `The job record and registry entry cannot be recovered. Checkpoint files ` +
      `already written to S3 are NOT deleted.`;
    if (window.confirm(msg)) del.mutate();
  };

  // Re-run needs a guard at least as strong as delete's. Each resubmission is a
  // GPU training run on on-demand capacity that scales from zero, so a stray click
  // after select-all is the most expensive action on this page — and it sits
  // immediately beside the button that does have a confirmation.
  const confirmRerun = () => {
    const n = selectedJobs.length;
    const msg =
      `Re-run ${n} fine-tuning job${n === 1 ? '' : 's'}?\n\n` +
      `Each one starts a separate GPU training run. They execute concurrently, ` +
      `not in the order listed, and there is no way to cancel them from this page.`;
    if (window.confirm(msg)) rerun.mutate();
  };

  return (
    <div className="max-w-6xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold">Training Jobs (Fine-Tuning)</h2>
          <p className="text-sm text-gray-400 mt-1">
            Every run fine-tunes a base model. Newest first. A run is created by
            annotating a detection job and submitting fine-tuning from the viewer —
            uploading data is only the first step.
          </p>
        </div>
        {/*
          Labelled for what this actually does. It routes to the training-mode
          upload, which creates a session and annotation jobs; the training job
          itself is submitted later from the viewer's fine-tuning dialog. Calling
          it "+ New Training Job" implied one run was created on click.
        */}
        <button
          onClick={() => navigate('/upload?mode=training')}
          data-testid="new-training-job"
          title="Upload point clouds to annotate, then submit fine-tuning from the viewer"
          className="px-4 py-2 bg-amber-600 hover:bg-amber-500 rounded text-sm font-medium transition"
        >
          + Upload data to fine-tune
        </button>
      </div>

      {/* Bulk action bar */}
      <div
        className="flex items-center gap-3 bg-gray-800 rounded-lg px-4 py-2.5"
        data-testid="bulk-actions"
      >
        <span className="text-sm text-gray-400">
          {selectedLive.length === 0
            ? 'Select jobs to delete or re-run'
            : `${selectedLive.length} selected`}
        </span>
        <div className="flex-1" />
        <button
          onClick={confirmRerun}
          disabled={selectedLive.length === 0 || busy}
          data-testid="rerun-selected"
          className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 disabled:text-gray-500 rounded text-sm font-medium transition"
        >
          {rerun.isPending ? 'Resubmitting…' : 'Re-run selected'}
        </button>
        <button
          onClick={confirmDelete}
          disabled={selectedLive.length === 0 || busy}
          data-testid="delete-selected"
          className="px-3 py-1.5 bg-red-600 hover:bg-red-500 disabled:bg-gray-700 disabled:text-gray-500 rounded text-sm font-medium transition"
        >
          {del.isPending ? 'Deleting…' : 'Delete selected'}
        </button>
      </div>

      {notice && (
        <div
          className="bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-sm flex items-center justify-between"
          data-testid="bulk-notice"
        >
          <span>{notice}</span>
          <button onClick={() => setNotice(null)} className="text-gray-500 hover:text-white">
            ✕
          </button>
        </div>
      )}

      {isLoading && <p className="text-gray-400">Loading...</p>}

      <div className="bg-gray-800 rounded-lg overflow-hidden">
        <table className="w-full text-sm" data-testid="training-jobs-table">
          <thead className="bg-gray-700">
            <tr>
              <th className="px-4 py-3 text-left w-10">
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={toggleAll}
                  disabled={jobs.length === 0}
                  aria-label="Select all fine-tuning jobs"
                  data-testid="select-all"
                  className="rounded"
                />
              </th>
              <th className="px-4 py-3 text-left">Source File</th>
              <th className="px-4 py-3 text-left">Base Model</th>
              <th className="px-4 py-3 text-left">Epochs</th>
              <th className="px-4 py-3 text-left">Status</th>
              <th className="px-4 py-3 text-left">Created</th>
              <th className="px-4 py-3 text-left">Completed</th>
              <th className="px-4 py-3 text-left">Model Artifact</th>
              <th className="px-4 py-3 text-left">Source Job</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((t, i) => {
              const isSelected = selectedLive.includes(t.training_job_id);
              // job_id comes from the server and becomes a route, so only a UUID
              // is followed (T56).
              const sourceJobId = isUuid(t.job_id) ? t.job_id : null;
              return (
                <tr
                  key={t.training_job_id}
                  data-testid={`training-job-${t.training_job_id}`}
                  className={`border-t border-gray-700 transition-colors ${
                    isSelected ? 'bg-blue-900/30' : 'hover:bg-gray-700/50'
                  }`}
                >
                  <td className="px-4 py-3">
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => toggleOne(t.training_job_id)}
                      aria-label={`Select fine-tuning job ${t.training_job_id.slice(0, 8)}`}
                      data-testid={`select-${t.training_job_id}`}
                      className="rounded"
                    />
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {t.source_filename ?? '—'}
                    {latestTrainingJobId
                      ? t.training_job_id === latestTrainingJobId && (
                          <span
                            className="ml-2 px-1.5 py-0.5 rounded bg-green-700 text-[10px] align-middle"
                            title="Produced the newest registered model - the fine-tuned model offered for detection"
                          >
                            latest
                          </span>
                        )
                      : i === 0 && (
                          <span
                            className="ml-2 px-1.5 py-0.5 rounded bg-gray-600 text-[10px] align-middle"
                            title="Most recently submitted run (no registered model yet)"
                          >
                            newest
                          </span>
                        )}
                  </td>
                  <td className="px-4 py-3">{t.base_model ?? '—'}</td>
                  <td className="px-4 py-3 text-gray-400">{t.epochs ?? '—'}</td>
                  <td className="px-4 py-3">
                    <span
                      className={`px-2 py-1 rounded text-xs font-medium ${
                        STATUS_COLORS[t.status] ?? 'bg-gray-600'
                      }`}
                    >
                      {t.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-400">{fmt(t.created_at)}</td>
                  <td className="px-4 py-3 text-xs text-gray-400">{fmt(t.completed_at)}</td>
                  <td className="px-4 py-3 font-mono text-[11px] break-all">
                    {t.status === 'complete' && t.model_artifact ? t.model_artifact : '—'}
                  </td>
                  <td className="px-4 py-3">
                    {sourceJobId ? (
                      <button
                        onClick={() => navigate(`/viewer/${encodeURIComponent(sourceJobId)}`)}
                        className="text-blue-400 hover:text-blue-300 underline text-xs"
                      >
                        View source
                      </button>
                    ) : (
                      <span className="text-gray-600 text-xs">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
            {jobs.length === 0 && !isLoading && (
              <tr>
                <td colSpan={9} className="px-4 py-8 text-center text-gray-500">
                  <p>No fine-tuning jobs yet.</p>
                  <p className="text-sm mt-1">
                    Run a detection job, correct its annotations in the viewer, then start
                    fine-tuning.
                  </p>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
