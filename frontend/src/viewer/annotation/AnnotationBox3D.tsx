/**
 * One editable oriented bounding box in the scene. Mirrors the read-only
 * `BoundingBox` in Viewer.tsx (group at center, yaw about Z, translucent fill +
 * edge outline) but is standalone and forwards its group ref so AnnotationLayer
 * can attach TransformControls to the selected box.
 */
import { forwardRef, useMemo } from 'react';
import * as THREE from 'three';
import type { AnnotationBox } from './types';

interface Props {
  box: AnnotationBox;
  color: string;
  selected: boolean;
  onSelect: () => void;
}

export const AnnotationBox3D = forwardRef<THREE.Group, Props>(function AnnotationBox3D(
  { box, color, selected, onSelect },
  ref,
) {
  const boxGeo = useMemo(
    () => new THREE.BoxGeometry(box.size[0], box.size[1], box.size[2]),
    [box.size[0], box.size[1], box.size[2]],
  );
  const edgeGeo = useMemo(() => new THREE.EdgesGeometry(boxGeo), [boxGeo]);
  const c = selected ? '#fbbf24' : color;

  return (
    <group ref={ref} position={[box.center[0], box.center[1], box.center[2]]} rotation={[0, 0, box.heading]}>
      <mesh
        geometry={boxGeo}
        onClick={(e) => {
          e.stopPropagation();
          onSelect();
        }}
      >
        <meshBasicMaterial color={c} transparent opacity={selected ? 0.28 : 0.12} depthWrite={false} />
      </mesh>
      <lineSegments geometry={edgeGeo}>
        <lineBasicMaterial color={c} />
      </lineSegments>
    </group>
  );
});
