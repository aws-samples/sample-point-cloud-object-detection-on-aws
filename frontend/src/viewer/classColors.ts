/**
 * Stable per-class color palette for detection boxes and class-colored points.
 * Colors are chosen to be distinguishable on a dark background.
 */
import * as THREE from 'three';

const PALETTE = [
  '#60a5fa', // blue
  '#f87171', // red
  '#34d399', // green
  '#fbbf24', // amber
  '#a78bfa', // violet
  '#f472b6', // pink
  '#22d3ee', // cyan
  '#fb923c', // orange
  '#a3e635', // lime
  '#e879f9', // fuchsia
  '#2dd4bf', // teal
  '#facc15', // yellow
];

const cache = new Map<string, string>();

/** Deterministic hash so a given label always maps to the same color. */
function hashLabel(label: string): number {
  let h = 0;
  for (let i = 0; i < label.length; i++) {
    h = (h * 31 + label.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

export function classColorHex(label: string): string {
  const cached = cache.get(label);
  if (cached) return cached;
  const color = PALETTE[hashLabel(label) % PALETTE.length];
  cache.set(label, color);
  return color;
}

const threeColorCache = new Map<string, THREE.Color>();

export function classColor(label: string): THREE.Color {
  const cached = threeColorCache.get(label);
  if (cached) return cached;
  const c = new THREE.Color(classColorHex(label));
  threeColorCache.set(label, c);
  return c;
}
