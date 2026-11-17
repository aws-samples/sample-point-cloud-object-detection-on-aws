/**
 * Geometry for the SUSTechPOINTS-style "draw box" gesture: given a screen-space
 * rectangle, project the loaded point cloud to pixels, select the points inside
 * the rectangle, and fit an axis-aligned box (heading 0) to their extents. The
 * user then refines heading/size with transform handles.
 *
 * Pure and side-effect free so it can be unit-tested without a DOM/WebGL context.
 */
import * as THREE from 'three';

export interface PixelRect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface FittedBox {
  center: [number, number, number];
  size: [number, number, number];
}

/** Minimum extent so a fit box is never degenerate/zero-thickness. */
const MIN_EXTENT = 0.05;

/**
 * Fit an axis-aligned box to the points whose screen projection falls inside `rect`.
 *
 * @param rect     Pixel rectangle (any corner order).
 * @param camera   Camera whose matrices are already up to date.
 * @param positions Interleaved world positions (n * 3), e.g. `PointData.positions`.
 * @param viewport Canvas size in pixels.
 * @param padding  Fractional padding added to each extent (default 5%).
 * @returns The fitted box, or `null` if no points fall inside the rectangle.
 */
export function fitBoxToRegion(
  rect: PixelRect,
  camera: THREE.Camera,
  positions: Float32Array,
  viewport: { width: number; height: number },
  padding = 0.05,
): FittedBox | null {
  const minX = Math.min(rect.x0, rect.x1);
  const maxX = Math.max(rect.x0, rect.x1);
  const minY = Math.min(rect.y0, rect.y1);
  const maxY = Math.max(rect.y0, rect.y1);

  const v = new THREE.Vector3();
  let found = false;
  let bxMin = Infinity;
  let byMin = Infinity;
  let bzMin = Infinity;
  let bxMax = -Infinity;
  let byMax = -Infinity;
  let bzMax = -Infinity;

  const n = Math.floor(positions.length / 3);
  for (let i = 0; i < n; i++) {
    const x = positions[i * 3];
    const y = positions[i * 3 + 1];
    const z = positions[i * 3 + 2];

    v.set(x, y, z).project(camera);
    if (v.z > 1 || v.z < -1) continue; // outside the near/far clip (e.g. behind camera)

    const px = (v.x * 0.5 + 0.5) * viewport.width;
    const py = (-v.y * 0.5 + 0.5) * viewport.height;
    if (px < minX || px > maxX || py < minY || py > maxY) continue;

    found = true;
    if (x < bxMin) bxMin = x;
    if (x > bxMax) bxMax = x;
    if (y < byMin) byMin = y;
    if (y > byMax) byMax = y;
    if (z < bzMin) bzMin = z;
    if (z > bzMax) bzMax = z;
  }

  if (!found) return null;

  const grow = 1 + padding;
  return {
    center: [(bxMin + bxMax) / 2, (byMin + byMax) / 2, (bzMin + bzMax) / 2],
    size: [
      Math.max(bxMax - bxMin, MIN_EXTENT) * grow,
      Math.max(byMax - byMin, MIN_EXTENT) * grow,
      Math.max(bzMax - bzMin, MIN_EXTENT) * grow,
    ],
  };
}
