import { useEffect, useState, useRef, useMemo, useCallback, ElementRef } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import { Canvas, useThree } from '@react-three/fiber';
import { OrbitControls, Grid, PerspectiveCamera, OrthographicCamera } from '@react-three/drei';
import * as THREE from 'three';
import {
  getDetections,
  getTileIndex,
  getTileData,
  getSegmentation,
  TileIndex,
  PointFormat,
} from '../services/api';
import { classColorHex } from '../viewer/classColors';
import {
  parseTile,
  parseSegmentation,
  computeColors,
  segmentColor,
  discTexture,
  ColorMode,
  Detection,
  PointData,
} from '../viewer/pointCloud';
import { EDL } from '../viewer/EDL';
import { useAnnotations } from '../viewer/annotation/useAnnotations';
import { AnnotationLayer, type TransformMode } from '../viewer/annotation/AnnotationLayer';
import { RegionSelect } from '../viewer/annotation/RegionSelect';
import { AnnotationSidebar } from '../viewer/annotation/AnnotationSidebar';
import { TrainingDialog } from '../viewer/annotation/TrainingDialog';
import type { PixelRect } from '../viewer/annotation/fitBox';

type CameraPreset = 'iso' | 'top' | 'front' | 'side';

const PRESET_POSITIONS: Record<CameraPreset, [number, number, number]> = {
  iso: [22, 22, 16],
  top: [0, 0.001, 40],
  front: [0, -40, 6],
  side: [40, 0, 6],
};

// --- 3D primitives ---

function PointCloudMesh({ positions, colors }: { positions: Float32Array; colors: Float32Array }) {
  const geometry = useMemo(() => {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geo.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
    geo.computeBoundingSphere();
    return geo;
  }, [positions, colors]);

  return (
    <points geometry={geometry}>
      <pointsMaterial
        size={0.09}
        vertexColors
        sizeAttenuation
        map={discTexture()}
        alphaTest={0.5}
        transparent
      />
    </points>
  );
}

function BoundingBox({
  detection,
  selected,
  dimmed,
  onClick,
}: {
  detection: Detection;
  selected: boolean;
  dimmed: boolean;
  onClick: () => void;
}) {
  const { center, size, heading } = detection.bbox_obb;
  const boxGeo = useMemo(() => new THREE.BoxGeometry(size[0], size[1], size[2]), [size]);
  const edgeGeo = useMemo(() => new THREE.EdgesGeometry(boxGeo), [boxGeo]);
  const color = selected ? '#fbbf24' : classColorHex(detection.label);

  return (
    <group position={[center[0], center[1], center[2]]} rotation={[0, 0, heading]}>
      <mesh geometry={boxGeo} onClick={(e) => { e.stopPropagation(); onClick(); }}>
        <meshBasicMaterial
          color={color}
          transparent
          opacity={selected ? 0.28 : dimmed ? 0.04 : 0.1}
          depthWrite={false}
        />
      </mesh>
      <lineSegments geometry={edgeGeo}>
        <lineBasicMaterial color={color} transparent opacity={dimmed ? 0.25 : 1} />
      </lineSegments>
    </group>
  );
}

function PresetCamera({ preset }: { preset: CameraPreset }) {
  const { camera } = useThree();
  useEffect(() => {
    const pos = PRESET_POSITIONS[preset];
    camera.position.set(pos[0], pos[1], pos[2]);
    camera.up.set(0, 0, 1);
    camera.lookAt(0, 0, 2);
  }, [preset, camera]);
  return null;
}

function FocusController({ target, enabled }: { target: number[] | null; enabled: boolean }) {
  const { camera } = useThree();
  const controls = useRef<ElementRef<typeof OrbitControls>>(null);
  useEffect(() => {
    if (target && controls.current) {
      const t = new THREE.Vector3(target[0], target[1], target[2]);
      const dir = new THREE.Vector3().subVectors(camera.position, controls.current.target).normalize();
      camera.position.copy(t.clone().add(dir.multiplyScalar(7)));
      controls.current.target.copy(t);
      controls.current.update();
    }
  }, [target, camera]);
  return <OrbitControls ref={controls} makeDefault enabled={enabled} enableDamping dampingFactor={0.12} />;
}

