/**
 * Attribution of fine-tuning runs to the detection job they were run on.
 *
 * Extracted from Dashboard so the bucketing and the caret arithmetic can be
 * asserted directly. Both were wrong in ways rendering could not reveal: the
 * parent lookup was built from the *filtered* rows, so applying a status filter
 * silently reclassified well-linked runs as having "no matching file", and the
 * caret badge counted the truncated history rather than the real revisions.
 */
import type { Job, TrainingJob } from '../services/api';

/** How many fine-tuning revisions the caret reveals, newest first. */
export const VERSION_HISTORY_LIMIT = 3;

export interface LinkageInput {
  /**
   * Every job the API returned, annotation jobs included. Fine-tuning started
   * from the training-mode upload flow is parented by an annotation job, which
   * this page never renders — dropping those from the lookup universe left such
   * runs permanently unattributable.
   */
  allJobs: Job[];
  /** The subset rendered as rows, i.e. after the status filter. */
  filteredJobs: Job[];
  trainingJobs: TrainingJob[];
}

export interface Linkage {
  /** Runs that sit under a visible row, newest first. */
  trainingByJob: Map<string, TrainingJob[]>;
  /** Parent is an annotation job, so it has no row on this page. */
  fromAnnotation: TrainingJob[];
  /** Parent exists but the status filter is hiding it. */
  hiddenByFilter: TrainingJob[];
  /** No parent job exists at all. */
  orphanedTraining: TrainingJob[];
}

/**
 * Bucket every fine-tuning run by *why* it is or is not shown under a row.
 *
 * The three unlinked buckets are kept separate because they need different copy:
 * "hidden by your filter" is a display artefact the user can undo, while "no
 * matching file at all" points at deleted source data.
 */
export function linkTrainingJobs({
  allJobs,
  filteredJobs,
  trainingJobs,
}: LinkageInput): Linkage {
  const visibleIds = new Set(filteredJobs.map((j) => j.job_id));
  const allIds = new Set(allJobs.map((j) => j.job_id));
  const annotationIds = new Set(
    allJobs.filter((j) => j.config?.job_type === 'annotation').map((j) => j.job_id),
  );

  const trainingByJob = new Map<string, TrainingJob[]>();
  const fromAnnotation: TrainingJob[] = [];
  const hiddenByFilter: TrainingJob[] = [];
  const orphanedTraining: TrainingJob[] = [];

  for (const t of trainingJobs) {
    if (t.job_id && visibleIds.has(t.job_id)) {
      const arr = trainingByJob.get(t.job_id) ?? [];
      arr.push(t);
      trainingByJob.set(t.job_id, arr);
    } else if (t.job_id && annotationIds.has(t.job_id)) {
      fromAnnotation.push(t);
    } else if (t.job_id && allIds.has(t.job_id)) {
      hiddenByFilter.push(t);
    } else {
      orphanedTraining.push(t);
    }
  }

  // Newest first, so index 0 is the current version of the model for that file.
  for (const arr of trainingByJob.values()) {
    arr.sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));
  }

  return { trainingByJob, fromAnnotation, hiddenByFilter, orphanedTraining };
}

export interface VersionSummary {
  /** Current version for this file, or undefined when there are no runs. */
  latest?: TrainingJob;
  /** The revisions the expanded caret renders. */
  history: TrainingJob[];
  /**
   * Revisions older than `latest`. This is the caret badge count, and it counts
   * every superseded revision — not just the ones `history` happens to show.
   */
  previousCount: number;
  /** True when `history` omits revisions, so the row needs a "showing N of M". */
  truncated: boolean;
  /** Total revisions for this file. */
  total: number;
}

/**
 * Summarise a file's revision list for the caret badge and its header.
 *
 * `previousCount` is deliberately derived from the total rather than from
 * `history`: capping the rendered list must not change how many revisions the
 * badge claims exist.
 */
export function versionSummary(linked: TrainingJob[]): VersionSummary {
  const total = linked.length;
  return {
    latest: linked[0],
    history: linked.slice(0, VERSION_HISTORY_LIMIT),
    previousCount: Math.max(total - 1, 0),
    truncated: total > VERSION_HISTORY_LIMIT,
    total,
  };
}
