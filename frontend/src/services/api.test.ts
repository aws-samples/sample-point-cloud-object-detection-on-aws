/**
 * Tests for the pure model-selection and bulk-operation logic in services/api.
 *
 * selectableModels decides which weights the detection dropdown offers, and
 * defaultModel which one is selected, so together they decide which weights run
 * inference. The default is always a pretrained model (T34).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';

// Hoisted so the factory below can close over it.
const mockPost = vi.hoisted(() => vi.fn());
const mockGet = vi.hoisted(() => vi.fn());
const mockDelete = vi.hoisted(() => vi.fn());

vi.mock('axios', () => ({
  default: {
    create: () => ({
      post: mockPost,
      get: mockGet,
      delete: mockDelete,
      put: vi.fn(),
      interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    }),
    isAxiosError: (e: unknown) =>
      typeof e === 'object' && e !== null && (e as { isAxiosError?: boolean }).isAxiosError === true,
  },
}));

const {
  selectableModels,
  defaultModel,
  isAvailable,
  DEFAULT_MODEL_ID,
  seg,
  isUuid,
  apiErrorDetail,
  getJob,
  deleteJob,
  getTileData,
  rerunTrainingJobs,
} = await import('./api');
type RegisteredModel = Awaited<
  ReturnType<typeof import('./api').listModels>
>['models'][number];
type TrainingJob = Parameters<typeof rerunTrainingJobs>[0][number];

function pretrained(id: string, mode = 'indoor', available = true): RegisteredModel {
  return {
    model_id: id,
    name: `Pretrained ${id}`,
    type: 'pretrained',
    base_model: id,
    mode,
    checkpoint_path: 'pretrained',
    classes: ['chair'],
    description: '',
    created_at: '2024-01-01T00:00:00Z',
    training_job_id: null,
    available,
    noncommercial: true,
  };
}

function finetuned(
  id: string,
  base: string,
  created: string,
  trainingJobId = `tj-${id}`,
): RegisteredModel {
  return {
    model_id: id,
    name: `Fine-tuned ${id}`,
    type: 'finetuned',
    base_model: base,
    mode: 'custom',
    checkpoint_path: `models/${id}.pth`,
    classes: ['chair'],
    description: '',
    created_at: created,
    training_job_id: trainingJobId,
  };
}

describe('selectableModels', () => {
  it('returns only pretrained models when nothing is fine-tuned', () => {
    const models = [pretrained('votenet_scannet'), pretrained('fcaf3d_scannet')];

    const out = selectableModels(models);

    expect(out.map((m) => m.model_id)).toEqual([
      'votenet_scannet',
      'fcaf3d_scannet',
    ]);
  });

  it('keeps every pretrained model first and offers only the newest fine-tuned one', () => {
    const models = [
      pretrained('votenet_scannet'),
      pretrained('fcaf3d_scannet'),
      finetuned('ft_old', 'votenet_scannet', '2024-05-01T00:00:00Z'),
      finetuned('ft_new', 'votenet_scannet', '2024-06-01T00:00:00Z'),
    ];

    const out = selectableModels(models);

    // T34: the pretrained baseline is no longer hidden or pushed down by a
    // fine-tuned model; older revisions live on the Training Jobs page.
    expect(out.map((m) => m.model_id)).toEqual([
      'votenet_scannet',
      'fcaf3d_scannet',
      'ft_new',
    ]);
  });

  it('keeps the offered set stable across three generations', () => {
    const base = [pretrained('votenet_scannet'), pretrained('fcaf3d_scannet')];
    const gen1 = finetuned('ft_1', 'votenet_scannet', '2024-06-01T00:00:00Z');
    const gen2 = finetuned('ft_2', 'ft_1', '2024-07-01T00:00:00Z');
    const gen3 = finetuned('ft_3', 'ft_2', '2024-08-01T00:00:00Z');

    const lengths = [
      selectableModels([...base, gen1]).length,
      selectableModels([...base, gen1, gen2]).length,
      selectableModels([...base, gen1, gen2, gen3]).length,
    ];

    expect(lengths).toEqual([3, 3, 3]);
  });

  it('tolerates a malformed or unresolvable base_model', () => {
    const models = [
      pretrained('votenet_scannet'),
      finetuned('ft_a', 'ft_b', '2024-06-01T00:00:00Z'),
      finetuned('ft_b', 'ft_a', '2024-07-01T00:00:00Z'),
      finetuned('ft_x', 'model_that_was_deleted', '2024-05-01T00:00:00Z'),
    ];

    expect(selectableModels(models).map((m) => m.model_id)).toEqual([
      'votenet_scannet',
      'ft_b',
    ]);
  });
});

describe('defaultModel', () => {
  it('selects the pretrained default even when a newer fine-tuned model exists', () => {
    // T34: registering a model must not change which weights the next job runs.
    const options = selectableModels([
      pretrained('fcaf3d_scannet'),
      pretrained(DEFAULT_MODEL_ID),
      finetuned('ft_new', DEFAULT_MODEL_ID, '2024-06-01T00:00:00Z'),
    ]);

    expect(defaultModel(options)?.model_id).toBe(DEFAULT_MODEL_ID);
  });

  it('falls back to the first pretrained model when the default is absent', () => {
    const options = [
      finetuned('ft_new', 'fcaf3d_scannet', '2024-06-01T00:00:00Z'),
      pretrained('fcaf3d_scannet'),
    ];

    expect(defaultModel(options)?.model_id).toBe('fcaf3d_scannet');
  });

  it('never selects a fine-tuned model, even when it is the only option', () => {
    const options = [finetuned('ft_only', DEFAULT_MODEL_ID, '2024-06-01T00:00:00Z')];

    expect(defaultModel(options)).toBeUndefined();
  });

  it('does not accept a fine-tuned model registered under the default id', () => {
    const options = [
      finetuned(DEFAULT_MODEL_ID, DEFAULT_MODEL_ID, '2024-06-01T00:00:00Z'),
      pretrained('fcaf3d_scannet'),
    ];

    expect(defaultModel(options)?.model_id).toBe('fcaf3d_scannet');
  });

  // The pretrained weights are non-commercially trained and disabled unless the
  // deployment sets ALLOW_NONCOMMERCIAL_WEIGHTS; the API marks them unavailable.
  it('skips unavailable pretrained models', () => {
    const options = [
      pretrained(DEFAULT_MODEL_ID, 'indoor', false),
      pretrained('fcaf3d_scannet'),
    ];

    expect(defaultModel(options)?.model_id).toBe('fcaf3d_scannet');
  });

  it('selects nothing when every pretrained model is unavailable', () => {
    // A fine-tuned model is still never the default (T34): the user picks it.
    const options = [
      pretrained(DEFAULT_MODEL_ID, 'indoor', false),
      pretrained('fcaf3d_scannet', 'indoor', false),
      finetuned('ft_clean', DEFAULT_MODEL_ID, '2024-06-01T00:00:00Z'),
    ];

    expect(defaultModel(options)).toBeUndefined();
  });
});

describe('isAvailable', () => {
  it('reads the registry flag, treating its absence as available', () => {
    const legacy = pretrained(DEFAULT_MODEL_ID);
    delete legacy.available;

    expect(isAvailable(pretrained(DEFAULT_MODEL_ID, 'indoor', false))).toBe(false);
    expect(isAvailable(pretrained(DEFAULT_MODEL_ID))).toBe(true);
    expect(isAvailable(legacy)).toBe(true);
    expect(isAvailable(finetuned('ft', DEFAULT_MODEL_ID, '2024-06-01T00:00:00Z'))).toBe(true);
  });
});

describe('seg and isUuid (T56)', () => {
  it('encodes characters that would add a segment or a query', () => {
    expect(seg('a/b?c#d')).toBe('a%2Fb%3Fc%23d');
    expect(seg('..%2F')).toBe('..%252F');
  });

  it.each(['', '.', '..'])('refuses %j, which the URL parser would resolve', (v) => {
    expect(() => seg(v)).toThrow(/Not a valid id/);
  });

  it('interpolates ids encoded into every path', async () => {
    mockGet.mockResolvedValue({ data: {} });
    mockDelete.mockResolvedValue({ data: {} });

    await getJob('../training');
    await deleteJob('x?y');
    await getTileData('j', '../../jobs');

    expect(mockGet).toHaveBeenCalledWith('/jobs/..%2Ftraining');
    expect(mockDelete).toHaveBeenCalledWith('/jobs/x%3Fy');
    expect(mockGet).toHaveBeenCalledWith('/results/j/tiles/..%2F..%2Fjobs', {
      responseType: 'arraybuffer',
    });
  });

  it('accepts only UUIDs as navigation targets', () => {
    expect(isUuid('3f2b8c1e-9a4d-4c3b-8e2f-1a2b3c4d5e6f')).toBe(true);
    expect(isUuid('3F2B8C1E-9A4D-4C3B-8E2F-1A2B3C4D5E6F')).toBe(true);
    for (const v of [
      '../dashboard',
      '3f2b8c1e-9a4d-4c3b-8e2f-1a2b3c4d5e6f/../x',
      'https://example.com',
      '',
      null,
      undefined,
      42,
    ]) {
      expect(isUuid(v)).toBe(false);
    }
  });
});

describe('apiErrorDetail', () => {
  it("returns the API's string detail", () => {
    const err = { isAxiosError: true, response: { data: { detail: 'Job is queued.' } } };
    expect(apiErrorDetail(err, 'fallback')).toBe('Job is queued.');
  });

  it('falls back for validation lists, network errors and non-axios errors', () => {
    expect(apiErrorDetail({ isAxiosError: true, response: { data: { detail: [{}] } } }, 'f')).toBe('f');
    expect(apiErrorDetail({ isAxiosError: true }, 'f')).toBe('f');
    expect(apiErrorDetail(new Error('boom'), 'f')).toBe('f');
  });
});

describe('rerunTrainingJobs', () => {
  function job(over: Partial<TrainingJob> = {}): TrainingJob {
    return {
      training_job_id: 'tj-1',
      status: 'complete',
      job_id: 'src-1',
      base_model: 'votenet_scannet',
      epochs: 10,
      custom_classes: ['chair', 'table'],
      ...over,
    };
  }

  beforeEach(() => {
    mockPost.mockReset();
  });

  it('forwards the original class list so a re-run reproduces the run', async () => {
    // Sending [] produced a 0-class model that was not a reproduction, and if it
    // landed newest it became the detection default.
    mockPost.mockResolvedValue({ data: job({ training_job_id: 'tj-new' }) });

    const res = await rerunTrainingJobs([job()]);

    expect(res.submitted).toHaveLength(1);
    expect(mockPost).toHaveBeenCalledWith(
      '/training',
      expect.objectContaining({ custom_classes: ['chair', 'table'] }),
    );
  });

  it('reports a job with no source detection job as skipped, not failed', async () => {
    const res = await rerunTrainingJobs([job({ job_id: null })]);

    expect(res.skipped).toHaveLength(1);
    expect(res.failed).toHaveLength(0);
    expect(res.submitted).toHaveLength(0);
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('reports an HTTP failure as failed, not as a missing source job', async () => {
    // Both used to land in one `skipped` array labelled "no source detection job
    // to retrain from", which actively misleads whoever is diagnosing a batch.
    mockPost.mockRejectedValue(new Error('500'));

    const res = await rerunTrainingJobs([job()]);

    expect(res.failed).toHaveLength(1);
    expect(res.skipped).toHaveLength(0);
  });

  it('separates skipped from failed within one batch', async () => {
    mockPost
      .mockResolvedValueOnce({ data: job({ training_job_id: 'ok' }) })
      .mockRejectedValueOnce(new Error('500'));

    const res = await rerunTrainingJobs([
      job({ training_job_id: 'a' }),
      job({ training_job_id: 'b' }),
      job({ training_job_id: 'c', job_id: null }),
    ]);

    expect(res.submitted).toHaveLength(1);
    expect(res.failed.map((t) => t.training_job_id)).toEqual(['b']);
    expect(res.skipped.map((t) => t.training_job_id)).toEqual(['c']);
  });
});
