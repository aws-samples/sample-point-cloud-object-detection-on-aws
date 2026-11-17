/**
 * Domain types for the in-viewer annotation tool.
 *
 * `AnnotationBox` is the editable, client-side oriented bounding box. It mirrors
 * the read-only `Detection.bbox_obb` schema (center / size / heading, Z-up, yaw
 * about Z) but is standalone and carries its own label. The wire DTOs it maps to
 * (`AnnotationDTO` / `AnnotationSet`) live in `services/api.ts`.
 */

/** Editable oriented bounding box created/edited in annotation mode. */
export interface AnnotationBox {
  /** Client-generated id (crypto.randomUUID). */
  id: string;
  /** Class name / label (maps to `class_name` on save). */
  label: string;
  /** World-space center [x, y, z] (Z-up). */
  center: [number, number, number];
  /** Box extents [w, l, h], all > 0. */
  size: [number, number, number];
  /** Yaw about the Z axis, in radians. */
  heading: number;
}

// Re-export the wire label type so annotation components have a single import site.
export type { LabelDef } from '../../services/api';
