import { Fragment, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useSearchParams, Link, Navigate } from 'react-router-dom';
import {
  listJobs,
  listTrainingJobs,
  createJob,
  apiErrorDetail,
  deleteJob,
  isUuid,
  type Job,
  type TrainingJob,
} from '../services/api';
import { VERSION_HISTORY_LIMIT, linkTrainingJobs, versionSummary } from './trainingLinkage';

const STATUS_COLORS: Record<string, string> = {
  queued: 'bg-gray-500',
  processing: 'bg-blue-500',
  complete: 'bg-green-500',
  failed: 'bg-red-500',
};

const fmt = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : '—');

/**
 * Copy text to the clipboard, falling back for non-HTTPS origins.
 *
 * Resolves true on success. The previous version neither awaited nor caught
 * writeText, so a permission denial (common when the document is not focused) was
 * a silent no-op: the `?.` guard had already passed, so the execCommand fallback
 * never ran and the user got no feedback either way.
 */
async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to the legacy path below
    }
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/** Trailing filename portion of an S3 key, for a compact display. */
function shortS3(key: string): string {
  if (!key) return '—';
  const parts = key.split('/').filter(Boolean);
  const tail = parts[parts.length - 1] ?? key;
  return parts.length > 1 ? `…/${tail}` : tail;
}

