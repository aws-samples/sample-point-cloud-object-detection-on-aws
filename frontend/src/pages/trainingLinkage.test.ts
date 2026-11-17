import { describe, it, expect } from 'vitest';
import type { Job, TrainingJob } from '../services/api';
import {
  VERSION_HISTORY_LIMIT,
  linkTrainingJobs,
  versionSummary,
} from './trainingLinkage';

function job(job_id: string, opts: { jobType?: 'detection' | 'annotation'; status?: Job['status'] } = {}): Job {
  return {
    job_id,
    filename: `${job_id}.ply`,
    s3_input_key: `uploads/${job_id}.ply`,
    status: opts.status ?? 'complete',
    created_at: '2026-01-01T00:00:00Z',
    started_at: null,
    completed_at: null,
    detection_count: null,
    tile_count: null,
    processing_time_s: null,
    error_message: null,
    config: {
      model_mode: 'indoor',
      confidence_threshold: 0.5,
      use_color: false,
      use_intensity: false,
      use_normals: false,
      coord_frame: 'z_up',
      downsample_voxel_m: 0.05,
      max_points: null,
      custom_weights_version: null,
      raw_mode: false,
      device_preference: 'auto',
      job_type: opts.jobType ?? 'detection',
    },
  };
}

function run(training_job_id: string, job_id: string | null, created_at: string): TrainingJob {
  return { training_job_id, job_id, status: 'complete', created_at };
}

describe('linkTrainingJobs', () => {
  it('groups runs under the detection job they were run on', () => {
    const a = job('job-a');
    const { trainingByJob, fromAnnotation, hiddenByFilter, orphanedTraining } = linkTrainingJobs({
      allJobs: [a],
      filteredJobs: [a],
      trainingJobs: [run('t1', 'job-a', '2026-01-01T00:00:00Z')],
    });

    expect(trainingByJob.get('job-a')?.map((t) => t.training_job_id)).toEqual(['t1']);
    expect(fromAnnotation).toEqual([]);
    expect(hiddenByFilter).toEqual([]);
    expect(orphanedTraining).toEqual([]);
  });

  it('orders a file\u2019s revisions newest first', () => {
    const a = job('job-a');
    const { trainingByJob } = linkTrainingJobs({
      allJobs: [a],
      filteredJobs: [a],
      trainingJobs: [
        run('older', 'job-a', '2026-01-01T00:00:00Z'),
        run('newest', 'job-a', '2026-03-01T00:00:00Z'),
        run('middle', 'job-a', '2026-02-01T00:00:00Z'),
      ],
    });

    expect(trainingByJob.get('job-a')?.map((t) => t.training_job_id)).toEqual([
      'newest',
      'middle',
      'older',
    ]);
  });

  // The bug this guards: the parent lookup used to be built from filteredJobs, so
  // any status filter moved well-linked runs into the "no matching file" bucket.
  it('reports a filtered-out parent as hidden, not orphaned', () => {
    const visible = job('job-visible', { status: 'complete' });
    const hidden = job('job-hidden', { status: 'failed' });

    const { trainingByJob, hiddenByFilter, orphanedTraining } = linkTrainingJobs({
      allJobs: [visible, hidden],
      filteredJobs: [visible], // user selected the "complete" filter
      trainingJobs: [
        run('t-visible', 'job-visible', '2026-01-01T00:00:00Z'),
        run('t-hidden', 'job-hidden', '2026-01-01T00:00:00Z'),
      ],
    });

    expect(trainingByJob.get('job-visible')?.map((t) => t.training_job_id)).toEqual(['t-visible']);
    expect(hiddenByFilter.map((t) => t.training_job_id)).toEqual(['t-hidden']);
    expect(orphanedTraining).toEqual([]);
  });

  // The other half: annotation jobs are never rendered on this page, so a run
  // parented by one has no row. That is not the same as having no source file.
  it('attributes an annotation-parented run rather than calling it orphaned', () => {
    const detection = job('job-d');
    const annotation = job('job-ann', { jobType: 'annotation' });

    const { fromAnnotation, hiddenByFilter, orphanedTraining } = linkTrainingJobs({
      allJobs: [detection, annotation],
      filteredJobs: [detection], // annotation jobs are excluded from the rows
      trainingJobs: [run('t-ann', 'job-ann', '2026-01-01T00:00:00Z')],
    });

    expect(fromAnnotation.map((t) => t.training_job_id)).toEqual(['t-ann']);
    expect(hiddenByFilter).toEqual([]);
    expect(orphanedTraining).toEqual([]);
  });

  it('treats a run whose parent no longer exists as orphaned', () => {
    const a = job('job-a');
    const { orphanedTraining } = linkTrainingJobs({
      allJobs: [a],
      filteredJobs: [a],
      trainingJobs: [run('t-gone', 'job-deleted', '2026-01-01T00:00:00Z')],
    });

    expect(orphanedTraining.map((t) => t.training_job_id)).toEqual(['t-gone']);
  });

  it('treats a run with no parent id at all as orphaned', () => {
    const { orphanedTraining } = linkTrainingJobs({
      allJobs: [],
      filteredJobs: [],
      trainingJobs: [run('t-null', null, '2026-01-01T00:00:00Z')],
    });

    expect(orphanedTraining.map((t) => t.training_job_id)).toEqual(['t-null']);
  });

  it('accounts for every run exactly once', () => {
    const visible = job('job-visible');
    const hidden = job('job-hidden', { status: 'failed' });
    const annotation = job('job-ann', { jobType: 'annotation' });
    const trainingJobs = [
      run('t1', 'job-visible', '2026-01-01T00:00:00Z'),
      run('t2', 'job-visible', '2026-02-01T00:00:00Z'),
      run('t3', 'job-hidden', '2026-01-01T00:00:00Z'),
      run('t4', 'job-ann', '2026-01-01T00:00:00Z'),
      run('t5', 'job-deleted', '2026-01-01T00:00:00Z'),
      run('t6', null, '2026-01-01T00:00:00Z'),
    ];

    const linkage = linkTrainingJobs({
      allJobs: [visible, hidden, annotation],
      filteredJobs: [visible],
      trainingJobs,
    });

    const bucketed = [
      ...[...linkage.trainingByJob.values()].flat(),
      ...linkage.fromAnnotation,
      ...linkage.hiddenByFilter,
      ...linkage.orphanedTraining,
    ].map((t) => t.training_job_id);

    expect(bucketed.sort()).toEqual(trainingJobs.map((t) => t.training_job_id).sort());
  });
});

