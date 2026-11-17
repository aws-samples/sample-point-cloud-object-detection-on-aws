/**
 * State + persistence for annotation mode.
 *
 * Owns the editable box list, label set, selection, and dirty tracking, and
 * handles load/save against the annotation API. Component-local state only
 * (consistent with the codebase); training-status polling lives in
 * TrainingDialog via React Query.
 */
import { useCallback, useMemo, useRef, useState } from 'react';
import {
  getLabels,
  getAnnotations,
  saveAnnotations,
  getJob,
  LabelDef,
  AnnotationSet,
} from '../../services/api';
import { classColorHex } from '../classColors';
import type { AnnotationBox } from './types';

/** Generate a UUID that works in both HTTPS (secure) and HTTP contexts. */
function generateId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // Fallback for HTTP (insecure) contexts where randomUUID is unavailable
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export interface UseAnnotations {
  boxes: AnnotationBox[];
  selectedId: string | null;
  labels: LabelDef[];
  activeLabel: string;
  dirty: boolean;
  loading: boolean;
  loadError: string | null;
  saving: boolean;
  saveError: string | null;
  usedClasses: string[];
  select: (id: string | null) => void;
  setActiveLabel: (name: string) => void;
  addBox: (box: Omit<AnnotationBox, 'id'>) => string;
  updateBox: (id: string, patch: Partial<Omit<AnnotationBox, 'id'>>) => void;
  removeBox: (id: string) => void;
  addCustomLabel: (name: string) => void;
  labelColor: (name: string) => string;
  defaultSizeFor: (name: string) => [number, number, number];
  load: () => Promise<void>;
  save: () => Promise<void>;
}

export function useAnnotations(jobId: string | undefined): UseAnnotations {
  const [boxes, setBoxes] = useState<AnnotationBox[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [labels, setLabels] = useState<LabelDef[]>([]);
  const [activeLabel, setActiveLabelState] = useState<string>('');
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const jobMetaRef = useRef<{ s3_input_key: string; filename: string } | null>(null);

  const load = useCallback(async () => {
    if (!jobId) return;
    setLoading(true);
    setLoadError(null);
    try {
      const [labelResp, existing, job] = await Promise.all([
        getLabels(jobId),
        getAnnotations(jobId),
        getJob(jobId),
      ]);
      setLabels(labelResp.labels);
      setActiveLabelState((prev) => prev || labelResp.labels[0]?.name || '');
      jobMetaRef.current = { s3_input_key: job.s3_input_key, filename: job.filename };
      setBoxes(
        existing
          ? existing.annotations.map((a) => ({
              id: generateId(),
              label: a.class_name,
              center: a.center_xyz,
              size: a.size_wlh,
              heading: a.heading,
            }))
          : [],
      );
      setDirty(false);
    } catch {
      setLoadError('Failed to load annotation data. Labels default to none; you can retry.');
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  const select = useCallback((id: string | null) => setSelectedId(id), []);
  const setActiveLabel = useCallback((name: string) => setActiveLabelState(name), []);

  const addBox = useCallback((box: Omit<AnnotationBox, 'id'>) => {
    const id = generateId();
    setBoxes((b) => [...b, { ...box, id }]);
    setSelectedId(id);
    setDirty(true);
    return id;
  }, []);

  const updateBox = useCallback((id: string, patch: Partial<Omit<AnnotationBox, 'id'>>) => {
    setBoxes((b) => b.map((x) => (x.id === id ? { ...x, ...patch } : x)));
    setDirty(true);
  }, []);

  const removeBox = useCallback((id: string) => {
    setBoxes((b) => b.filter((x) => x.id !== id));
    setSelectedId((s) => (s === id ? null : s));
    setDirty(true);
  }, []);

  const addCustomLabel = useCallback((name: string) => {
    const trimmed = name.trim();
    if (!trimmed) return;
    setLabels((prev) =>
      prev.some((l) => l.name === trimmed)
        ? prev
        : [...prev, { name: trimmed, color: classColorHex(trimmed), default_size: [1, 1, 1] }],
    );
    setActiveLabelState(trimmed);
  }, []);

  const labelColor = useCallback(
    (name: string) => labels.find((l) => l.name === name)?.color ?? classColorHex(name),
    [labels],
  );

  const defaultSizeFor = useCallback(
    (name: string): [number, number, number] =>
      labels.find((l) => l.name === name)?.default_size ?? [1, 1, 1],
    [labels],
  );

  const usedClasses = useMemo(() => Array.from(new Set(boxes.map((b) => b.label))), [boxes]);

  const save = useCallback(async () => {
    if (!jobId) return;
    setSaving(true);
    setSaveError(null);
    const meta = jobMetaRef.current;
    const sceneId = meta?.filename ? meta.filename.replace(/\.[^.]+$/, '') : jobId;
    const set: AnnotationSet = {
      scene_id: sceneId,
      point_cloud_path: meta?.s3_input_key ?? '',
      num_annotations: boxes.length,
      annotations: boxes.map((b) => ({
        center_xyz: b.center,
        size_wlh: b.size,
        heading: b.heading,
        class_name: b.label,
      })),
    };
    try {
      await saveAnnotations(jobId, set);
      setDirty(false);
    } catch (e) {
      setSaveError('Failed to save annotations. Your boxes are preserved — retry.');
      throw e;
    } finally {
      setSaving(false);
    }
  }, [jobId, boxes]);

  return {
    boxes,
    selectedId,
    labels,
    activeLabel,
    dirty,
    loading,
    loadError,
    saving,
    saveError,
    usedClasses,
    select,
    setActiveLabel,
    addBox,
    updateBox,
    removeBox,
    addCustomLabel,
    labelColor,
    defaultSizeFor,
    load,
    save,
  };
}