function MainScene({
  point,
  colors,
  detections,
  selectedId,
  onSelect,
  focusTarget,
  preset,
  edlStrength,
  orbitEnabled,
}: {
  point: PointData;
  colors: Float32Array;
  detections: Detection[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  focusTarget: number[] | null;
  preset: CameraPreset;
  edlStrength: number;
  orbitEnabled: boolean;
}) {
  return (
    <>
      <PerspectiveCamera makeDefault position={PRESET_POSITIONS.iso} fov={55} near={0.1} far={1000} />
      <PresetCamera preset={preset} />
      <FocusController target={focusTarget} enabled={orbitEnabled} />
      <ambientLight intensity={0.7} />

      <PointCloudMesh positions={point.positions} colors={colors} />

      {detections.map((det) => (
        <BoundingBox
          key={det.id}
          detection={det}
          selected={det.id === selectedId}
          dimmed={selectedId !== null && det.id !== selectedId}
          onClick={() => onSelect(det.id)}
        />
      ))}

      <Grid
        position={[0, 0, -0.02]}
        args={[80, 80]}
        cellSize={1}
        cellThickness={0.5}
        cellColor="#1e293b"
        sectionSize={5}
        sectionThickness={1}
        sectionColor="#334155"
        fadeDistance={60}
        infiniteGrid
      />

      <EDL strength={edlStrength} />
    </>
  );
}

// --- Bird's-eye (top-down orthographic) mini view ---

// Fits the top-down orthographic camera to the cloud's actual XY bounds.
// The point cloud is authored in raw world coordinates (often far from the
// origin, e.g. S3DIS rooms around x=10, y=-23), so a camera hardcoded to look
// at the origin renders black. Here we center the camera over the cloud's XY
// midpoint and set the ortho zoom so the whole footprint fits the mini-canvas.
function BevCameraFit({ bounds }: { bounds: { cx: number; cy: number; cz: number; spanX: number; spanY: number; maxZ: number } }) {
  const { size } = useThree();
  const camRef = useRef<THREE.OrthographicCamera>(null);
  useEffect(() => {
    const cam = camRef.current;
    if (!cam) return;
    cam.position.set(bounds.cx, bounds.cy, bounds.maxZ + 50);
    cam.up.set(0, 1, 0);
    cam.lookAt(bounds.cx, bounds.cy, bounds.cz);
    // Fit the larger XY span to the smaller canvas dimension, with padding.
    const span = Math.max(bounds.spanX, bounds.spanY, 0.001) * 1.15;
    const canvasMin = Math.max(Math.min(size.width, size.height), 1);
    cam.zoom = canvasMin / span;
    cam.near = 0.1;
    cam.far = bounds.maxZ + 200;
    cam.updateProjectionMatrix();
  }, [bounds, size.width, size.height]);
  return <OrthographicCamera ref={camRef} makeDefault position={[bounds.cx, bounds.cy, bounds.maxZ + 50]} />;
}

function BevScene({
  positions,
  colors,
  detections,
  selectedId,
}: {
  positions: Float32Array;
  colors: Float32Array;
  detections: Detection[];
  selectedId: string | null;
}) {
  const geometry = useMemo(() => {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geo.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
    return geo;
  }, [positions, colors]);

  // Derive the cloud's XY center and extent from the actual points so the
  // top-down camera frames the scene wherever it sits in world space.
  const bounds = useMemo(() => {
    let minX = Infinity, minY = Infinity, minZ = Infinity;
    let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    for (let i = 0; i < positions.length; i += 3) {
      const x = positions[i], y = positions[i + 1], z = positions[i + 2];
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
      if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
    }
    if (!isFinite(minX)) {
      return { cx: 0, cy: 0, cz: 0, spanX: 1, spanY: 1, maxZ: 1 };
    }
    return {
      cx: (minX + maxX) / 2,
      cy: (minY + maxY) / 2,
      cz: (minZ + maxZ) / 2,
      spanX: maxX - minX,
      spanY: maxY - minY,
      maxZ,
    };
  }, [positions]);

  return (
    <>
      <BevCameraFit bounds={bounds} />
      <points geometry={geometry}>
        <pointsMaterial size={1.6} vertexColors sizeAttenuation={false} />
      </points>
      {detections.map((det) => {
        const { center, size, heading } = det.bbox_obb;
        return (
          <group key={det.id} position={[center[0], center[1], center[2]]} rotation={[0, 0, heading]}>
            <lineSegments>
              <edgesGeometry args={[new THREE.BoxGeometry(size[0], size[1], size[2])]} />
              <lineBasicMaterial color={det.id === selectedId ? '#fbbf24' : classColorHex(det.label)} />
            </lineSegments>
          </group>
        );
      })}
    </>
  );
}

// --- Page ---

const COLOR_MODES: { id: ColorMode; label: string }[] = [
  { id: 'height', label: 'Height' },
  { id: 'rgb', label: 'RGB' },
  { id: 'intensity', label: 'Intensity' },
  { id: 'class', label: 'Class' },
  { id: 'segment', label: 'Segment' },
];

export function ViewerPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  const [viewerParams] = useSearchParams();
  const cameFromTraining = viewerParams.get('from') === 'training';

  const [detections, setDetections] = useState<Detection[]>([]);
  const [tileIndex, setTileIndex] = useState<TileIndex | null>(null);
  const [point, setPoint] = useState<PointData | null>(null);
  const [tileId, setTileId] = useState<string | null>(null);
  const [segments, setSegments] = useState<Uint16Array | null>(null);
  const [segLoading, setSegLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [focusTarget, setFocusTarget] = useState<number[] | null>(null);
  const [preset, setPreset] = useState<CameraPreset>('iso');
  const [colorMode, setColorMode] = useState<ColorMode>('height');
  const [edlStrength, setEdlStrength] = useState(0.35);
  const [showBev, setShowBev] = useState(true);
  const [minConfidence, setMinConfidence] = useState(0);
  const [hiddenClasses, setHiddenClasses] = useState<Set<string>>(new Set());

  // Annotation mode
  const anno = useAnnotations(jobId);
  const [annotationMode, setAnnotationMode] = useState(false);
  const [orbitEnabled, setOrbitEnabled] = useState(true);
  const [transformMode, setTransformMode] = useState<TransformMode>('translate');
  const [drawRect, setDrawRect] = useState<PixelRect | null>(null);
  const [showTraining, setShowTraining] = useState(false);
  const [annoHint, setAnnoHint] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) return;
    loadData(jobId);
  }, [jobId]);

  async function loadData(id: string) {
    setLoading(true);
    setError(null);
    try {
      const [detData, index] = await Promise.all([getDetections(id), getTileIndex(id)]);
      setDetections(detData.detections || []);
      setTileIndex(index);

      const overview = index.tiles.find((t) => t.tile_id === 'overview') ?? index.tiles[0];
      if (overview) {
        const buffer = await getTileData(id, overview.tile_id);
        const fmt: PointFormat = index.point_format ?? 'xyz';
        setPoint(parseTile(buffer, fmt));
        setTileId(overview.tile_id);
        setSegments(null); // reset; loaded lazily when Segment mode is selected
      }
    } catch (err) {
      console.error('Failed to load viewer data:', err);
      setError('Failed to load 3D data. The job may not be complete yet.');
    } finally {
      setLoading(false);
    }
  }

  // Classes present + counts
  const classInfo = useMemo(() => {
    const map = new Map<string, number>();
    for (const d of detections) map.set(d.label, (map.get(d.label) ?? 0) + 1);
    return Array.from(map.entries()).sort((a, b) => b[1] - a[1]);
  }, [detections]);

  // Filtered detections (class + confidence)
  const visibleDetections = useMemo(
    () =>
      detections.filter(
        (d) => d.confidence >= minConfidence && !hiddenClasses.has(d.label),
      ),
    [detections, minConfidence, hiddenClasses],
  );

  // Lazily fetch segmentation the first time Segment mode is selected.
  useEffect(() => {
    if (colorMode !== 'segment' || segments || !jobId || !tileId) return;
    if (!tileIndex?.has_segmentation) return;
    let cancelled = false;
    setSegLoading(true);
    getSegmentation(jobId, tileId)
      .then((buf) => {
        if (!cancelled) setSegments(parseSegmentation(buf));
      })
      .catch((err) => console.error('Failed to load segmentation:', err))
      .finally(() => {
        if (!cancelled) setSegLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [colorMode, segments, jobId, tileId, tileIndex]);

  const segmentLabels = tileIndex?.segmentation_labels ?? null;

  // Point colors (recomputed only when mode or relevant data changes)
  const colors = useMemo(() => {
    if (!point) return new Float32Array(0);
    return computeColors(point, colorMode, visibleDetections, segments, segmentLabels);
    // recompute class coloring when the filtered set changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [point, colorMode, segments, segmentLabels, colorMode === 'class' ? visibleDetections : null]);

  // Segment legend with per-segment point counts (computed once per segments load)
  const segmentLegend = useMemo(() => {
    if (!segments || !segmentLabels) return [];
    const counts = new Map<number, number>();
    for (let i = 0; i < segments.length; i++) counts.set(segments[i], (counts.get(segments[i]) ?? 0) + 1);
    return segmentLabels
      .map((l) => ({ ...l, count: counts.get(l.id) ?? 0 }))
      .filter((l) => l.count > 0)
      .sort((a, b) => b.count - a.count);
  }, [segments, segmentLabels]);

  const selectDetection = useCallback(
    (id: string) => {
      setSelectedId(id);
      const det = detections.find((d) => d.id === id);
      if (det) setFocusTarget([...det.bbox_obb.center]);
    },
    [detections],
  );

  const toggleClass = (label: string) => {
    setHiddenClasses((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  };

  // Keyboard navigation
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (visibleDetections.length === 0) return;
        const idx = visibleDetections.findIndex((d) => d.id === selectedId);
        const nextIdx =
          e.key === 'ArrowDown'
            ? (idx + 1) % visibleDetections.length
            : (idx - 1 + visibleDetections.length) % visibleDetections.length;
        selectDetection(visibleDetections[nextIdx].id);
      } else if (e.key === 'f' && selectedId) {
        const det = detections.find((d) => d.id === selectedId);
        if (det) setFocusTarget([...det.bbox_obb.center]);
      } else if (e.key === 'b') {
        setShowBev((v) => !v);
      } else if (e.key === 'Escape') {
        setSelectedId(null);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [visibleDetections, selectedId, detections, selectDetection]);

  // Load annotation data (labels + existing annotations) when entering annotation mode.
  useEffect(() => {
    if (annotationMode) anno.load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [annotationMode, jobId]);

  const handleCreateBox = useCallback(
    (center: [number, number, number], size: [number, number, number]) => {
      const label = anno.activeLabel || anno.labels[0]?.name || 'object';
      anno.addBox({ label, center, size, heading: 0 });
    },
    [anno],
  );

  const handleEmptySelection = useCallback(() => {
    setAnnoHint('No points in selection — try again.');
    window.setTimeout(() => setAnnoHint(null), 2500);
  }, []);

  const toggleAnnotationMode = useCallback(() => {
    if (annotationMode && anno.dirty && !window.confirm('Discard unsaved annotation changes?')) return;
    setAnnotationMode((v) => !v);
    setOrbitEnabled(true);
    setDrawRect(null);
  }, [annotationMode, anno.dirty]);

  // Delete / Escape / W-E-R shortcuts for annotation mode.
  useEffect(() => {
    if (!annotationMode) return;
    const h = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
      if ((e.key === 'Delete' || e.key === 'Backspace') && anno.selectedId) {
        e.preventDefault();
        if (window.confirm('Delete this box?')) anno.removeBox(anno.selectedId);
      } else if (e.key === 'Escape') {
        anno.select(null);
      } else if (e.key === 'w' || e.key === 'W') {
        setTransformMode('translate');
      } else if (e.key === 'e' || e.key === 'E') {
        setTransformMode('rotate');
      } else if (e.key === 'r' || e.key === 'R') {
        setTransformMode('scale');
      } else if (e.key === 's' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        if (anno.dirty) anno.save().catch(() => {});
      }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [annotationMode, anno.selectedId, anno.removeBox, anno.select, anno.dirty, anno.save]);

  const selected = detections.find((d) => d.id === selectedId) ?? null;

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-blue-500 mx-auto mb-4" />
          <p className="text-gray-400">Loading 3D scene...</p>
        </div>
      </div>
    );
  }

  if (error || !point) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <p className="text-red-400 mb-4">{error ?? 'No point data available.'}</p>
          <button onClick={() => navigate(cameFromTraining ? '/dashboard?tab=training' : '/dashboard')} className="text-blue-400 hover:underline">
            ← Back to Dashboard
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-[calc(100vh-96px)] gap-3">
      {/* ===== 3D Canvas ===== */}
      <div className="flex-1 bg-gray-950 rounded-lg relative overflow-hidden" data-testid="point-cloud-viewer">
        <Canvas gl={{ antialias: true, alpha: false }} style={{ background: '#08080d' }}>
          <MainScene
            point={point}
            colors={colors}
            detections={annotationMode ? [] : visibleDetections}
            selectedId={selectedId}
            onSelect={selectDetection}
            focusTarget={focusTarget}
            preset={preset}
            edlStrength={edlStrength}
            orbitEnabled={orbitEnabled}
          />
          {annotationMode && (
            <>
              <AnnotationLayer
                boxes={anno.boxes}
                selectedId={anno.selectedId}
                mode={transformMode}
                labelColor={anno.labelColor}
                onSelect={anno.select}
                onUpdate={anno.updateBox}
              />
              <RegionSelect
                positions={point.positions}
                enabled={annotationMode}
                onDrawStateChange={setDrawRect}
                onOrbitEnabled={setOrbitEnabled}
                onCreate={handleCreateBox}
                onEmpty={handleEmptySelection}
              />
            </>
          )}
        </Canvas>

        {/* Selection rectangle overlay (annotation draw gesture) */}
        {drawRect && (
          <div
            className="absolute border border-blue-400 bg-blue-400/10 pointer-events-none"
            style={{
              left: Math.min(drawRect.x0, drawRect.x1),
              top: Math.min(drawRect.y0, drawRect.y1),
              width: Math.abs(drawRect.x1 - drawRect.x0),
              height: Math.abs(drawRect.y1 - drawRect.y0),
            }}
          />
        )}
        {annoHint && (
          <div className="absolute top-14 left-1/2 -translate-x-1/2 bg-gray-800/90 text-amber-300 text-xs px-3 py-1.5 rounded">
            {annoHint}
          </div>
        )}

        {/* Top-left: navigation + camera presets */}
        <div className="absolute top-3 left-3 flex flex-wrap gap-2">
          <button
            onClick={() => navigate(annotationMode || cameFromTraining ? '/dashboard?tab=training' : '/dashboard')}
            className="bg-gray-800/80 backdrop-blur px-3 py-1.5 rounded text-sm hover:bg-gray-700 transition"
          >
            ← Back
          </button>
          <button
            data-testid="annotation-mode-toggle"
            onClick={toggleAnnotationMode}
            className={`px-3 py-1.5 rounded text-sm transition ${
              annotationMode
                ? 'bg-amber-600 text-white hover:bg-amber-500'
                : 'bg-gray-800/80 backdrop-blur text-gray-200 hover:bg-gray-700'
            }`}
          >
            {annotationMode ? 'Exit Annotation' : 'Annotate'}
          </button>
          <div className="flex rounded overflow-hidden border border-gray-700">
            {(['iso', 'top', 'front', 'side'] as CameraPreset[]).map((p) => (
              <button
                key={p}
                onClick={() => setPreset(p)}
                className={`px-2.5 py-1.5 text-xs capitalize transition ${
                  preset === p ? 'bg-blue-600 text-white' : 'bg-gray-800/80 text-gray-300 hover:bg-gray-700'
                }`}
              >
                {p}
              </button>
            ))}
          </div>
        </div>

        {/* Top-right: color mode */}
        <div className="absolute top-3 right-3 flex rounded overflow-hidden border border-gray-700">
          {COLOR_MODES.map((m) => {
            const disabled =
              (m.id === 'rgb' && !point.hasRGB) ||
              (m.id === 'intensity' && !point.hasIntensity) ||
              (m.id === 'segment' && !tileIndex?.has_segmentation);
            return (
              <button
                key={m.id}
                disabled={disabled}
                onClick={() => setColorMode(m.id)}
                title={disabled ? 'No data for this channel' : `Color by ${m.label}`}
                className={`px-2.5 py-1.5 text-xs transition ${
                  colorMode === m.id
                    ? 'bg-blue-600 text-white'
                    : disabled
                    ? 'bg-gray-800/40 text-gray-600 cursor-not-allowed'
                    : 'bg-gray-800/80 text-gray-300 hover:bg-gray-700'
                }`}
              >
                {m.label}
              </button>
            );
          })}
        </div>

        {/* Bottom-left: EDL + stats */}
        <div className="absolute bottom-3 left-3 bg-gray-800/80 backdrop-blur rounded px-3 py-2 text-xs text-gray-300 flex items-center gap-4">
          <label className="flex items-center gap-2">
            <span className="text-gray-400">Depth (EDL)</span>
            <input
              type="range"
              min="0"
              max="1"
              step="0.05"
              value={edlStrength}
              onChange={(e) => setEdlStrength(parseFloat(e.target.value))}
              className="w-20"
            />
          </label>
          <span className="text-gray-500">|</span>
          <span>{point.count.toLocaleString()} pts</span>
          {tileIndex && <span>{tileIndex.total_points.toLocaleString()} total · {tileIndex.levels} LOD</span>}
          <span>{visibleDetections.length}/{detections.length} shown</span>
        </div>

        {/* Bottom-right: BEV mini-view */}
        {showBev && (
          <div className="absolute bottom-3 right-3 w-52 h-52 bg-gray-950 rounded-lg border border-gray-700 overflow-hidden">
            <div className="absolute top-1 left-2 text-[10px] text-gray-400 z-10">TOP / BEV</div>
            <Canvas gl={{ antialias: true }} style={{ background: '#08080d' }}>
              <BevScene
                positions={point.positions}
                colors={colors}
                detections={visibleDetections}
                selectedId={selectedId}
              />
            </Canvas>
          </div>
        )}
      </div>

      {/* ===== Right rail ===== */}
      {annotationMode ? (
        <AnnotationSidebar
          anno={anno}
          mode={transformMode}
          onModeChange={setTransformMode}
          onRequestTrain={() => setShowTraining(true)}
        />
      ) : (
      <div className="w-80 flex flex-col gap-3">
        {/* Segmentation legend (only in Segment color mode) */}
        {colorMode === 'segment' && (
          <div className="bg-gray-800 rounded-lg p-4" data-testid="segmentation-legend">
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-bold text-sm">Segmentation</h3>
              {segLoading && <span className="text-xs text-gray-400">loading…</span>}
            </div>
            {segmentLegend.length > 0 ? (
              <div className="space-y-1 max-h-48 overflow-y-auto">
                {segmentLegend.map((seg) => {
                  const c = segmentColor(seg.name);
                  const hex = `#${c.getHexString()}`;
                  return (
                    <div key={seg.id} className="flex items-center gap-2 px-1 py-0.5 text-sm">
                      <span className="w-3 h-3 rounded-sm flex-shrink-0" style={{ background: hex }} />
                      <span className="capitalize flex-1">{seg.name}</span>
                      <span className="text-gray-400 text-xs">{seg.count.toLocaleString()}</span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-xs text-gray-500">{segLoading ? 'Loading segmentation…' : 'No segmentation data.'}</p>
            )}
          </div>
        )}

        {/* Filters */}
        <div className="bg-gray-800 rounded-lg p-4">
          <h3 className="font-bold text-sm mb-3">Filters</h3>
          <label className="block mb-3">
            <div className="flex justify-between text-xs text-gray-400 mb-1">
              <span>Min confidence</span>
              <span>{(minConfidence * 100).toFixed(0)}%</span>
            </div>
            <input
              type="range"
              min="0"
              max="0.95"
              step="0.05"
              value={minConfidence}
              onChange={(e) => setMinConfidence(parseFloat(e.target.value))}
              className="w-full"
            />
          </label>

          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-gray-400">Classes</span>
            <div className="flex gap-2 text-xs">
              <button onClick={() => setHiddenClasses(new Set())} className="text-blue-400 hover:underline">All</button>
              <button
                onClick={() => setHiddenClasses(new Set(classInfo.map(([l]) => l)))}
                className="text-blue-400 hover:underline"
              >
                None
              </button>
            </div>
          </div>
          <div className="space-y-1 max-h-40 overflow-y-auto">
            {classInfo.map(([label, count]) => {
              const hidden = hiddenClasses.has(label);
              return (
                <button
                  key={label}
                  onClick={() => toggleClass(label)}
                  className={`w-full flex items-center gap-2 px-2 py-1 rounded text-sm transition ${
                    hidden ? 'opacity-40 hover:opacity-70' : 'hover:bg-gray-700'
                  }`}
                >
                  <span className="w-3 h-3 rounded-sm flex-shrink-0" style={{ background: classColorHex(label) }} />
                  <span className="capitalize flex-1 text-left">{label}</span>
                  <span className="text-gray-400 text-xs">{count}</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Inspector */}
        {selected && (
          <div className="bg-gray-800 rounded-lg p-4" data-testid="detection-inspector">
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-bold text-sm capitalize flex items-center gap-2">
                <span className="w-3 h-3 rounded-sm" style={{ background: classColorHex(selected.label) }} />
                {selected.label}
              </h3>
              <button onClick={() => setSelectedId(null)} className="text-gray-500 hover:text-white text-xs">✕</button>
            </div>
            <dl className="grid grid-cols-2 gap-y-1.5 text-xs">
              <dt className="text-gray-400">Confidence</dt>
              <dd className="text-right">{(selected.confidence * 100).toFixed(1)}%</dd>
              <dt className="text-gray-400">Center (x,y,z)</dt>
              <dd className="text-right font-mono">
                {selected.bbox_obb.center.map((v) => v.toFixed(1)).join(', ')}
              </dd>
              <dt className="text-gray-400">Size (l,w,h)</dt>
              <dd className="text-right font-mono">
                {selected.bbox_obb.size.map((v) => v.toFixed(2)).join(', ')}
              </dd>
              <dt className="text-gray-400">Heading</dt>
              <dd className="text-right font-mono">
                {((selected.bbox_obb.heading * 180) / Math.PI).toFixed(1)}°
              </dd>
              <dt className="text-gray-400">Volume</dt>
              <dd className="text-right font-mono">
                {(selected.bbox_obb.size[0] * selected.bbox_obb.size[1] * selected.bbox_obb.size[2]).toFixed(2)} m³
              </dd>
            </dl>
          </div>
        )}

        {/* Detection list */}
        <div className="bg-gray-800 rounded-lg flex-1 overflow-hidden flex flex-col" data-testid="detection-sidebar">
          <div className="p-3 border-b border-gray-700 flex items-center justify-between">
            <h3 className="font-bold text-sm">Detections ({visibleDetections.length})</h3>
            <span className="text-[10px] text-gray-500">↑↓ navigate · f focus · b BEV</span>
          </div>
          <div className="flex-1 overflow-y-auto p-2 space-y-1">
            {visibleDetections.map((det) => (
              <button
                key={det.id}
                data-testid={`detection-${det.id}`}
                onClick={() => selectDetection(det.id)}
                className={`w-full text-left rounded px-2.5 py-1.5 text-sm flex items-center gap-2 transition ${
                  det.id === selectedId
                    ? 'bg-blue-600/30 border border-blue-500'
                    : 'bg-gray-700/50 hover:bg-gray-700 border border-transparent'
                }`}
              >
                <span className="w-2.5 h-2.5 rounded-sm flex-shrink-0" style={{ background: classColorHex(det.label) }} />
                <span className="capitalize flex-1">{det.label}</span>
                <span
                  className={`text-xs px-1.5 py-0.5 rounded ${
                    det.confidence >= 0.8
                      ? 'bg-green-900 text-green-300'
                      : det.confidence >= 0.6
                      ? 'bg-yellow-900 text-yellow-300'
                      : 'bg-red-900 text-red-300'
                  }`}
                >
                  {(det.confidence * 100).toFixed(0)}%
                </span>
              </button>
            ))}
            {visibleDetections.length === 0 && (
              <p className="text-center text-gray-500 text-sm py-6">No detections match filters.</p>
            )}
          </div>
        </div>
      </div>
      )}

      {showTraining && jobId && (
        <TrainingDialog jobId={jobId} usedClasses={anno.usedClasses} onClose={() => setShowTraining(false)} />
      )}
    </div>
  );
}
