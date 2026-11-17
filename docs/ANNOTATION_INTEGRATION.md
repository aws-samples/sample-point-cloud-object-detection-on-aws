# Annotation Integration — Adding Bounding Box Drawing to the Web UI

**Owner**: frontend
**Priority**: Week 3
**Context**: the full annotation → fine-tuning pipeline has been validated using SUSTechPOINTS as a standalone tool. This integrates that capability into the web UI so users can annotate directly within the solution.

---

## What We Need

A mode in the 3D Viewer page where users can:
1. Draw 7-DOF bounding boxes on the point cloud (center, size, yaw rotation)
2. Assign labels from a configurable class list (e.g., door, window, wall)
3. Save annotations in our unified format
4. Submit annotations to trigger fine-tuning

---

## Current State

- The 3D viewer already renders point clouds + detection bounding boxes
- SUSTechPOINTS (standalone tool) handles annotation — we want to bring that UX into our viewer
- Backend API already has fine-tuning endpoints (`POST /api/training`)

---

## Proposed UX Flow

```
Viewer Page (/viewer/:jobId)
  → Toggle "Annotation Mode" button
  → User draws boxes:
      - Ctrl + drag on the point cloud to select a region
      - Box appears, user adjusts size/rotation with handles
      - Dropdown to assign label (Door, Window, Wall, etc.)
      - Box list in sidebar (click to select, delete, rename)
  → "Save Annotations" button → POST to API
  → "Start Fine-Tuning" button → triggers training job
```

---

## Technical Implementation

### Frontend Components to Add

#### 1. AnnotationMode Toggle
```tsx
// In Viewer.tsx — add a toggle button
<button onClick={toggleAnnotationMode} data-testid="annotation-mode-toggle">
  {annotationMode ? 'Exit Annotation' : 'Annotate'}
</button>
```

#### 2. BoundingBoxDrawer Component
The key interaction: user draws a 2D rectangle on screen → project to 3D → create an initial box → user refines.

Reference: SUSTechPOINTS uses this approach (see `tools/SUSTechPOINTS/public/js/box_op.js`):
- Ctrl + left-drag creates a selection rectangle
- Points within the rectangle are selected
- A bounding box is fit to the selected points (min/max extents)
- User can then resize/rotate with handles

**Three.js implementation approach:**
- Use `THREE.Raycaster` to project screen rectangle to 3D
- Use `THREE.Box3` to compute initial AABB from selected points
- Render box as `THREE.BoxHelper` or wireframe mesh
- Add `TransformControls` for resize/rotate handles

#### 3. AnnotationSidebar Component
```tsx
interface Annotation {
  id: string;
  label: string;
  center: [number, number, number];
  size: [number, number, number];
  heading: number;  // yaw rotation in radians
}

// Shows list of annotations, allows label editing, deletion
<AnnotationSidebar
  annotations={annotations}
  labels={['door', 'window', 'wall', 'column']}
  onLabelChange={(id, label) => ...}
  onDelete={(id) => ...}
  onSelect={(id) => ...}  // zoom to box
/>
```

#### 4. Save Annotations API Call
```typescript
// POST /api/annotations/{jobId}
async function saveAnnotations(jobId: string, annotations: Annotation[]) {
  await api.post(`/annotations/${jobId}`, {
    annotations: annotations.map(a => ({
      center_xyz: a.center,
      size_wlh: a.size,
      heading: a.heading,
      class_name: a.label,
    }))
  });
}
```

---

## Backend API Endpoint (to be added)

```
POST /api/annotations/{job_id}
Body: {
  "scene_id": "string",
  "point_cloud_path": "s3://...",
  "annotations": [
    {"center_xyz": [x,y,z], "size_wlh": [w,l,h], "heading": float, "class_name": "door"}
  ]
}
Response: 201 Created
```

```
POST /api/training
Body: {
  "base_model": "votenet_scannet",
  "data_path": "annotations from above",
  "epochs": 30,
  "custom_classes": ["door", "window"]
}
Response: 201 Created (returns training job ID)
```

---

## Label Configuration

Labels come from a configurable list. The API serves available labels:

```
GET /api/labels
Response: {
  "labels": [
    {"name": "door", "color": "#00cc66", "default_size": [1.0, 0.2, 2.1]},
    {"name": "window", "color": "#44ccff", "default_size": [1.2, 0.2, 1.2]},
    {"name": "wall", "color": "#aaaaaa", "default_size": [3.0, 0.3, 2.8]},
    ...
  ]
}
```

This is configurable — users can add custom labels for their domain.

---

## Annotation Data Format (Our Unified Format)

This is what we save and what the training pipeline consumes:

```json
{
  "scene_id": "office_chair_scene",
  "point_cloud_path": "s3://bucket/uploads/xxx/scan.ply",
  "num_annotations": 3,
  "annotations": [
    {
      "center_xyz": [0.66, -0.68, -1.83],
      "size_wlh": [1.04, 2.29, 1.18],
      "heading": 1.58,
      "class_name": "door"
    }
  ]
}
```

---

## Reference: SUSTechPOINTS Controls

These are the controls that work well for 3D annotation (proven UX from SUSTechPOINTS):

| Action | Control |
|--------|---------|
| Rotate view | Left-click + drag |
| Pan view | Right-click + drag |
| Zoom | Scroll wheel |
| Draw box | Ctrl + left-drag (select region) |
| Move box | Click box + drag |
| Resize box | Click handle + drag |
| Rotate box (yaw) | R + drag (or rotation handle) |
| Delete box | Select + Delete key |
| Assign label | Click box → dropdown in sidebar |

---

## File Locations

- Existing viewer: `frontend/src/pages/Viewer.tsx`
- API types: `frontend/src/services/api.ts`
- Backend annotations endpoint: to be added under `src/pointcloud_detect/api/routes/`
- Annotation format: the `AnnotationSet` shape above, consumed by
  `src/pointcloud_detect/training/data_loader.py`
- Backend data model (jobs, tiles, model versions): `docs/reference/backend-domain-entities.md`

---

## Testing

- `data-testid="annotation-mode-toggle"` — toggle annotation mode
- `data-testid="annotation-box-{id}"` — each drawn box
- `data-testid="annotation-label-select"` — label dropdown
- `data-testid="annotation-save-button"` — save button
- `data-testid="annotation-train-button"` — trigger training

---

## Scope for Aug 8 (MVP)

For the review deadline, minimum viable:
1. ✅ Draw boxes on the point cloud (Ctrl + drag → select → fit box)
2. ✅ Assign labels from dropdown
3. ✅ Save to API (our annotation format)
4. 🔲 Nice-to-have: resize/rotate handles
5. 🔲 Nice-to-have: trigger fine-tuning from UI

The key demo: upload → detect → view results → annotate corrections → save → fine-tune → improved results.
