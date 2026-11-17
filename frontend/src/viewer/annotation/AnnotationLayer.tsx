/**
 * Renders all annotation boxes and attaches a single TransformControls gizmo to
 * the selected box for move / rotate (Z-only) / resize.
 *
 * Important: TransformControls must NOT set `makeDefault` — the default stays on
 * the scene's OrbitControls so drei can suspend orbit while a handle is dragged.
 * Box transforms are synced back to state on `onMouseUp` (not per-frame) to keep
 * geometry rebuilds cheap; scale is baked into `size` and reset to 1.
 */
import { useState } from 'react';
import * as THREE from 'three';
import { TransformControls } from '@react-three/drei';
import { AnnotationBox3D } from './AnnotationBox3D';
import type { AnnotationBox } from './types';

export type TransformMode = 'translate' | 'rotate' | 'scale';

interface Props {
  boxes: AnnotationBox[];
  selectedId: string | null;
  mode: TransformMode;
  labelColor: (name: string) => string;
  onSelect: (id: string) => void;
  onUpdate: (id: string, patch: Partial<Omit<AnnotationBox, 'id'>>) => void;
}

export function AnnotationLayer({ boxes, selectedId, mode, labelColor, onSelect, onUpdate }: Props) {
  // Capture the selected box's group via callback ref so TransformControls only
  // mounts once it has a valid object to attach to. The `key={id}` on the selected
  // box remounts it (and re-fires this ref) whenever the selection changes, so no
  // manual reset is needed — resetting here would null the object right after the
  // ref set it and the gizmo would flash then vanish.
  const [selObj, setSelObj] = useState<THREE.Group | null>(null);

  const selectedBox = boxes.find((b) => b.id === selectedId) ?? null;

  const syncFromGizmo = () => {
    if (!selObj || !selectedBox) return;
    const center: [number, number, number] = [selObj.position.x, selObj.position.y, selObj.position.z];
    const heading = selObj.rotation.z;
    let size = selectedBox.size;
    const s = selObj.scale;
    if (s.x !== 1 || s.y !== 1 || s.z !== 1) {
      size = [selectedBox.size[0] * s.x, selectedBox.size[1] * s.y, selectedBox.size[2] * s.z];
      selObj.scale.set(1, 1, 1);
    }
    onUpdate(selectedBox.id, { center, size, heading });
  };

  return (
    <>
      {boxes
        .filter((b) => b.id !== selectedId)
        .map((b) => (
          <AnnotationBox3D
            key={b.id}
            box={b}
            color={labelColor(b.label)}
            selected={false}
            onSelect={() => onSelect(b.id)}
          />
        ))}

      {selectedBox && (
        <AnnotationBox3D
          key={selectedBox.id}
          ref={setSelObj}
          box={selectedBox}
          color={labelColor(selectedBox.label)}
          selected
          onSelect={() => onSelect(selectedBox.id)}
        />
      )}

      {selectedBox && selObj && (
        <TransformControls
          object={selObj}
          mode={mode}
          space="world"
          showX={mode !== 'rotate'}
          showY={mode !== 'rotate'}
          showZ
          onMouseUp={syncFromGizmo}
        />
      )}
    </>
  );
}