describe('versionSummary', () => {
  const revisions = (n: number) =>
    Array.from({ length: n }, (_, i) =>
      run(`t${i}`, 'job-a', `2026-01-0${i + 1}T00:00:00Z`),
    );

  it('has no latest and no badge for a file with no runs', () => {
    const s = versionSummary([]);
    expect(s.latest).toBeUndefined();
    expect(s.previousCount).toBe(0);
    expect(s.truncated).toBe(false);
    expect(s.history).toEqual([]);
  });

  it('shows no badge for a single run', () => {
    const s = versionSummary(revisions(1));
    expect(s.latest?.training_job_id).toBe('t0');
    expect(s.previousCount).toBe(0);
    expect(s.truncated).toBe(false);
  });

  // The bug this guards: the badge was Math.min(total, LIMIT) - 1, so a file with
  // 5 revisions advertised "+2 prev" instead of "+4 prev" - the cap on the
  // rendered list was leaking into the count of what exists.
  it('counts every superseded revision, not just the rendered ones', () => {
    const total = VERSION_HISTORY_LIMIT + 2;
    const s = versionSummary(revisions(total));

    expect(s.total).toBe(total);
    expect(s.previousCount).toBe(total - 1);
    expect(s.previousCount).toBeGreaterThan(s.history.length - 1);
  });

  it('caps the rendered history at the limit and flags the truncation', () => {
    const s = versionSummary(revisions(VERSION_HISTORY_LIMIT + 2));
    expect(s.history).toHaveLength(VERSION_HISTORY_LIMIT);
    expect(s.truncated).toBe(true);
  });

  it('does not flag truncation when the history fits exactly', () => {
    const s = versionSummary(revisions(VERSION_HISTORY_LIMIT));
    expect(s.history).toHaveLength(VERSION_HISTORY_LIMIT);
    expect(s.truncated).toBe(false);
    expect(s.previousCount).toBe(VERSION_HISTORY_LIMIT - 1);
  });

  it('keeps previousCount equal to total-1 at every size', () => {
    for (let n = 1; n <= VERSION_HISTORY_LIMIT + 5; n++) {
      expect(versionSummary(revisions(n)).previousCount).toBe(n - 1);
    }
  });
});
