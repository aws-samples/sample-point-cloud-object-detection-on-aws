/**
 * Point cloud parsing + color-mode computation.
 *
 * Tile binary formats (interleaved Float32):
 *   xyz      -> [x,y,z]              stride 3
 *   xyzrgb   -> [x,y,z,r,g,b]        stride 6   (rgb in 0..1)
 *   xyzrgbi  -> [x,y,z,r,g,b,i]      stride 7   (intensity in 0..1)
 */
import * as THREE from 'three';
import type { PointFormat } from '../services/api';
import { classColor } from './classColors';

export type ColorMode = 'height' | 'rgb' | 'intensity' | 'class' | 'segment';

export interface SegmentLabel {
  id: number;
  name: string;
}

export interface Detection {
  id: string;
  label: string;
  confidence: number;
  bbox_obb: { center: number[]; size: number[]; heading: number };
  bbox_aabb: { min: number[]; max: number[] };
}

export interface PointData {
  positions: Float32Array; // n * 3
  rgb: Float32Array | null; // n * 3 or null
  intensity: Float32Array | null; // n or null
  count: number;
  hasRGB: boolean;
  hasIntensity: boolean;
}

const STRIDE: Record<PointFormat, number> = { xyz: 3, xyzrgb: 6, xyzrgbi: 7 };

export function parseTile(buffer: ArrayBuffer, format: PointFormat = 'xyz'): PointData {
  const raw = new Float32Array(buffer);
  const stride = STRIDE[format];
  const count = Math.floor(raw.length / stride);

  const positions = new Float32Array(count * 3);
  const rgb = stride >= 6 ? new Float32Array(count * 3) : null;
  const intensity = stride >= 7 ? new Float32Array(count) : null;

  for (let i = 0; i < count; i++) {
    const o = i * stride;
    positions[i * 3] = raw[o];
    positions[i * 3 + 1] = raw[o + 1];
    positions[i * 3 + 2] = raw[o + 2];
    if (rgb) {
      rgb[i * 3] = raw[o + 3];
      rgb[i * 3 + 1] = raw[o + 4];
      rgb[i * 3 + 2] = raw[o + 5];
    }
    if (intensity) {
      intensity[i] = raw[o + 6];
    }
  }

  return {
    positions,
    rgb,
    intensity,
    count,
    hasRGB: rgb !== null,
    hasIntensity: intensity !== null,
  };
}

/** True if point p (world) is inside the oriented bounding box (yaw about z). */
export function pointInOBB(
  px: number,
  py: number,
  pz: number,
  obb: { center: number[]; size: number[]; heading: number },
): boolean {
  const { center, size, heading } = obb;
  const dx = px - center[0];
  const dy = py - center[1];
  const dz = pz - center[2];
  const cos = Math.cos(-heading);
  const sin = Math.sin(-heading);
  const lx = dx * cos - dy * sin;
  const ly = dx * sin + dy * cos;
  return (
    Math.abs(lx) <= size[0] / 2 &&
    Math.abs(ly) <= size[1] / 2 &&
    Math.abs(dz) <= size[2] / 2
  );
}

const _c = new THREE.Color();

/** Parse a raw segmentation buffer into per-point Uint16 labels. */
export function parseSegmentation(buffer: ArrayBuffer): Uint16Array {
  return new Uint16Array(buffer);
}

/** Names that should render as a dim/background segment rather than a class color. */
function isBackgroundSegment(name: string): boolean {
  const n = name.toLowerCase();
  return n === 'floor' || n === 'background' || n === 'unlabeled' || n === 'none';
}

/** Segment color: dim gray for background/floor, class palette otherwise. */
export function segmentColor(name: string): THREE.Color {
  if (isBackgroundSegment(name)) return new THREE.Color(0.22, 0.24, 0.28);
  return classColor(name);
}

/** Compute a per-point color buffer (n*3) for the requested mode. */
export function computeColors(
  data: PointData,
  mode: ColorMode,
  detections: Detection[],
  segments?: Uint16Array | null,
  segmentLabels?: SegmentLabel[] | null,
): Float32Array {
  const { positions, rgb, intensity, count } = data;
  const colors = new Float32Array(count * 3);

  if (mode === 'rgb' && rgb) {
    colors.set(rgb);
    return colors;
  }

  if (mode === 'segment' && segments && segmentLabels) {
    const idToColor = new Map<number, THREE.Color>();
    for (const l of segmentLabels) idToColor.set(l.id, segmentColor(l.name));
    const fallback = new THREE.Color(0.22, 0.24, 0.28);
    for (let i = 0; i < count; i++) {
      const c = idToColor.get(segments[i]) ?? fallback;
      colors[i * 3] = c.r;
      colors[i * 3 + 1] = c.g;
      colors[i * 3 + 2] = c.b;
    }
    return colors;
  }

  if (mode === 'intensity' && intensity) {
    for (let i = 0; i < count; i++) {
      const t = Math.min(1, Math.max(0, intensity[i]));
      // Viridis-ish ramp: dark purple -> teal -> yellow
      _c.setHSL(0.75 - t * 0.55, 0.85, 0.25 + t * 0.4);
      colors[i * 3] = _c.r;
      colors[i * 3 + 1] = _c.g;
      colors[i * 3 + 2] = _c.b;
    }
    return colors;
  }

  if (mode === 'class') {
    // Dim base color; points inside a detection take that class color.
    for (let i = 0; i < count; i++) {
      colors[i * 3] = 0.16;
      colors[i * 3 + 1] = 0.18;
      colors[i * 3 + 2] = 0.22;
    }
    for (let i = 0; i < count; i++) {
      const px = positions[i * 3];
      const py = positions[i * 3 + 1];
      const pz = positions[i * 3 + 2];
      for (const det of detections) {
        if (pointInOBB(px, py, pz, det.bbox_obb)) {
          const c = classColor(det.label);
          colors[i * 3] = c.r;
          colors[i * 3 + 1] = c.g;
          colors[i * 3 + 2] = c.b;
          break;
        }
      }
    }
    return colors;
  }

  // Default: height (z) ramp — blue (low) -> red (high)
  let minZ = Infinity;
  let maxZ = -Infinity;
  for (let i = 0; i < count; i++) {
    const z = positions[i * 3 + 2];
    if (z < minZ) minZ = z;
    if (z > maxZ) maxZ = z;
  }
  const range = maxZ - minZ || 1;
  for (let i = 0; i < count; i++) {
    const t = (positions[i * 3 + 2] - minZ) / range;
    _c.setHSL(0.66 - t * 0.66, 0.9, 0.5);
    colors[i * 3] = _c.r;
    colors[i * 3 + 1] = _c.g;
    colors[i * 3 + 2] = _c.b;
  }
  return colors;
}

/** A soft round sprite so points render as discs instead of squares. */
let _discTexture: THREE.Texture | null = null;
export function discTexture(): THREE.Texture {
  if (_discTexture) return _discTexture;
  const size = 64;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d')!;
  const grd = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grd.addColorStop(0, 'rgba(255,255,255,1)');
  grd.addColorStop(0.7, 'rgba(255,255,255,1)');
  grd.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = grd;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.needsUpdate = true;
  _discTexture = tex;
  return tex;
}