function FineTuningSubTable({ jobs }: { jobs: TrainingJob[] }) {
  return (
    <table className="w-full text-xs">
      <thead className="text-gray-500">
        <tr>
          <th className="text-left py-1 font-normal">Version</th>
          <th className="text-left py-1 font-normal">Base Model</th>
          <th className="text-left py-1 font-normal">Status</th>
          <th className="text-left py-1 font-normal">Started</th>
          <th className="text-left py-1 font-normal">Completed</th>
          <th className="text-left py-1 font-normal">Model</th>
        </tr>
      </thead>
      <tbody>
        {jobs.map((t, i) => (
          <tr key={t.training_job_id} data-testid={`training-job-${t.training_job_id}`}>
            <td className="py-1 pr-3 text-gray-400">
              {i === 0 ? <span className="text-green-400">latest</span> : `-${i}`}
            </td>
            <td className="py-1 pr-3">{t.base_model ?? '—'}</td>
            <td className="py-1 pr-3">
              <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${STATUS_COLORS[t.status]}`}>
                {t.status}
              </span>
            </td>
            <td className="py-1 pr-3">{fmt(t.started_at)}</td>
            <td className="py-1 pr-3">{fmt(t.completed_at)}</td>
            <td className="py-1 font-mono text-[10px] break-all">
              {t.status === 'complete' && t.model_artifact ? t.model_artifact : '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function DashboardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // Retry a failed job by re-submitting the same file + config as a new job.
  const retry = useMutation({
    mutationFn: (job: Job) => createJob(job.s3_input_key, job.filename, 0, job.config),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  });

  // Delete a finished job and its stored data (T60). The API refuses (409) while
  // the job, or a retry of the same upload, is still queued or processing.
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: (jobId: string) => deleteJob(jobId),
    onMutate: () => setDeleteError(null),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
    onError: (err) => setDeleteError(apiErrorDetail(err, 'The job could not be deleted.')),
  });
  const confirmDelete = (job: Job) => {
    if (window.confirm(`Delete ${job.filename}, its results and the uploaded scan? This cannot be undone.`)) {
      remove.mutate(job.job_id);
    }
  };

  const { data, isLoading } = useQuery({
    queryKey: ['jobs'],
    queryFn: () => listJobs(),
    refetchInterval: 5000,
    placeholderData: (prev) => prev,
  });
  // Annotation jobs are excluded from the DISPLAYED list (this page is Detection
  // Jobs), but kept in `allJobs` because fine-tuning started from the training-mode
  // upload flow is parented by an annotation job. Filtering them out of the linkage
  // universe too meant those runs could never be attributed to anything.
  const allJobs: Job[] = data?.jobs ?? [];
  const jobs = allJobs.filter((j) => j.config?.job_type !== 'annotation');

  const { data: training } = useQuery({
    queryKey: ['training-jobs'],
    queryFn: () => listTrainingJobs(),
    refetchInterval: 5000,
    placeholderData: (prev) => prev,
  });
  const trainingJobs = training?.training_jobs ?? [];

  // The Training Sessions tab used to live at /dashboard?tab=training. A Route
  // cannot match on a query param, so the redirect belongs here. Evaluated now but
  // ACTED ON after every hook has run — returning early from the middle of the hook
  // list changes hook order between renders, which React forbids.
  const legacyTrainingTab = searchParams.get('tab') === 'training';

  // Copy feedback. Without this a denied clipboard permission looked identical to
  // a successful copy. Keyed per control, not per row: both the job-id and the S3
  // button live on the same row, so a row-keyed flag put the result next to
  // whichever control happened to render it rather than the one that was clicked.
  const [copyFeedback, setCopyFeedback] = useState<{ key: string; ok: boolean } | null>(null);

  const statusFilter = searchParams.get('status') ?? 'all';
  const statusCounts = jobs.reduce<Record<string, number>>((a, j) => {
    a[j.status] = (a[j.status] ?? 0) + 1;
    return a;
  }, {});
  const filteredJobs = statusFilter === 'all' ? jobs : jobs.filter((j) => j.status === statusFilter);
  const FILTERS = ['all', 'queued', 'processing', 'complete', 'failed'];
  const setStatus = (f: string) => {
    const next = new URLSearchParams(searchParams);
    if (f === 'all') next.delete('status');
    else next.set('status', f);
    setSearchParams(next, { replace: true });
  };

  // Group fine-tuning jobs under the detection job they were run on. See
  // trainingLinkage.ts for why the buckets are split by reason.
  const { trainingByJob, fromAnnotation, hiddenByFilter, orphanedTraining } =
    linkTrainingJobs({ allJobs, filteredJobs, trainingJobs });

  /** Copy `text`, then report the outcome against `key` and clear it shortly after. */
  const runCopy = async (key: string, text: string) => {
    const ok = await copyText(text);
    setCopyFeedback({ key, ok });
    window.setTimeout(
      () => setCopyFeedback((cur) => (cur?.key === key ? null : cur)),
      ok ? 2000 : 4000,
    );
  };

  const copyLabel = (key: string) =>
    copyFeedback?.key !== key ? 'copy' : copyFeedback.ok ? 'copied' : 'copy failed';

  const toggle = (jobId: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(jobId)) next.delete(jobId);
      else next.add(jobId);
      return next;
    });

  // Acted on here, below every hook: a Route cannot match on a query param, and
  // returning early from among the hooks changes hook order between renders.
  // It must also sit outside the row loop — nested in there it only fired when a
  // row happened to exist, so the redirect was a no-op on an empty job list.
  if (legacyTrainingTab) {
    return <Navigate to="/training-jobs" replace />;
  }

  return (
    <div className="max-w-screen-2xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Detection Jobs</h2>
        <button
          onClick={() => navigate('/upload')}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded text-sm font-medium transition"
        >
          + New Detection
        </button>
      </div>

      {isLoading && <p className="text-gray-400">Loading...</p>}
      {deleteError && (
        <p className="text-sm text-red-400" role="alert" data-testid="delete-error">
          {deleteError}
        </p>
      )}

      <div className="flex gap-2 flex-wrap" data-testid="jobs-filters">
        {FILTERS.map((f) => (
          <button
            key={f}
            onClick={() => setStatus(f)}
            data-testid={`jobs-filter-${f}`}
            className={`px-2.5 py-1 rounded text-xs capitalize transition ${
              statusFilter === f ? 'bg-blue-600 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
            }`}
          >
            {f}
            {f !== 'all' && <span className="ml-1 opacity-70">{statusCounts[f] ?? 0}</span>}
          </button>
        ))}
      </div>

      <div className="bg-gray-800 rounded-lg overflow-x-auto">
        <table className="w-full text-sm" data-testid="jobs-table">
          <thead className="bg-gray-700">
            <tr>
              <th className="px-4 py-3 text-left">Job ID</th>
              <th className="px-4 py-3 text-left">File</th>
              <th className="px-4 py-3 text-left">S3</th>
              <th className="px-4 py-3 text-left">Submitted</th>
              <th className="px-4 py-3 text-left">Status</th>
              <th className="px-4 py-3 text-left">Mode</th>
              <th className="px-4 py-3 text-left">Detections</th>
              <th className="px-4 py-3 text-left">Time</th>
              <th className="px-4 py-3 text-left whitespace-nowrap">Fine-Tuning</th>
              <th className="px-4 py-3 text-left">Actions</th>
            </tr>
          </thead>
          <tbody>
            {filteredJobs.map((job: Job) => {
              const linked = trainingByJob.get(job.job_id) ?? [];
              const { latest, history, previousCount, truncated, total } = versionSummary(linked);
              const isOpen = expanded.has(job.job_id);
              return (
                <Fragment key={job.job_id}>
                  <tr className="border-t border-gray-700 hover:bg-gray-700/50 transition-colors">
                    <td className="px-4 py-3 font-mono text-[11px] text-gray-400" title={job.job_id}>
                      <button
                        onClick={() => runCopy(`${job.job_id}:id`, job.job_id)}
                        className="hover:text-white transition cursor-copy"
                        title={`Copy full job ID: ${job.job_id}`}
                      >
                        {job.job_id.slice(0, 8)}
                        <span className="ml-1 text-gray-500">
                          {copyLabel(`${job.job_id}:id`)}
                        </span>
                      </button>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs">{job.filename}</td>
                    <td className="px-4 py-3 font-mono text-[11px] text-gray-400">
                      {/*
                        A copy button, not a link. The job record carries only the
                        key, not the bucket, so a console URL cannot be built —
                        underline styling made this read as navigation.
                      */}
                      <button
                        onClick={() => runCopy(`${job.job_id}:s3`, job.s3_input_key)}
                        data-testid={`s3-link-${job.job_id}`}
                        title={`Copy S3 key: ${job.s3_input_key}`}
                        className="hover:text-white transition cursor-copy"
                      >
                        {shortS3(job.s3_input_key)}
                        <span className="ml-1 text-gray-500">
                          {copyLabel(`${job.job_id}:s3`)}
                        </span>
                      </button>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-400">{fmt(job.created_at)}</td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-1 rounded text-xs font-medium ${STATUS_COLORS[job.status]}`}>
                        {job.status}
                      </span>
                    </td>
                    <td className="px-4 py-3">{job.config.model_mode}</td>
                    <td className="px-4 py-3">{job.detection_count ?? '—'}</td>
                    <td className="px-4 py-3">{job.processing_time_s ? `${job.processing_time_s}s` : '—'}</td>
                    <td className="px-4 py-3">
                      {latest ? (
                        <button
                          onClick={() => toggle(job.job_id)}
                          data-testid={`expand-${job.job_id}`}
                          aria-expanded={isOpen}
                          aria-label="Toggle fine-tuning version history"
                          className="flex items-center gap-1.5 text-gray-300 hover:text-white whitespace-nowrap"
                        >
                          <span className="w-3 text-gray-400">{isOpen ? '▾' : '▸'}</span>
                          <span
                            className="px-1.5 py-0.5 rounded bg-gray-700 text-[11px] whitespace-nowrap"
                            title={
                              latest.model_artifact
                                ? `Current model: ${latest.model_artifact}`
                                : `Latest run is ${latest.status}`
                            }
                          >
                            {latest.base_model ?? 'model'} · latest
                          </span>
                          {previousCount > 0 && (
                            <span className="text-[10px] text-gray-500">
                              +{previousCount} prev
                            </span>
                          )}
                        </button>
                      ) : (
                        <span className="text-gray-600">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      {/* job_id comes from the server and becomes a route, so only a
                          UUID is followed (T56). */}
                      {job.status === 'complete' && isUuid(job.job_id) && (
                        <button
                          data-testid={`view-results-${job.job_id}`}
                          onClick={() => navigate(`/viewer/${encodeURIComponent(job.job_id)}`)}
                          className="text-blue-400 hover:text-blue-300 underline"
                        >
                          View Results
                        </button>
                      )}
                      {job.status === 'failed' && (
                        <div className="flex flex-col gap-1">
                          <span className="text-red-400 text-xs">{job.error_message}</span>
                          <button
                            data-testid={`retry-${job.job_id}`}
                            onClick={() => retry.mutate(job)}
                            disabled={retry.isPending}
                            className="text-blue-400 hover:text-blue-300 underline text-xs self-start disabled:text-gray-500"
                          >
                            {retry.isPending ? 'Retrying…' : 'Retry'}
                          </button>
                        </div>
                      )}
                      {(job.status === 'complete' || job.status === 'failed') && (
                        <button
                          data-testid={`delete-${job.job_id}`}
                          onClick={() => confirmDelete(job)}
                          disabled={remove.isPending}
                          className="block mt-1 text-red-400 hover:text-red-300 underline text-xs disabled:text-gray-500"
                        >
                          {remove.isPending && remove.variables === job.job_id ? 'Deleting…' : 'Delete'}
                        </button>
                      )}
                    </td>
                  </tr>
                  {isOpen && history.length > 0 && (
                    <tr className="bg-gray-900/60">
                      <td colSpan={10} className="px-4 py-2 pl-10">
                        <div className="text-xs text-gray-400 mb-1 flex items-center gap-2">
                          <span>
                            Fine-tuning versions for this file, newest first
                            {truncated && ` (showing ${VERSION_HISTORY_LIMIT} of ${total})`}
                          </span>
                          <Link
                            to="/training-jobs"
                            className="text-blue-400 hover:text-blue-300 underline"
                          >
                            full history
                          </Link>
                        </div>
                        <FineTuningSubTable jobs={history} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
            {filteredJobs.length === 0 && !isLoading && (
              <tr>
                <td colSpan={10} className="px-4 py-8 text-center text-gray-500">
                  {statusFilter === 'all'
                    ? 'No jobs yet. Upload a point cloud to get started.'
                    : `No ${statusFilter} jobs.`}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {fromAnnotation.length + hiddenByFilter.length + orphanedTraining.length > 0 && (
        <div className="text-xs text-gray-500 space-y-1" data-testid="unlinked-training-note">
          {fromAnnotation.length > 0 && (
            <p>
              {fromAnnotation.length} fine-tuning run
              {fromAnnotation.length === 1 ? '' : 's'} started from an annotated upload
              rather than a detection job, so {fromAnnotation.length === 1 ? 'it has' : 'they have'}{' '}
              no row here.
            </p>
          )}
          {hiddenByFilter.length > 0 && (
            <p>
              {hiddenByFilter.length} fine-tuning run
              {hiddenByFilter.length === 1 ? '' : 's'} belong to files hidden by the
              current status filter.
            </p>
          )}
          {orphanedTraining.length > 0 && (
            <p>
              {orphanedTraining.length} fine-tuning run
              {orphanedTraining.length === 1 ? '' : 's'} have no matching file at all.
            </p>
          )}
          <p>
            <Link to="/training-jobs" className="text-blue-400 hover:text-blue-300 underline">
              View all training jobs
            </Link>
          </p>
        </div>
      )}
    </div>
  );
}
