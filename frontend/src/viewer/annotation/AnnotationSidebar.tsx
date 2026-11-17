/**
 * Right-rail UI for annotation mode: active-label picker (+ custom labels),
 * per-box inspector (transform mode, relabel, numeric center/size/heading,
 * delete), the box list, and Save / Start-fine-tuning actions.
 *
 * Mirrors the existing viewer's panel styling. `data-testid`s follow
 * docs/ANNOTATION_INTEGRATION.md.
 */
import { useState } from 'react';
import type { UseAnnotations } from './useAnnotations';
import type { TransformMode } from './AnnotationLayer';
import { MAX_CLASS_NAME_LENGTH } from '../../services/limits';

interface Props {
  anno: UseAnnotations;
  mode: TransformMode;
  onModeChange: (m: TransformMode) => void;
  onRequestTrain: () => void;
}

const MODES: TransformMode[] = ['translate', 'rotate', 'scale'];
const MODE_LABEL: Record<TransformMode, string> = { translate: 'Move', rotate: 'Rotate', scale: 'Resize' };

function Vec3Input({
  label,
  value,
  onChange,
  step = 0.1,
  min,
}: {
  label: string;
  value: [number, number, number];
  onChange: (v: [number, number, number]) => void;
  step?: number;
  min?: number;
}) {
  const set = (i: number, s: string) => {
    const n = parseFloat(s);
    const v = [...value] as [number, number, number];
    v[i] = Number.isFinite(n) ? (min !== undefined ? Math.max(min, n) : n) : 0;
    onChange(v);
  };
  return (
    <div>
      <label className="block text-xs text-gray-400 mb-1">{label}</label>
      <div className="grid grid-cols-3 gap-1">
        {value.map((c, i) => (
          <input
            key={i}
            type="number"
            step={step}
            value={+c.toFixed(3)}
            onChange={(e) => set(i, e.target.value)}
            className="bg-gray-700 rounded px-1.5 py-1 text-xs font-mono"
          />
        ))}
      </div>
    </div>
  );
}

