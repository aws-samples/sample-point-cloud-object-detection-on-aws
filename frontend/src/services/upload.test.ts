import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { uploadFile } from './upload';
import * as api from './api';

/**
 * These tests exist because the original uploadPart loop checked neither
 * `response.ok` nor the presence of the ETag header. It read
 * `response.headers.get('ETag') || ''`, so a 403 on an expired pre-signed URL or a 5xx
 * from S3 produced an empty ETag, pushed it as a legitimate part, and let the upload
 * "succeed" all the way to complete-upload, which then failed with an opaque S3 error
 * after every byte had already been sent.
 *
 * Timers are faked throughout: the retry path backs off exponentially, so on real
 * timers the give-up test alone would sit for several seconds.
 */

const PART_SIZE = 8;
const S3_KEY = 'uploads/abc/scene.las';

function fakeFile(parts: number): File {
  // jsdom's Blob.slice is real, so a Uint8Array of the right length makes the chunking
  // arithmetic in uploadFile observable.
  return new File([new Uint8Array(PART_SIZE * parts)], 'scene.las', {
    type: 'application/octet-stream',
  });
}

function partResponse(status: number, etag: string | null): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `status ${status}`,
    headers: { get: (name: string) => (name === 'ETag' ? etag : null) },
  } as unknown as Response;
}

/** Point initiateUpload at exactly `count` parts, matching the file under test. */
function mockInitiate(count: number): void {
  vi.spyOn(api, 'initiateUpload').mockResolvedValue({
    upload_id: 'upload-1',
    s3_key: S3_KEY,
    part_size_bytes: PART_SIZE,
    total_parts: count,
    part_urls: Array.from({ length: count }, (_, i) => ({
      part_number: i + 1,
      presigned_url: `https://s3.example/part${i + 1}`,
    })),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any);
}

/**
 * Drive a promise to settlement while advancing fake timers past any backoff.
 *
 * The outcome is captured with a two-branch `then` rather than a `catch` that re-throws.
 * Re-throwing produces a rejected promise whose handler is only attached after
 * runAllTimersAsync returns, which node reports as an unhandled rejection that was
 * "handled asynchronously" -- six of them, drowning the real output. Here both branches
 * are handled immediately, so nothing is ever transiently unhandled.
 */
async function settle<T>(promise: Promise<T>): Promise<T> {
  let outcome: { ok: true; value: T } | { ok: false; error: unknown } | undefined;
  const recorded = promise.then(
    (value) => {
      outcome = { ok: true, value };
    },
    (error: unknown) => {
      outcome = { ok: false, error };
    },
  );
  await vi.runAllTimersAsync();
  await recorded;
  if (!outcome) throw new Error('promise did not settle');
  if (outcome.ok) return outcome.value;
  throw outcome.error;
}

beforeEach(() => {
  vi.useFakeTimers();
  mockInitiate(1);
  vi.spyOn(api, 'completeUpload').mockResolvedValue({ s3_key: S3_KEY });
  vi.spyOn(api, 'abortUpload').mockResolvedValue({ status: 'aborted' });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('uploadFile part handling', () => {
  it('completes with the ETags S3 returned', async () => {
    mockInitiate(2);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(partResponse(200, '"etag-value"')));

    const progress: number[] = [];
    const result = await settle(
      uploadFile(fakeFile(2), (p) => progress.push(p.percent)),
    );

    expect(result.s3Key).toBe(S3_KEY);
    expect(progress).toEqual([50, 100]);
    // Quotes stripped, and both parts reported.
    expect(api.completeUpload).toHaveBeenCalledWith('upload-1', S3_KEY, [
      { part_number: 1, etag: 'etag-value' },
      { part_number: 2, etag: 'etag-value' },
    ]);
  });

  it('does not complete the upload when a part returns a non-2xx status', async () => {
    // The regression this file exists for: previously this resolved successfully,
    // having sent an empty ETag for the failed part.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(partResponse(403, null)));

    await expect(settle(uploadFile(fakeFile(1), () => {}))).rejects.toThrow(/HTTP 403/);
    expect(api.completeUpload).not.toHaveBeenCalled();
  });

  it('does not complete the upload when the ETag header is unreadable', async () => {
    // A 200 with no exposed ETag is the CORS misconfiguration case, and is just as
    // unusable as a hard failure -- S3 rejects a blank ETag in the part list.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(partResponse(200, null)));

    await expect(settle(uploadFile(fakeFile(1), () => {}))).rejects.toThrow(
      /no readable ETag/,
    );
    expect(api.completeUpload).not.toHaveBeenCalled();
  });

  it('retries a transient failure and then succeeds', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(partResponse(500, null))
      .mockResolvedValue(partResponse(200, '"recovered"'));
    vi.stubGlobal('fetch', fetchMock);

    const result = await settle(uploadFile(fakeFile(1), () => {}));

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.s3Key).toBe(S3_KEY);
    expect(api.completeUpload).toHaveBeenCalledWith('upload-1', S3_KEY, [
      { part_number: 1, etag: 'recovered' },
    ]);
  });

  it('gives up after a bounded number of attempts rather than retrying forever', async () => {
    const fetchMock = vi.fn().mockResolvedValue(partResponse(500, null));
    vi.stubGlobal('fetch', fetchMock);

    await expect(settle(uploadFile(fakeFile(1), () => {}))).rejects.toThrow(
      /after 4 attempts/,
    );
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it('aborts the multipart upload when a part fails, so parts are not left billable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(partResponse(403, null)));

    await expect(settle(uploadFile(fakeFile(1), () => {}))).rejects.toThrow();
    expect(api.abortUpload).toHaveBeenCalledWith('upload-1', S3_KEY);
  });

  it('surfaces the original error even if the cleanup abort also fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(partResponse(403, null)));
    vi.spyOn(api, 'abortUpload').mockRejectedValue(new Error('abort endpoint down'));

    await expect(settle(uploadFile(fakeFile(1), () => {}))).rejects.toThrow(/HTTP 403/);
  });

  it('stops immediately when the caller aborts, without retrying', async () => {
    mockInitiate(2);
    const controller = new AbortController();
    const fetchMock = vi.fn().mockImplementation(() => {
      controller.abort();
      return Promise.reject(new DOMException('Upload aborted', 'AbortError'));
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      settle(uploadFile(fakeFile(2), () => {}, controller.signal)),
    ).rejects.toThrow(/aborted/i);
    // One attempt only: an AbortError is final, not a transient failure to retry.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
