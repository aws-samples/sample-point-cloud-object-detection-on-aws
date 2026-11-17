import { describe, it, expect } from 'vitest';
import * as THREE from 'three';
import { fitBoxToRegion } from './fitBox';
import { pointInOBB } from '../pointCloud';

describe('pointInOBB', () => {
  const box = { center: [0, 0, 0], size: [2, 4, 6], heading: 0 };

  it('accepts a point at the center', () => {
    expect(pointInOBB(0, 0, 0, box)).toBe(true);
  });

  it('accepts points on the inside of each half-extent', () => {
    expect(pointInOBB(0.9, 1.9, 2.9, box)).toBe(true);
  });

  it('rejects points beyond a half-extent', () => {
    expect(pointInOBB(1.1, 0, 0, box)).toBe(false); // x half-extent is 1
    expect(pointInOBB(0, 2.1, 0, box)).toBe(false); // y half-extent is 2
    expect(pointInOBB(0, 0, 3.1, box)).toBe(false); // z half-extent is 3
  });

  it('respects yaw (heading) about Z', () => {
    // Rotate the 2x4 footprint by 90deg: the long axis (y, half 2) now lies along world x.
    const rotated = { center: [0, 0, 0], size: [2, 4, 6], heading: Math.PI / 2 };
    expect(pointInOBB(1.9, 0, 0, rotated)).toBe(true); // within rotated long axis
    expect(pointInOBB(0, 1.1, 0, rotated)).toBe(false); // beyond rotated short axis (half 1)
  });
});

describe('fitBoxToRegion', () => {
  // Orthographic camera looking straight down -Z so world (x,y) map linearly to NDC.
  // left/right/top/bottom = +/-10 => world coord c maps to ndc c/10.
  function orthoCamera() {
    const cam = new THREE.OrthographicCamera(-10, 10, 10, -10, 0.1, 100);
    cam.position.set(0, 0, 10);
    cam.up.set(0, 1, 0);
    cam.lookAt(0, 0, 0);
    cam.updateMatrixWorld(true);
    cam.updateProjectionMatrix();
    return cam;
  }
  const viewport = { width: 200, height: 200 };
  // Four points forming a 4x4 square at z in [0,1].
  const positions = new Float32Array([
    -2, -2, 0,
    2, -2, 1,
    2, 2, 0,
    -2, 2, 1,
  ]);

  it('fits a box to all points when the rectangle covers the screen', () => {
    const cam = orthoCamera();
    const box = fitBoxToRegion({ x0: 0, y0: 0, x1: 200, y1: 200 }, cam, positions, viewport, 0);
    expect(box).not.toBeNull();
    expect(box!.center[0]).toBeCloseTo(0, 5);
    expect(box!.center[1]).toBeCloseTo(0, 5);
    expect(box!.size[0]).toBeCloseTo(4, 5); // x extent -2..2
    expect(box!.size[1]).toBeCloseTo(4, 5); // y extent -2..2
    expect(box!.size[2]).toBeCloseTo(1, 5); // z extent 0..1
  });

  it('selects only the points inside a partial rectangle', () => {
    const cam = orthoCamera();
    // Right half of the screen (px 100..200) => world x >= 0 => the two x=2 points.
    const box = fitBoxToRegion({ x0: 100, y0: 0, x1: 200, y1: 200 }, cam, positions, viewport, 0);
    expect(box).not.toBeNull();
    expect(box!.center[0]).toBeCloseTo(2, 5);
    expect(box!.size[0]).toBeCloseTo(0.05, 5); // both selected points share x=2 -> min extent
  });

  it('returns null when no points fall inside the rectangle', () => {
    const cam = orthoCamera();
    // A tiny rectangle in the far top-left corner where no points project.
    const box = fitBoxToRegion({ x0: 0, y0: 0, x1: 2, y1: 2 }, cam, positions, viewport, 0);
    expect(box).toBeNull();
  });

  it('applies padding to the fitted extents', () => {
    const cam = orthoCamera();
    const box = fitBoxToRegion({ x0: 0, y0: 0, x1: 200, y1: 200 }, cam, positions, viewport, 0.1);
    expect(box!.size[0]).toBeCloseTo(4.4, 5); // 4 * 1.1
  });
});
