/**
 * Multipart upload service — chunked upload directly to S3 via pre-signed URLs.
 *
 * On scope: this path is for interactive browser uploads, which Upload.tsx caps at
 * MAX_FILE_SIZE_MB. Multi-gigabyte scenes are staged straight to the bucket instead
 * (see docs/EKS_DEPLOY_RUNBOOK.md), because a browser upload large enough to matter
 * would outlive the pre-signed URLs it started with: they expire after
 * Settings.presigned_url_expiry, one hour by default. Refreshing part URLs mid-flight
 * would be needed to lift that, and there is no endpoint for it.
 */
import { initiateUpload, completeUpload, abortUpload } from './api';

export interface UploadProgress {
  loaded: number;
  total: number;
  percent: number;
  partsCompleted: number;
  totalParts: number;
}

/** Retries per part, on top of the initial attempt. */
const MAX_PART_ATTEMPTS = 4;
/** Base for exponential backoff between part attempts, in milliseconds. */
const RETRY_BASE_MS = 500;

function backoffMs(attempt: number): number {
  // Exponential, with jitter so concurrent parts failing together do not retry in
  // lockstep and reproduce the same burst that caused the failure.
  const exponential = RETRY_BASE_MS * 2 ** (attempt - 1);
  return exponential + Math.random() * RETRY_BASE_MS;
}

const delay = (ms: number, signal?: AbortSignal): Promise<void> =>
  new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Upload aborted', 'AbortError'));
      return;
    }
    // The listener is removed on both exits. Leaving it attached would accumulate one
    // listener per retry for the lifetime of the signal, and each would later call
    // reject() on an already-settled promise when the upload was eventually cancelled.
    //
    // The timer is declared before onAbort so it can be const; the timeout callback
    // referring to onAbort is fine, since it cannot run before this function returns.
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException('Upload aborted', 'AbortError'));
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });

/**
 * PUT one part, retrying transient failures, and return its ETag.
 *
 * The ETag is required: S3's CompleteMultipartUpload rejects a part list with a blank
 * one. An earlier version of this function checked neither response.ok nor the header,
 * so a 403 on an expired URL or a 5xx produced `''` from the `|| ''` fallback, got
 * pushed as a legitimate part, and surfaced much later as an opaque failure from
 * complete-upload -- with every byte already re-sent. Both are now hard errors here,
 * where the part number is still in hand.
 */
async function uploadPart(
  url: string,
  partNumber: number,
  chunk: Blob,
  signal?: AbortSignal,
): Promise<string> {
  let lastError: Error | undefined;

  for (let attempt = 1; attempt <= MAX_PART_ATTEMPTS; attempt++) {
    if (signal?.aborted) {
      throw new DOMException('Upload aborted', 'AbortError');
    }
    try {
      const response = await fetch(url, {
        method: 'PUT',
        body: chunk,
        headers: { 'Content-Type': 'application/octet-stream' },
        signal,
      });

      if (!response.ok) {
        throw new Error(
          `Part ${partNumber} failed: HTTP ${response.status} ${response.statusText}`,
        );
      }

      const etag = response.headers.get('ETag');
      if (!etag) {
        // Cross-origin reads of ETag require the bucket's CORS to list it under
        // ExposeHeaders. Without that the browser hides it and the upload cannot be
        // completed, so say so rather than sending a blank ETag.
        throw new Error(
          `Part ${partNumber} returned no readable ETag. The bucket's CORS ` +
            `configuration must expose the ETag header.`,
        );
      }
      return etag.replace(/"/g, '');
    } catch (err) {
      // A caller-driven abort is final; retrying it would ignore the cancellation.
      if (err instanceof DOMException && err.name === 'AbortError') throw err;
      lastError = err instanceof Error ? err : new Error(String(err));
      if (attempt < MAX_PART_ATTEMPTS) {
        await delay(backoffMs(attempt), signal);
      }
    }
  }

  throw new Error(
    `Part ${partNumber} failed after ${MAX_PART_ATTEMPTS} attempts: ${lastError?.message}`,
  );
}

export async function uploadFile(
  file: File,
  onProgress: (progress: UploadProgress) => void,
  signal?: AbortSignal,
): Promise<{ s3Key: string }> {
  // Step 1: Initiate multipart upload
  const init = await initiateUpload(file.name, file.size);
  const { upload_id, s3_key, part_size_bytes, part_urls } = init;

  let totalUploaded = 0;
  const parts: { part_number: number; etag: string }[] = [];

  try {
    // Step 2: Upload each part directly to S3
    for (const partInfo of part_urls) {
      const start = (partInfo.part_number - 1) * part_size_bytes;
      const end = Math.min(start + part_size_bytes, file.size);
      const chunk = file.slice(start, end);

      const etag = await uploadPart(
        partInfo.presigned_url,
        partInfo.part_number,
        chunk,
        signal,
      );
      parts.push({ part_number: partInfo.part_number, etag });

      totalUploaded += chunk.size;
      onProgress({
        loaded: totalUploaded,
        total: file.size,
        percent: Math.round((totalUploaded / file.size) * 100),
        partsCompleted: parts.length,
        totalParts: part_urls.length,
      });
    }

    // Step 3: Complete multipart upload
    await completeUpload(upload_id, s3_key, parts);
  } catch (err) {
    // Leaving the upload open would keep the uploaded parts billable indefinitely, so
    // clean up. Best-effort: the original failure is what the caller needs to see, and
    // an abort that also fails must not replace it.
    try {
      await abortUpload(upload_id, s3_key);
    } catch {
      /* preserve the original error */
    }
    throw err;
  }

  return { s3Key: s3_key };
}