export function AnnotationSidebar({ anno, mode, onModeChange, onRequestTrain }: Props) {
  const { boxes, labels, activeLabel, selectedId, dirty, saving, saveError, loadError } = anno;
  const [custom, setCustom] = useState('');
  const [showHelp, setShowHelp] = useState(false);
  const selected = boxes.find((b) => b.id === selectedId) ?? null;

  const confirmDelete = (id: string) => {
    if (window.confirm('Delete this box?')) anno.removeBox(id);
  };

  return (
    <div className="w-80 flex flex-col gap-3" data-testid="annotation-sidebar">
      {loadError && (
        <div className="bg-red-900/40 border border-red-700 rounded-lg p-3 text-sm text-red-200">
          {loadError}
          <button onClick={() => anno.load()} className="ml-2 underline text-red-100">
            Retry
          </button>
        </div>
      )}

      {/* Active label + custom */}
      <div className="bg-gray-800 rounded-lg p-4">
        <h3 className="font-bold text-sm mb-3">Annotation</h3>
        <label className="block text-xs text-gray-400 mb-1">Active label (new boxes)</label>
        <select
          data-testid="annotation-label-select"
          value={activeLabel}
          onChange={(e) => anno.setActiveLabel(e.target.value)}
          className="w-full bg-gray-700 rounded px-2 py-1.5 text-sm mb-2"
        >
          {labels.map((l) => (
            <option key={l.name} value={l.name}>
              {l.name}
            </option>
          ))}
        </select>
        <div className="flex gap-2">
          <input
            maxLength={MAX_CLASS_NAME_LENGTH}
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            placeholder="add custom label"
            className="flex-1 bg-gray-700 rounded px-2 py-1 text-sm"
          />
          <button
            onClick={() => {
              anno.addCustomLabel(custom);
              setCustom('');
            }}
            className="px-2 py-1 text-sm bg-blue-600 rounded hover:bg-blue-500"
          >
            Add
          </button>
        </div>
        <p className="text-[11px] text-gray-500 mt-2">Ctrl/⌘ + drag on the cloud to draw a box.</p>
      </div>

      {/* Keyboard shortcuts help */}
      <div className="bg-gray-800 rounded-lg overflow-hidden">
        <button
          onClick={() => setShowHelp((h) => !h)}
          className="w-full flex items-center justify-between px-4 py-2.5 text-sm text-gray-300 hover:bg-gray-700/50"
        >
          <span className="font-medium">Shortcuts</span>
          <span className="text-xs text-gray-500">{showHelp ? '▲' : '▼'}</span>
        </button>
        {showHelp && (
          <div className="px-4 pb-3 space-y-1 text-xs text-gray-400 border-t border-gray-700 pt-2">
            <div className="flex justify-between"><span>Draw box</span><kbd className="bg-gray-700 px-1.5 py-0.5 rounded text-gray-300">Ctrl + drag</kbd></div>
            <div className="flex justify-between"><span>Move mode</span><kbd className="bg-gray-700 px-1.5 py-0.5 rounded text-gray-300">W</kbd></div>
            <div className="flex justify-between"><span>Rotate mode</span><kbd className="bg-gray-700 px-1.5 py-0.5 rounded text-gray-300">E</kbd></div>
            <div className="flex justify-between"><span>Resize mode</span><kbd className="bg-gray-700 px-1.5 py-0.5 rounded text-gray-300">R</kbd></div>
            <div className="flex justify-between"><span>Delete selected</span><kbd className="bg-gray-700 px-1.5 py-0.5 rounded text-gray-300">Del / Backspace</kbd></div>
            <div className="flex justify-between"><span>Deselect</span><kbd className="bg-gray-700 px-1.5 py-0.5 rounded text-gray-300">Esc</kbd></div>
            <div className="flex justify-between"><span>Save</span><kbd className="bg-gray-700 px-1.5 py-0.5 rounded text-gray-300">Ctrl + S</kbd></div>
          </div>
        )}
      </div>

      {/* Selected-box inspector */}
      {selected && (
        <div className="bg-gray-800 rounded-lg p-4 space-y-3" data-testid="annotation-inspector">
          <div className="flex items-center justify-between">
            <h3 className="font-bold text-sm">Selected box</h3>
            <button onClick={() => anno.select(null)} className="text-gray-500 hover:text-white text-xs">
              ✕
            </button>
          </div>

          <div className="flex rounded overflow-hidden border border-gray-700">
            {MODES.map((m) => (
              <button
                key={m}
                onClick={() => onModeChange(m)}
                className={`flex-1 px-2 py-1 text-xs ${
                  mode === m ? 'bg-blue-600 text-white' : 'bg-gray-700/50 text-gray-300 hover:bg-gray-700'
                }`}
              >
                {MODE_LABEL[m]}
              </button>
            ))}
          </div>

          <div>
            <label className="block text-xs text-gray-400 mb-1">Label</label>
            <select
              value={selected.label}
              onChange={(e) => anno.updateBox(selected.id, { label: e.target.value })}
              className="w-full bg-gray-700 rounded px-2 py-1.5 text-sm"
            >
              {labels.map((l) => (
                <option key={l.name} value={l.name}>
                  {l.name}
                </option>
              ))}
            </select>
          </div>

          <Vec3Input
            label="Center (x, y, z)"
            value={selected.center}
            step={0.1}
            onChange={(v) => anno.updateBox(selected.id, { center: v })}
          />
          <Vec3Input
            label="Size (w, l, h)"
            value={selected.size}
            step={0.05}
            min={0.01}
            onChange={(v) => anno.updateBox(selected.id, { size: v })}
          />
          <div>
            <label className="block text-xs text-gray-400 mb-1">Heading (deg)</label>
            <input
              type="number"
              step={1}
              value={+((selected.heading * 180) / Math.PI).toFixed(1)}
              onChange={(e) =>
                anno.updateBox(selected.id, { heading: (parseFloat(e.target.value || '0') * Math.PI) / 180 })
              }
              className="w-full bg-gray-700 rounded px-2 py-1 text-sm"
            />
          </div>

          <button
            onClick={() => confirmDelete(selected.id)}
            className="w-full px-2 py-1.5 text-sm bg-red-700/70 rounded hover:bg-red-700"
          >
            Delete box
          </button>
        </div>
      )}

      {/* Box list */}
      <div className="bg-gray-800 rounded-lg flex-1 overflow-hidden flex flex-col">
        <div className="p-3 border-b border-gray-700">
          <h3 className="font-bold text-sm">Boxes ({boxes.length})</h3>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-1">
          {boxes.map((b) => (
            <div
              key={b.id}
              data-testid={`annotation-box-${b.id}`}
              className={`w-full rounded px-2.5 py-1.5 text-sm flex items-center gap-2 ${
                b.id === selectedId
                  ? 'bg-blue-600/30 border border-blue-500'
                  : 'bg-gray-700/50 hover:bg-gray-700 border border-transparent'
              }`}
            >
              <button onClick={() => anno.select(b.id)} className="flex items-center gap-2 flex-1 text-left">
                <span className="w-2.5 h-2.5 rounded-sm flex-shrink-0" style={{ background: anno.labelColor(b.label) }} />
                <span className="capitalize flex-1">{b.label}</span>
              </button>
              <button
                onClick={() => confirmDelete(b.id)}
                className="text-gray-500 hover:text-red-400 text-xs"
                aria-label="Delete box"
              >
                ✕
              </button>
            </div>
          ))}
          {boxes.length === 0 && (
            <p className="text-center text-gray-500 text-sm py-6">No boxes yet. Ctrl/⌘ + drag to draw.</p>
          )}
        </div>
      </div>

      {/* Save + Train */}
      <div className="bg-gray-800 rounded-lg p-4 space-y-2">
        {saveError && <p className="text-xs text-red-400">{saveError}</p>}
        <button
          data-testid="annotation-save-button"
          disabled={!dirty || saving}
          onClick={() => anno.save().catch(() => {})}
          className={`w-full px-3 py-2 rounded text-sm font-medium ${
            !dirty || saving ? 'bg-gray-700 text-gray-500 cursor-not-allowed' : 'bg-blue-600 hover:bg-blue-500 text-white'
          }`}
        >
          {saving ? 'Saving…' : dirty ? 'Save annotations' : 'Saved'}
        </button>
        <button
          data-testid="annotation-train-button"
          disabled={boxes.length === 0 || dirty}
          onClick={onRequestTrain}
          title={boxes.length === 0 ? 'Add boxes first' : dirty ? 'Save before training' : ''}
          className={`w-full px-3 py-2 rounded text-sm font-medium ${
            boxes.length === 0 || dirty
              ? 'bg-gray-700 text-gray-500 cursor-not-allowed'
              : 'bg-green-600 hover:bg-green-500 text-white'
          }`}
        >
          Start fine-tuning
        </button>
      </div>
    </div>
  );
}
