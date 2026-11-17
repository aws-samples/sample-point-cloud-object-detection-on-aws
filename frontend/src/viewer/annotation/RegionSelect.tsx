/**
 * SUSTechPOINTS-style draw gesture. Rendered inside <Canvas>; uses the live
 * camera + canvas element to turn a Ctrl/Cmd + left-drag into a fitted box.
 *
 * The in-progress selection rectangle is reported via `onDrawStateChange` so the
 * Viewer can render a DOM overlay (R3F can't draw screen-space HTML in-scene).
 * Orbit is suspended while dragging via `onOrbitEnabled`.
 *
 * Callbacks are read through a ref so the pointer listeners stay stable for the
 * duration of a drag even if the parent re-renders.
 */
import { useEffect, useRef } from 'react';
import { useThree } from '@react-three/fiber';
import { fitBoxToRegion, PixelRect } from './fitBox';

interface Props {
  positions: Float32Array;
  enabled: boolean;
  onDrawStateChange: (rect: PixelRect | null) => void;
  onOrbitEnabled: (enabled: boolean) => void;
  onCreate: (center: [number, number, number], size: [number, number, number]) => void;
  onEmpty: () => void;
}

export function RegionSelect(props: Props) {
  const { camera, gl } = useThree();
  const enabled = props.enabled;

  const cb = useRef(props);
  cb.current = props;

  const drag = useRef<{ x0: number; y0: number } | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const el = gl.domElement;

    const local = (e: PointerEvent) => {
      const r = el.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top, rect: r };
    };

    const onDown = (e: PointerEvent) => {
      if (e.button !== 0 || !(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      const { x, y } = local(e);
      drag.current = { x0: x, y0: y };
      cb.current.onOrbitEnabled(false);
      cb.current.onDrawStateChange({ x0: x, y0: y, x1: x, y1: y });
    };

    const onMove = (e: PointerEvent) => {
      if (!drag.current) return;
      const { x, y } = local(e);
      cb.current.onDrawStateChange({ x0: drag.current.x0, y0: drag.current.y0, x1: x, y1: y });
    };

    const onUp = (e: PointerEvent) => {
      if (!drag.current) return;
      const start = drag.current;
      drag.current = null;
      const { x, y, rect } = local(e);
      cb.current.onDrawStateChange(null);
      cb.current.onOrbitEnabled(true);

      const pr: PixelRect = { x0: start.x0, y0: start.y0, x1: x, y1: y };
      // Treat a tiny drag as a click, not a selection.
      if (Math.abs(pr.x1 - pr.x0) < 4 && Math.abs(pr.y1 - pr.y0) < 4) return;

      camera.updateMatrixWorld();
      const fit = fitBoxToRegion(pr, camera, cb.current.positions, {
        width: rect.width,
        height: rect.height,
      });
      if (fit) cb.current.onCreate(fit.center, fit.size);
      else cb.current.onEmpty();
    };

    const onContext = (e: Event) => {
      if (drag.current) e.preventDefault();
    };

    el.addEventListener('pointerdown', onDown);
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    el.addEventListener('contextmenu', onContext);
    return () => {
      el.removeEventListener('pointerdown', onDown);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      el.removeEventListener('contextmenu', onContext);
      cb.current.onOrbitEnabled(true);
    };
  }, [enabled, gl, camera]);

  return null;
}
