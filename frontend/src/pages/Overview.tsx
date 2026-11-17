/**
 * System Overview — aggregated performance/status/stats across the whole
 * project (detection, annotation, fine-tuning), for anyone to gauge how the
 * system is running. Rolls up the same job/training data the Dashboard lists,
 * polling every 5s. Shares React Query keys with the Dashboard so the data is
 * fetched once and cached.
 */
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { listJobs, listTrainingJobs } from '../services/api';

const STATUS_COLORS: Record<string, string> = {
  queued: 'bg-gray-500',
  processing: 'bg-blue-500',
  complete: 'bg-green-500',
  failed: 'bg-red-500',
};

function StatCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="bg-gray-800 rounded-lg p-4">
      <div className="text-xs text-gray-400">{label}</div>
      <div className="text-3xl font-bold mt-1">{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-1">{sub}</div>}
    </div>
  );
}

function StatusBreakdown({
  counts,
  onFailedClick,
}: {
  counts: Record<string, number>;
  onFailedClick?: () => void;
}) {
  const order = ['complete', 'processing', 'queued', 'failed'];
  return (
    <div className="flex flex-wrap gap-3">
      {order.map((s) => {
        const inner = (
          <>
            <span className={`w-2.5 h-2.5 rounded-sm ${STATUS_COLORS[s]}`} />
            <span className="capitalize text-gray-300">{s}</span>
            <span className="text-gray-400">{counts[s] ?? 0}</span>
          </>
        );
        const failedClickable = s === 'failed' && (counts.failed ?? 0) > 0 && onFailedClick;
        return failedClickable ? (
          <button
            key={s}
            onClick={onFailedClick}
            title="View failed jobs"
            className="flex items-center gap-1.5 text-sm underline decoration-dotted underline-offset-2 hover:text-white"
          >
            {inner}
          </button>
        ) : (
          <div key={s} className="flex items-center gap-1.5 text-sm">
            {inner}
          </div>
        );
      })}
    </div>
  );
}

function fmtDuration(sec: number): string {
  if (!sec) return '—';
  if (sec < 90) return `${Math.round(sec)}s`;
  const m = Math.round(sec / 60);
  if (m < 90) return `${m}m`;
  return `${(m / 60).toFixed(1)}h`;
}

function health(counts: Record<string, number>, total: number): { label: string; cls: string } {
  if (!total) return { label: 'Idle', cls: 'bg-gray-600' };
  if ((counts.failed ?? 0) > 0) return { label: 'Attention', cls: 'bg-amber-600' };
  if ((counts.processing ?? 0) + (counts.queued ?? 0) > 0) return { label: 'Active', cls: 'bg-blue-600' };
  return { label: 'Healthy', cls: 'bg-green-600' };
}

export function OverviewPage() {
  const navigate = useNavigate();
  const { data: jobData } = useQuery({ queryKey: ['jobs'], queryFn: () => listJobs(), refetchInterval: 5000, placeholderData: (prev) => prev });
  const { data: trainData } = useQuery({
    queryKey: ['training-jobs'],
    queryFn: () => listTrainingJobs(),
    refetchInterval: 5000,
    placeholderData: (prev) => prev,
  });
  const jobs = jobData?.jobs ?? [];
  const training = trainData?.training_jobs ?? [];

  const detCounts = jobs.reduce<Record<string, number>>((a, j) => {
    a[j.status] = (a[j.status] ?? 0) + 1;
    return a;
  }, {});
  const trainCounts = training.reduce<Record<string, number>>((a, t) => {
    a[t.status] = (a[t.status] ?? 0) + 1;
    return a;
  }, {});

  const detComplete = jobs.filter((j) => j.status === 'complete');
  const detFailed = detCounts.failed ?? 0;
  const detFinished = (detCounts.complete ?? 0) + detFailed;
  const detSuccess = detFinished ? Math.round(((detCounts.complete ?? 0) / detFinished) * 100) : 0;
  const totalDetections = jobs.reduce((s, j) => s + (j.detection_count ?? 0), 0);
  const avgProc = detComplete.length
    ? detComplete.reduce((s, j) => s + (j.processing_time_s ?? 0), 0) / detComplete.length
    : 0;

  const modelsProduced = trainCounts.complete ?? 0;
  const completeTrain = training.filter((t) => t.status === 'complete' && t.started_at && t.completed_at);
  const avgTurn = completeTrain.length
    ? completeTrain.reduce(
        (s, t) => s + (new Date(t.completed_at as string).getTime() - new Date(t.started_at as string).getTime()),
        0,
      ) /
      completeTrain.length /
      1000
    : 0;

  const detHealth = health(detCounts, jobs.length);
  const trainHealth = health(trainCounts, training.length);

  return (
    <div className="max-w-5xl mx-auto space-y-6" data-testid="overview-page">
      <h2 className="text-2xl font-bold">System Overview</h2>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="Detection jobs" value={jobs.length} sub={`${detCounts.complete ?? 0} complete`} />
        <StatCard label="Fine-tuning jobs" value={training.length} sub={`${modelsProduced} models produced`} />
        <StatCard label="Total detections" value={totalDetections.toLocaleString()} />
        <StatCard label="Detection success rate" value={`${detSuccess}%`} sub={`${detFinished} finished`} />
      </div>

      <div className="grid md:grid-cols-3 gap-4">
        <div className="bg-gray-800 rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="font-bold">Detection Pipeline</h3>
            <span className={`px-2 py-0.5 rounded text-xs font-medium ${detHealth.cls}`}>{detHealth.label}</span>
          </div>
          <StatusBreakdown counts={detCounts} onFailedClick={() => navigate('/dashboard?status=failed')} />
          <dl className="text-sm grid grid-cols-2 gap-y-1">
            <dt className="text-gray-400">Avg processing</dt>
            <dd className="text-right">{fmtDuration(avgProc)}</dd>
            <dt className="text-gray-400">Failures</dt>
            <dd className="text-right">{detFailed}</dd>
          </dl>
        </div>

        <div className="bg-gray-800 rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="font-bold">Fine-Tuning Pipeline</h3>
            <span className={`px-2 py-0.5 rounded text-xs font-medium ${trainHealth.cls}`}>{trainHealth.label}</span>
          </div>
          <StatusBreakdown counts={trainCounts} />
          <dl className="text-sm grid grid-cols-2 gap-y-1">
            <dt className="text-gray-400">Models produced</dt>
            <dd className="text-right">{modelsProduced}</dd>
            <dt className="text-gray-400">Avg turnaround</dt>
            <dd className="text-right">{fmtDuration(avgTurn)}</dd>
          </dl>
        </div>

        <div className="bg-gray-800 rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="font-bold">Annotation</h3>
            <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-600">Available</span>
          </div>
          <p className="text-sm text-gray-400">
            3D bounding-box annotation is available in the viewer for completed jobs. Saved annotations feed the
            fine-tuning pipeline.
          </p>
        </div>
      </div>
    </div>
  );
}
