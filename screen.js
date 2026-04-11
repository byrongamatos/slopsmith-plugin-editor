/* Slopsmith Arrangement Editor — DAW-style timeline note editor */

(function () {
'use strict';

// ════════════════════════════════════════════════════════════════════
// Constants
// ════════════════════════════════════════════════════════════════════

const STRING_COLORS = [
    '#FC3A51', // 0 low E — red
    '#FFC600', // 1 A     — yellow
    '#3FAAFF', // 2 D     — blue
    '#FF8A00', // 3 G     — orange
    '#58D263', // 4 B     — green
    '#C473FF', // 5 high e — purple
];
// Display: lane 0 = high e (top), lane 5 = low E (bottom)
const LANE_LABELS = ['e', 'B', 'G', 'D', 'A', 'E'];

let WAVEFORM_H = 70;
let LANE_H = 44;
const LANES = 6;
let BEAT_H = 24;
const LABEL_W = 52;
const MIN_NOTE_W = 18;
const NOTE_PAD = 3;
const SNAP_VALUES = [1, 0.5, 0.25, 0.125, 0.0625, 0]; // 1/1 … 1/16, off
const DPR = window.devicePixelRatio || 1;

// ── Piano roll constants ────────────────────────────────────────────
const PIANO_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
const PIANO_OCTAVE_COLORS = [
    '#ff4466', '#ff8844', '#ffcc33', '#66dd55', '#44ccaa',
    '#44aaff', '#7766ff', '#cc55ff', '#ff55aa', '#aaaaaa',
];
let PIANO_LANE_H = 10;  // pixels per MIDI semitone
let pianoRange = { lo: 36, hi: 96 }; // MIDI range, updated per arrangement
const KEYS_PATTERN = /^keys/i;

// ════════════════════════════════════════════════════════════════════
// State
// ════════════════════════════════════════════════════════════════════

const S = {
    // Song data
    title: '', artist: '', sessionId: null, filename: '',
    arrangements: [],
    currentArr: 0,
    beats: [], sections: [], duration: 0, offset: 0,

    // View
    scrollX: 0,   // seconds
    zoom: 120,     // px per second
    snapIdx: 2,    // default 1/4

    // Selection
    sel: new Set(),

    // Drag state
    drag: null, // { type, startX, startY, startTime, startString, noteIdx, origTimes, origStrings }

    // Playback
    playing: false,
    cursorTime: 0,
    audioCtx: null, audioBuffer: null, audioSource: null,
    playStartWall: 0, playStartTime: 0,

    // Waveform cache
    waveformPeaks: null,

    // History
    history: null,

    // Songs list cache
    songsList: null,

    // Clipboard
    clipboard: null, // { notes: [...], baseTime }
};

let canvas, ctx;
let rafId = null;

// ════════════════════════════════════════════════════════════════════
// Coordinate mapping
// ════════════════════════════════════════════════════════════════════

function timeToX(t)  { return LABEL_W + (t - S.scrollX) * S.zoom; }
function xToTime(x)  { return (x - LABEL_W) / S.zoom + S.scrollX; }
function laneToY(l)  { return WAVEFORM_H + l * LANE_H; }
function yToLane(y)  { return Math.floor((y - WAVEFORM_H) / LANE_H); }
function strToLane(s) { return 5 - s; }
function laneToStr(l) { return 5 - l; }
function strToY(s)   { return laneToY(strToLane(s)); }
function yToStr(y)   { const l = Math.max(0, Math.min(5, yToLane(y))); return laneToStr(l); }
function canvasH()   {
    if (isKeysMode()) return WAVEFORM_H + pianoLaneCount() * PIANO_LANE_H + BEAT_H;
    return WAVEFORM_H + LANES * LANE_H + BEAT_H;
}

// ── Piano roll mode helpers ─────────────────────────────────────────

function isKeysMode() {
    if (!S.arrangements.length) return false;
    const arr = S.arrangements[S.currentArr];
    return arr && KEYS_PATTERN.test(arr.name || '');
}

function pianoLaneCount() { return pianoRange.hi - pianoRange.lo + 1; }

function midiToNote(midi) { return PIANO_NOTE_NAMES[midi % 12] + (Math.floor(midi / 12) - 1); }
function isBlackKey(midi) { const pc = midi % 12; return pc===1||pc===3||pc===6||pc===8||pc===10; }

function noteToMidi(string, fret) { return string * 24 + fret; }
function midiToString(midi) { return Math.floor(midi / 24); }
function midiToFret(midi) { return midi % 24; }

// Piano roll Y: higher MIDI = higher on screen (lower Y)
function midiToY(midi) { return WAVEFORM_H + (pianoRange.hi - midi) * PIANO_LANE_H; }
function yToMidi(y) { return pianoRange.hi - Math.floor((y - WAVEFORM_H) / PIANO_LANE_H); }

function updatePianoRange() {
    const nn = notes();
    let lo = 127, hi = 0;
    for (const n of nn) {
        const m = noteToMidi(n.string, n.fret);
        if (m < lo) lo = m;
        if (m > hi) hi = m;
    }
    if (lo > hi) { lo = 48; hi = 84; }
    // Expand to octave boundaries with padding
    lo = Math.max(0, Math.floor(lo / 12) * 12 - 6);
    hi = Math.min(127, Math.ceil((hi + 1) / 12) * 12 + 5);
    pianoRange = { lo, hi };
    // Adjust lane height to fill available space nicely
    PIANO_LANE_H = Math.max(6, Math.min(14, 350 / (hi - lo + 1)));
}

function snapTime(t) {
    const sv = SNAP_VALUES[S.snapIdx];
    if (sv === 0 || S.beats.length < 2) return t;
    // Find surrounding beat
    let bi = 0;
    for (let i = 0; i < S.beats.length - 1; i++) {
        if (S.beats[i].time <= t) bi = i; else break;
    }
    const bt = S.beats[bi].time;
    const nt = bi < S.beats.length - 1 ? S.beats[bi + 1].time : bt + 0.5;
    const bd = nt - bt;
    const subs = 1 / sv;
    const sd = bd / subs;
    const idx = Math.round((t - bt) / sd);
    return bt + idx * sd;
}

// ════════════════════════════════════════════════════════════════════
// Note accessors
// ════════════════════════════════════════════════════════════════════

function notes() { return S.arrangements.length ? S.arrangements[S.currentArr].notes : []; }
function chords() { return S.arrangements.length ? S.arrangements[S.currentArr].chords : []; }

// Flatten chord notes into the main notes array on load, tagging with _fromChord.
// On save, reconstruct chords from notes sharing the same time+_fromChord group.
function flattenChords() {
    if (!S.arrangements.length) return;
    const arr = S.arrangements[S.currentArr];
    for (const ch of arr.chords) {
        for (const cn of ch.notes) {
            arr.notes.push({
                time: cn.time || ch.time,
                string: cn.string,
                fret: cn.fret,
                sustain: cn.sustain || 0,
                techniques: cn.techniques || {},
                _fromChord: true,
                _chordId: ch.chord_id,
            });
        }
    }
    arr.chords = [];
    arr.notes.sort((a, b) => a.time - b.time);
}

// Reconstruct chords from notes at the same time before saving
function reconstructChords() {
    if (!S.arrangements.length) return;
    const arr = S.arrangements[S.currentArr];
    const byTime = {};
    const soloNotes = [];
    for (const n of arr.notes) {
        const key = n.time.toFixed(4);
        if (!byTime[key]) byTime[key] = [];
        byTime[key].push(n);
    }
    const newNotes = [];
    const newChords = [];
    const chordTemplates = arr.chord_templates || [];
    const templateMap = {};

    for (const key of Object.keys(byTime).sort((a, b) => parseFloat(a) - parseFloat(b))) {
        const group = byTime[key];
        if (group.length === 1) {
            newNotes.push(group[0]);
        } else {
            // Multiple notes at same time = chord
            const frets = [-1, -1, -1, -1, -1, -1];
            for (const n of group) {
                if (n.string >= 0 && n.string < 6) frets[n.string] = n.fret;
            }
            const fretKey = frets.join(',');
            let tmplIdx;
            if (fretKey in templateMap) {
                tmplIdx = templateMap[fretKey];
            } else {
                tmplIdx = chordTemplates.length;
                chordTemplates.push({
                    name: '',
                    frets: [...frets],
                    fingers: [-1, -1, -1, -1, -1, -1],
                });
                templateMap[fretKey] = tmplIdx;
            }
            newChords.push({
                time: group[0].time,
                chord_id: tmplIdx,
                high_density: false,
                notes: group.map(n => ({
                    time: n.time,
                    string: n.string,
                    fret: n.fret,
                    sustain: n.sustain || 0,
                    techniques: n.techniques || {},
                })),
            });
        }
    }
    arr.notes = newNotes;
    arr.chords = newChords;
    arr.chord_templates = chordTemplates;
}

// ════════════════════════════════════════════════════════════════════
// Drawing
// ════════════════════════════════════════════════════════════════════

function draw() {
    if (!canvas) return;
    const w = canvas.width / DPR;
    const h = canvas.height / DPR;
    ctx.save();
    ctx.scale(DPR, DPR);
    ctx.clearRect(0, 0, w, h);

    drawWaveform(w);
    drawLanes(w);
    drawGrid(w);
    drawSections(w);
    drawBeatBar(w);
    drawNotes(w);
    drawSelectionRect(w);
    drawCursor(w, h);
    drawLabels(w);

    ctx.restore();
}

function drawWaveform(w) {
    ctx.fillStyle = '#08081a';
    ctx.fillRect(0, 0, w, WAVEFORM_H);
    if (!S.waveformPeaks) return;

    const peaks = S.waveformPeaks;
    const mid = WAVEFORM_H / 2;
    ctx.fillStyle = '#4080e060';
    for (let px = LABEL_W; px < w; px++) {
        const t = xToTime(px);
        if (t < 0 || t >= S.duration) continue;
        const i = Math.floor(t / S.duration * peaks.length);
        if (i < 0 || i >= peaks.length) continue;
        const bh = peaks[i] * (WAVEFORM_H / 2 - 4);
        ctx.fillRect(px, mid - bh, 1, bh * 2);
    }
}

function drawLanes(w) {
    if (isKeysMode()) return drawPianoLanes(w);
    for (let l = 0; l < LANES; l++) {
        const y = laneToY(l);
        ctx.fillStyle = l % 2 === 0 ? '#0c0c1c' : '#0f0f24';
        ctx.fillRect(LABEL_W, y, w - LABEL_W, LANE_H);
        // Separator
        ctx.strokeStyle = '#1a1a35';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(LABEL_W, y + LANE_H);
        ctx.lineTo(w, y + LANE_H);
        ctx.stroke();
    }
}

function drawPianoLanes(w) {
    for (let midi = pianoRange.lo; midi <= pianoRange.hi; midi++) {
        const y = midiToY(midi);
        const black = isBlackKey(midi);
        ctx.fillStyle = black ? '#0a0a1a' : '#0e0e22';
        ctx.fillRect(LABEL_W, y, w - LABEL_W, PIANO_LANE_H);

        // Octave boundary (C notes)
        if (midi % 12 === 0) {
            ctx.strokeStyle = '#2a2a55';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(LABEL_W, y + PIANO_LANE_H);
            ctx.lineTo(w, y + PIANO_LANE_H);
            ctx.stroke();
        }
    }
}

function drawGrid(w) {
    const st = S.scrollX - 1;
    const et = S.scrollX + (w - LABEL_W) / S.zoom + 1;
    const laneBottom = isKeysMode()
        ? WAVEFORM_H + pianoLaneCount() * PIANO_LANE_H
        : WAVEFORM_H + LANES * LANE_H;
    for (const b of S.beats) {
        if (b.time < st || b.time > et) continue;
        const x = timeToX(b.time);
        if (x < LABEL_W || x > w) continue;
        const meas = b.measure > 0;
        ctx.strokeStyle = meas ? '#2a2a50' : '#16162c';
        ctx.lineWidth = meas ? 1.5 : 0.5;
        ctx.beginPath();
        ctx.moveTo(x, WAVEFORM_H);
        ctx.lineTo(x, laneBottom);
        ctx.stroke();
    }
}

function drawSections(w) {
    const st = S.scrollX - 1;
    const et = S.scrollX + (w - LABEL_W) / S.zoom + 1;
    const laneBottom = isKeysMode()
        ? WAVEFORM_H + pianoLaneCount() * PIANO_LANE_H
        : WAVEFORM_H + LANES * LANE_H;
    ctx.font = '9px monospace';
    ctx.textBaseline = 'top';
    for (const s of S.sections) {
        if (s.start_time < st || s.start_time > et) continue;
        const x = timeToX(s.start_time);
        if (x < LABEL_W || x > w) continue;
        // Dashed vertical line
        ctx.strokeStyle = '#e8c04060';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(x, WAVEFORM_H);
        ctx.lineTo(x, laneBottom);
        ctx.stroke();
        ctx.setLineDash([]);
        // Label at top of lanes
        ctx.fillStyle = '#e8c040';
        ctx.textAlign = 'left';
        ctx.fillText(s.name, x + 3, WAVEFORM_H + 2);
    }
}

function drawBeatBar(w) {
    const y = WAVEFORM_H + LANES * LANE_H;
    ctx.fillStyle = '#08081a';
    ctx.fillRect(0, y, w, BEAT_H);
    ctx.fillStyle = '#08081a';
    ctx.fillRect(0, y, LABEL_W, BEAT_H);

    ctx.fillStyle = '#555';
    ctx.font = '9px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const st = S.scrollX - 1;
    const et = S.scrollX + (w - LABEL_W) / S.zoom + 1;
    for (const b of S.beats) {
        if (b.measure <= 0 || b.time < st || b.time > et) continue;
        const x = timeToX(b.time);
        if (x < LABEL_W || x > w) continue;
        ctx.fillText(String(b.measure), x, y + BEAT_H / 2);
    }
}

function drawLabels(w) {
    // Waveform label
    ctx.fillStyle = '#0a0a1a';
    ctx.fillRect(0, 0, LABEL_W, WAVEFORM_H);
    ctx.fillStyle = '#555';
    ctx.font = '9px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('Audio', LABEL_W / 2, WAVEFORM_H / 2);

    if (isKeysMode()) return drawPianoLabels(w);

    // String labels
    for (let l = 0; l < LANES; l++) {
        const y = laneToY(l);
        ctx.fillStyle = '#0a0a1a';
        ctx.fillRect(0, y, LABEL_W, LANE_H);
        const s = laneToStr(l);
        ctx.fillStyle = STRING_COLORS[s];
        ctx.font = 'bold 12px monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(LANE_LABELS[l], LABEL_W / 2, y + LANE_H / 2);
    }
}

function drawPianoLabels() {
    // MIDI note labels on the left axis
    ctx.font = '8px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (let midi = pianoRange.lo; midi <= pianoRange.hi; midi++) {
        const y = midiToY(midi);
        ctx.fillStyle = '#0a0a1a';
        ctx.fillRect(0, y, LABEL_W, PIANO_LANE_H);

        // Only label C notes and F notes to avoid clutter
        if (midi % 12 === 0 || midi % 12 === 5) {
            const octave = Math.floor(midi / 12) - 1;
            const color = PIANO_OCTAVE_COLORS[Math.min(octave + 1, PIANO_OCTAVE_COLORS.length - 1)];
            ctx.fillStyle = color;
            ctx.fillText(midiToNote(midi), LABEL_W / 2, y + PIANO_LANE_H / 2);
        }
    }
}

function drawNotes(w) {
    const nn = notes();
    const st = S.scrollX - 2;
    const et = S.scrollX + (w - LABEL_W) / S.zoom + 2;
    const keysMode = isKeysMode();
    for (let i = 0; i < nn.length; i++) {
        const n = nn[i];
        if (n.time + (n.sustain || 0) < st || n.time > et) continue;
        if (keysMode) {
            _drawPianoNote(n, S.sel.has(i));
        } else {
            _drawNote(n, S.sel.has(i));
        }
    }
}

function _drawNote(n, selected) {
    const x = timeToX(n.time);
    const y = strToY(n.string) + NOTE_PAD;
    const sw = Math.max(MIN_NOTE_W, (n.sustain || 0) * S.zoom);
    const h = LANE_H - NOTE_PAD * 2;
    const color = STRING_COLORS[n.string] || '#888';

    // Body
    ctx.fillStyle = color + 'cc';
    ctx.beginPath();
    ctx.roundRect(x, y, sw, h, 3);
    ctx.fill();

    // Border
    if (selected) {
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 2;
    } else {
        ctx.strokeStyle = color;
        ctx.lineWidth = 0.5;
    }
    ctx.beginPath();
    ctx.roundRect(x, y, sw, h, 3);
    ctx.stroke();

    // Fret number
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 13px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(String(n.fret), x + Math.min(sw, MIN_NOTE_W) / 2, y + h / 2);

    // Technique badges
    const techs = n.techniques || {};
    const badges = [];
    if (techs.hammer_on) badges.push('H');
    if (techs.pull_off) badges.push('P');
    if (techs.slide_to >= 0) badges.push('/' + techs.slide_to);
    if (techs.bend > 0) badges.push('b');
    if (techs.harmonic) badges.push('*');
    if (techs.palm_mute) badges.push('PM');
    if (techs.tap) badges.push('T');
    if (techs.tremolo) badges.push('~');
    if (techs.mute) badges.push('x');
    if (badges.length) {
        ctx.fillStyle = '#ffffffbb';
        ctx.font = '7px monospace';
        ctx.textAlign = 'left';
        ctx.fillText(badges.join(' '), x + 2, y + 9);
    }

    // Sustain tail
    if (sw > MIN_NOTE_W) {
        ctx.fillStyle = color + '40';
        ctx.fillRect(x + MIN_NOTE_W, y + h / 2 - 2, sw - MIN_NOTE_W, 4);
    }
}

function _drawPianoNote(n, selected) {
    const midi = noteToMidi(n.string, n.fret);
    if (midi < pianoRange.lo || midi > pianoRange.hi) return;

    const x = timeToX(n.time);
    const y = midiToY(midi) + 1;
    const sw = Math.max(MIN_NOTE_W, (n.sustain || 0) * S.zoom);
    const h = PIANO_LANE_H - 2;
    const octave = Math.floor(midi / 12);
    const color = PIANO_OCTAVE_COLORS[Math.min(octave, PIANO_OCTAVE_COLORS.length - 1)];

    // Body
    ctx.fillStyle = color + 'cc';
    ctx.beginPath();
    ctx.roundRect(x, y, sw, h, 2);
    ctx.fill();

    // Border
    if (selected) {
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 2;
    } else {
        ctx.strokeStyle = color;
        ctx.lineWidth = 0.5;
    }
    ctx.beginPath();
    ctx.roundRect(x, y, sw, h, 2);
    ctx.stroke();

    // Note name (only if enough space)
    if (sw >= 20 && h >= 8) {
        ctx.fillStyle = '#000';
        ctx.font = `bold ${Math.min(9, h - 1)}px monospace`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(midiToNote(midi), x + Math.min(sw, 24) / 2, y + h / 2);
    }
}

function drawCursor(w, h) {
    const x = timeToX(S.cursorTime);
    if (x < LABEL_W || x > w) return;
    ctx.strokeStyle = '#ff4444';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, canvasH());
    ctx.stroke();
}

function drawSelectionRect() {
    if (!S.drag || S.drag.type !== 'select') return;
    const x1 = Math.min(S.drag.startX, S.drag.curX);
    const y1 = Math.min(S.drag.startY, S.drag.curY);
    const x2 = Math.max(S.drag.startX, S.drag.curX);
    const y2 = Math.max(S.drag.startY, S.drag.curY);
    ctx.strokeStyle = '#4080e0';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    ctx.setLineDash([]);
    ctx.fillStyle = '#4080e018';
    ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
}

// ════════════════════════════════════════════════════════════════════
// Hit testing
// ════════════════════════════════════════════════════════════════════

const EDGE_GRAB = 8; // pixels from right edge to trigger resize

function hitNote(mx, my) {
    const nn = notes();
    const keysMode = isKeysMode();
    for (let i = nn.length - 1; i >= 0; i--) {
        const n = nn[i];
        const x = timeToX(n.time);
        let y, w, h;
        if (keysMode) {
            const midi = noteToMidi(n.string, n.fret);
            y = midiToY(midi) + 1;
            w = Math.max(MIN_NOTE_W, (n.sustain || 0) * S.zoom);
            h = PIANO_LANE_H - 2;
        } else {
            y = strToY(n.string) + NOTE_PAD;
            w = Math.max(MIN_NOTE_W, (n.sustain || 0) * S.zoom);
            h = LANE_H - NOTE_PAD * 2;
        }
        if (mx >= x && mx <= x + w && my >= y && my <= y + h) return i;
    }
    return -1;
}

function hitNoteEdge(mx, my) {
    // Returns note index if mouse is near the right edge of a note (for sustain resize)
    const nn = notes();
    for (let i = nn.length - 1; i >= 0; i--) {
        const n = nn[i];
        const x = timeToX(n.time);
        const y = strToY(n.string) + NOTE_PAD;
        const w = Math.max(MIN_NOTE_W, (n.sustain || 0) * S.zoom);
        const h = LANE_H - NOTE_PAD * 2;
        const rightEdge = x + w;
        if (mx >= rightEdge - EDGE_GRAB && mx <= rightEdge + EDGE_GRAB && my >= y && my <= y + h) return i;
    }
    return -1;
}

// ════════════════════════════════════════════════════════════════════
// Undo / Redo
// ════════════════════════════════════════════════════════════════════

class EditHistory {
    constructor() { this.undo = []; this.redo = []; }
    exec(cmd) { cmd.exec(); this.undo.push(cmd); this.redo = []; this._ui(); }
    doUndo() { if (!this.undo.length) return; const c = this.undo.pop(); c.rollback(); this.redo.push(c); this._ui(); draw(); }
    doRedo() { if (!this.redo.length) return; const c = this.redo.pop(); c.exec(); this.undo.push(c); this._ui(); draw(); }
    _ui() {
        const u = document.getElementById('editor-undo');
        const r = document.getElementById('editor-redo');
        if (u) u.disabled = !this.undo.length;
        if (r) r.disabled = !this.redo.length;
    }
}

class MoveNoteCmd {
    constructor(indices, dtimes, dstrings, dfrets) {
        this.indices = indices;
        this.dtimes = dtimes;
        this.dstrings = dstrings;
        this.dfrets = dfrets; // null for guitar mode, array for piano mode
    }
    exec() {
        const nn = notes();
        for (let i = 0; i < this.indices.length; i++) {
            nn[this.indices[i]].time += this.dtimes[i];
            nn[this.indices[i]].string += this.dstrings[i];
            if (this.dfrets) nn[this.indices[i]].fret += this.dfrets[i];
        }
    }
    rollback() {
        const nn = notes();
        for (let i = 0; i < this.indices.length; i++) {
            nn[this.indices[i]].time -= this.dtimes[i];
            nn[this.indices[i]].string -= this.dstrings[i];
            if (this.dfrets) nn[this.indices[i]].fret -= this.dfrets[i];
        }
    }
}

class AddNoteCmd {
    constructor(note) { this.note = note; this.idx = -1; }
    exec() {
        const nn = notes();
        nn.push(this.note);
        this.idx = nn.length - 1;
        nn.sort((a, b) => a.time - b.time);
        // Find new index
        this.idx = nn.indexOf(this.note);
    }
    rollback() {
        const nn = notes();
        const i = nn.indexOf(this.note);
        if (i >= 0) nn.splice(i, 1);
    }
}

class DeleteNotesCmd {
    constructor(indices) {
        this.indices = [...indices].sort((a, b) => b - a);
        this.removed = [];
    }
    exec() {
        const nn = notes();
        this.removed = [];
        for (const i of this.indices) {
            this.removed.push({ idx: i, note: nn[i] });
            nn.splice(i, 1);
        }
        S.sel.clear();
    }
    rollback() {
        const nn = notes();
        for (const r of [...this.removed].reverse()) {
            nn.splice(r.idx, 0, r.note);
        }
    }
}

class ResizeSustainCmd {
    constructor(index, newSustain) {
        this.index = index;
        this.newSustain = newSustain;
        this.oldSustain = notes()[index].sustain || 0;
    }
    exec() { notes()[this.index].sustain = this.newSustain; }
    rollback() { notes()[this.index].sustain = this.oldSustain; }
}

class ChangeFretCmd {
    constructor(index, newFret) {
        this.index = index;
        this.newFret = newFret;
        this.oldFret = notes()[index].fret;
    }
    exec() { notes()[this.index].fret = this.newFret; }
    rollback() { notes()[this.index].fret = this.oldFret; }
}

// ════════════════════════════════════════════════════════════════════
// Mouse interactions
// ════════════════════════════════════════════════════════════════════

function getMousePos(e) {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
}

function onMouseDown(e) {
    const { x, y } = getMousePos(e);
    hideContextMenu();
    hideAddNote();

    // Middle button = pan
    if (e.button === 1) {
        e.preventDefault();
        S.drag = { type: 'pan', startX: x, origScroll: S.scrollX };
        return;
    }

    // Right button = context menu (handled in onContextMenu)
    if (e.button === 2) return;

    // Left button
    if (y < WAVEFORM_H) {
        // Click on waveform = set cursor
        S.cursorTime = Math.max(0, xToTime(x));
        if (S.playing) { stopPlayback(); startPlayback(); }
        draw();
        return;
    }

    // Check for sustain edge grab first
    const edgeIdx = hitNoteEdge(x, y);
    if (edgeIdx >= 0) {
        if (!S.sel.has(edgeIdx)) { S.sel.clear(); S.sel.add(edgeIdx); }
        const n = notes()[edgeIdx];
        S.drag = {
            type: 'resize',
            noteIdx: edgeIdx,
            startX: x,
            origSustain: n.sustain || 0,
        };
        draw();
        return;
    }

    const idx = hitNote(x, y);

    if (idx >= 0) {
        // Click on note — also select all chord siblings (same time)
        const nn = notes();
        const clickedTime = nn[idx].time;
        const chordSiblings = [];
        for (let i = 0; i < nn.length; i++) {
            if (Math.abs(nn[i].time - clickedTime) < 0.001) chordSiblings.push(i);
        }
        const isChord = chordSiblings.length > 1;

        if (e.shiftKey) {
            // Multi-select toggle — toggle the whole chord group
            const allSelected = chordSiblings.every(i => S.sel.has(i));
            for (const i of chordSiblings) {
                if (allSelected) S.sel.delete(i); else S.sel.add(i);
            }
        } else if (!S.sel.has(idx)) {
            S.sel.clear();
            for (const i of chordSiblings) S.sel.add(i);
        }

        // Start drag
        const selArr = [...S.sel];
        S.drag = {
            type: 'move',
            startX: x, startY: y,
            origTimes: selArr.map(i => nn[i].time),
            origStrings: selArr.map(i => nn[i].string),
            origFrets: selArr.map(i => nn[i].fret),
            indices: selArr,
            moved: false,
        };
        draw();
    } else {
        // Click on empty space = start selection rect or deselect
        if (!e.shiftKey) S.sel.clear();
        S.drag = {
            type: 'select',
            startX: x, startY: y,
            curX: x, curY: y,
        };
        draw();
    }
}

function onMouseMove(e) {
    const { x, y } = getMousePos(e);

    // Cursor hint when not dragging
    if (!S.drag) {
        if (canvas && y >= WAVEFORM_H && y < WAVEFORM_H + LANES * LANE_H) {
            canvas.style.cursor = hitNoteEdge(x, y) >= 0 ? 'ew-resize' : '';
        } else if (canvas) {
            canvas.style.cursor = '';
        }
        return;
    }

    if (S.drag.type === 'pan') {
        const dx = x - S.drag.startX;
        S.scrollX = Math.max(0, S.drag.origScroll - dx / S.zoom);
        draw();
        return;
    }

    if (S.drag.type === 'select') {
        S.drag.curX = x;
        S.drag.curY = y;
        draw();
        return;
    }

    if (S.drag.type === 'resize') {
        const dt = (x - S.drag.startX) / S.zoom;
        const nn = notes();
        nn[S.drag.noteIdx].sustain = Math.max(0, S.drag.origSustain + dt);
        draw();
        return;
    }

    if (S.drag.type === 'move') {
        S.drag.moved = true;
        const nn = notes();
        const dt = (x - S.drag.startX) / S.zoom;
        const dy = y - S.drag.startY;

        if (isKeysMode()) {
            const dMidi = -Math.round(dy / PIANO_LANE_H);
            for (let i = 0; i < S.drag.indices.length; i++) {
                const ni = S.drag.indices[i];
                let newTime = S.drag.origTimes[i] + dt;
                newTime = snapTime(Math.max(0, newTime));
                nn[ni].time = newTime;

                const origMidi = noteToMidi(S.drag.origStrings[i], S.drag.origFrets[i]);
                const newMidi = Math.max(0, Math.min(143, origMidi + dMidi));
                nn[ni].string = midiToString(newMidi);
                nn[ni].fret = midiToFret(newMidi);
            }
        } else {
            const dLanes = Math.round(dy / LANE_H);
            for (let i = 0; i < S.drag.indices.length; i++) {
                const ni = S.drag.indices[i];
                let newTime = S.drag.origTimes[i] + dt;
                newTime = snapTime(Math.max(0, newTime));
                nn[ni].time = newTime;

                const origLane = strToLane(S.drag.origStrings[i]);
                const newLane = Math.max(0, Math.min(5, origLane + dLanes));
                nn[ni].string = laneToStr(newLane);
            }
        }
        draw();
    }
}

function onMouseUp(e) {
    if (!S.drag) return;
    const { x, y } = getMousePos(e);

    if (S.drag.type === 'resize') {
        const nn = notes();
        const finalSustain = nn[S.drag.noteIdx].sustain;
        // Revert so the command can apply it
        nn[S.drag.noteIdx].sustain = S.drag.origSustain;
        if (finalSustain !== S.drag.origSustain) {
            S.history.exec(new ResizeSustainCmd(S.drag.noteIdx, finalSustain));
        }
    }

    if (S.drag.type === 'move' && S.drag.moved) {
        // Commit move as undo command
        const nn = notes();
        const dtimes = S.drag.indices.map((ni, i) => nn[ni].time - S.drag.origTimes[i]);
        const dstrings = S.drag.indices.map((ni, i) => nn[ni].string - S.drag.origStrings[i]);
        const dfrets = isKeysMode()
            ? S.drag.indices.map((ni, i) => nn[ni].fret - S.drag.origFrets[i])
            : null;

        // Revert to original first so exec() applies the delta
        for (let i = 0; i < S.drag.indices.length; i++) {
            nn[S.drag.indices[i]].time = S.drag.origTimes[i];
            nn[S.drag.indices[i]].string = S.drag.origStrings[i];
            if (dfrets) nn[S.drag.indices[i]].fret = S.drag.origFrets[i];
        }
        S.history.exec(new MoveNoteCmd(S.drag.indices, dtimes, dstrings, dfrets));
    }

    if (S.drag.type === 'select') {
        // Select notes inside rectangle
        const x1 = Math.min(S.drag.startX, S.drag.curX);
        const y1 = Math.min(S.drag.startY, S.drag.curY);
        const x2 = Math.max(S.drag.startX, S.drag.curX);
        const y2 = Math.max(S.drag.startY, S.drag.curY);

        const nn = notes();
        const keysMode = isKeysMode();
        for (let i = 0; i < nn.length; i++) {
            const nx = timeToX(nn[i].time);
            let ny;
            if (keysMode) {
                const midi = noteToMidi(nn[i].string, nn[i].fret);
                ny = midiToY(midi) + PIANO_LANE_H / 2;
            } else {
                ny = strToY(nn[i].string) + LANE_H / 2;
            }
            if (nx >= x1 && nx <= x2 && ny >= y1 && ny <= y2) {
                S.sel.add(i);
            }
        }
    }

    S.drag = null;
    draw();
    updateStatus();
}

function onDblClick(e) {
    const { x, y } = getMousePos(e);
    const keysMode = isKeysMode();
    const laneBottom = keysMode
        ? WAVEFORM_H + pianoLaneCount() * PIANO_LANE_H
        : WAVEFORM_H + LANES * LANE_H;
    if (y < WAVEFORM_H || y > laneBottom) return;

    const idx = hitNote(x, y);
    if (idx >= 0) return; // double-click on existing note = no-op

    // Show add-note dialog
    const t = snapTime(Math.max(0, xToTime(x)));
    if (keysMode) {
        const midi = yToMidi(y);
        showAddNote(e.clientX, e.clientY, t, midiToString(midi), midiToFret(midi));
    } else {
        const s = yToStr(y);
        showAddNote(e.clientX, e.clientY, t, s);
    }
}

function onWheel(e) {
    e.preventDefault();
    if (e.ctrlKey) {
        // Ctrl+scroll = zoom
        const { x } = getMousePos(e);
        const timeBefore = xToTime(x);
        const factor = e.deltaY < 0 ? 1.15 : 0.87;
        S.zoom = Math.max(20, Math.min(2000, S.zoom * factor));
        // Keep the time under cursor stable
        S.scrollX = timeBefore - (x - LABEL_W) / S.zoom;
        S.scrollX = Math.max(0, S.scrollX);
    } else {
        // Scroll = pan
        S.scrollX = Math.max(0, S.scrollX + e.deltaY / S.zoom * 2);
    }
    updateZoomDisplay();
    draw();
}

function onContextMenu(e) {
    e.preventDefault();
    const { x, y } = getMousePos(e);

    // Right-click on beat bar or lanes with no note = section menu
    const beatBarY = isKeysMode()
        ? WAVEFORM_H + pianoLaneCount() * PIANO_LANE_H
        : WAVEFORM_H + LANES * LANE_H;
    if (y >= beatBarY || (y >= WAVEFORM_H && hitNote(x, y) < 0)) {
        showSectionMenu(e.clientX, e.clientY, xToTime(x));
        return;
    }

    const idx = hitNote(x, y);
    if (idx < 0) return;

    if (!S.sel.has(idx)) {
        S.sel.clear();
        S.sel.add(idx);
    }
    draw();
    showContextMenu(e.clientX, e.clientY, idx);
}

function showSectionMenu(cx, cy, time) {
    const menu = document.getElementById('editor-context-menu');
    // Check if clicking near an existing section
    let nearSection = null;
    for (const s of S.sections) {
        if (Math.abs(s.start_time - time) < 1.0) { nearSection = s; break; }
    }

    let html = '';
    html += `<button class="w-full text-left px-3 py-1 text-xs hover:bg-dark-500" data-action="add">Add Section Here</button>`;
    if (nearSection) {
        html += `<button class="w-full text-left px-3 py-1 text-xs hover:bg-dark-500" data-action="rename">Rename "${nearSection.name}"</button>`;
        html += `<button class="w-full text-left px-3 py-1 text-xs hover:bg-dark-500 text-red-400" data-action="delete">Delete "${nearSection.name}"</button>`;
    }
    menu.innerHTML = html;
    menu.querySelectorAll('[data-action]').forEach(btn => {
        btn.onclick = () => {
            hideContextMenu();
            if (btn.dataset.action === 'add') {
                const name = prompt('Section name:', 'verse');
                if (!name) return;
                const num = S.sections.filter(s => s.name === name).length + 1;
                S.sections.push({ name, number: num, start_time: snapTime(time) });
                S.sections.sort((a, b) => a.start_time - b.start_time);
                draw();
            } else if (btn.dataset.action === 'rename' && nearSection) {
                const name = prompt('New name:', nearSection.name);
                if (name) { nearSection.name = name; draw(); }
            } else if (btn.dataset.action === 'delete' && nearSection) {
                const i = S.sections.indexOf(nearSection);
                if (i >= 0) { S.sections.splice(i, 1); draw(); }
            }
        };
    });
    menu.style.left = cx + 'px';
    menu.style.top = cy + 'px';
    menu.classList.remove('hidden');
}

function onKeyDown(e) {
    // Only handle when editor screen is visible
    const screen = document.getElementById('plugin-editor');
    if (!screen || !screen.classList.contains('active')) return;

    if (e.key === ' ' && !e.target.matches('input, select, textarea')) {
        e.preventDefault();
        editorTogglePlay();
        return;
    }

    if (e.key === 'Delete' || e.key === 'Backspace') {
        if (S.sel.size && !e.target.matches('input, select, textarea')) {
            e.preventDefault();
            S.history.exec(new DeleteNotesCmd([...S.sel]));
            draw();
            updateStatus();
            return;
        }
    }

    if ((e.ctrlKey || e.metaKey) && e.key === 'z') {
        e.preventDefault();
        editorUndo();
        return;
    }
    if ((e.ctrlKey || e.metaKey) && (e.key === 'y' || (e.shiftKey && e.key === 'Z'))) {
        e.preventDefault();
        editorRedo();
        return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
        if (!e.target.matches('input, select, textarea')) {
            e.preventDefault();
            const nn = notes();
            for (let i = 0; i < nn.length; i++) S.sel.add(i);
            draw();
            return;
        }
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'c') {
        if (S.sel.size && !e.target.matches('input, select, textarea')) {
            e.preventDefault();
            const nn = notes();
            const selNotes = [...S.sel].map(i => nn[i]);
            const baseTime = Math.min(...selNotes.map(n => n.time));
            S.clipboard = {
                notes: selNotes.map(n => ({
                    time: n.time - baseTime,
                    string: n.string,
                    fret: n.fret,
                    sustain: n.sustain || 0,
                    techniques: { ...(n.techniques || {}) },
                })),
                baseTime,
            };
            setStatus(`Copied ${selNotes.length} notes`);
            return;
        }
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'v') {
        if (S.clipboard && S.clipboard.notes.length && !e.target.matches('input, select, textarea')) {
            e.preventDefault();
            const pasteTime = S.cursorTime;
            const newNotes = S.clipboard.notes.map(n => ({
                time: n.time + pasteTime,
                string: n.string,
                fret: n.fret,
                sustain: n.sustain,
                techniques: { ...(n.techniques || {}) },
            }));
            // Batch add via a compound command
            const nn = notes();
            const addCmd = {
                _notes: newNotes,
                exec() { for (const n of this._notes) nn.push(n); nn.sort((a, b) => a.time - b.time); },
                rollback() { for (const n of this._notes) { const i = nn.indexOf(n); if (i >= 0) nn.splice(i, 1); } },
            };
            S.history.exec(addCmd);
            // Select pasted notes
            S.sel.clear();
            for (const n of newNotes) { const i = nn.indexOf(n); if (i >= 0) S.sel.add(i); }
            draw();
            updateStatus();
            setStatus(`Pasted ${newNotes.length} notes at cursor`);
            return;
        }
    }
}

// ════════════════════════════════════════════════════════════════════
// Context menu
// ════════════════════════════════════════════════════════════════════

function showContextMenu(cx, cy, idx) {
    const menu = document.getElementById('editor-context-menu');
    const items = [
        { label: 'Change Fret...', action: () => promptFret(idx) },
        { label: 'Bend...', action: () => promptBend(idx) },
        { label: 'Slide To...', action: () => promptSlide(idx) },
        { label: 'Delete', action: () => { S.history.exec(new DeleteNotesCmd([...S.sel])); draw(); updateStatus(); } },
        { type: 'sep' },
        { label: 'Hammer-On', toggle: 'hammer_on', idx },
        { label: 'Pull-Off', toggle: 'pull_off', idx },
        { label: 'Palm Mute', toggle: 'palm_mute', idx },
        { label: 'Harmonic', toggle: 'harmonic', idx },
        { label: 'Accent', toggle: 'accent', idx },
        { label: 'Tap', toggle: 'tap', idx },
        { label: 'Tremolo', toggle: 'tremolo', idx },
        { label: 'Mute', toggle: 'mute', idx },
    ];

    const n = notes()[idx];
    let html = '';
    for (const it of items) {
        if (it.type === 'sep') {
            html += '<div class="border-t border-gray-700 my-1"></div>';
            continue;
        }
        if (it.toggle) {
            const techs = n.techniques || {};
            const on = techs[it.toggle];
            html += `<button class="w-full text-left px-3 py-1 text-xs hover:bg-dark-500 flex items-center gap-2" onclick="editorToggleTech(${idx},'${it.toggle}')">
                <span class="w-3">${on ? '✓' : ''}</span>${it.label}</button>`;
        } else {
            html += `<button class="w-full text-left px-3 py-1 text-xs hover:bg-dark-500" data-action="${items.indexOf(it)}">${it.label}</button>`;
        }
    }
    menu.innerHTML = html;
    // Wire up non-toggle actions
    menu.querySelectorAll('[data-action]').forEach(btn => {
        const actionItem = items[parseInt(btn.dataset.action)];
        btn.onclick = () => { hideContextMenu(); actionItem.action(); };
    });

    menu.style.left = cx + 'px';
    menu.style.top = cy + 'px';
    menu.classList.remove('hidden');
}

function hideContextMenu() {
    document.getElementById('editor-context-menu').classList.add('hidden');
}

function promptFret(idx) {
    hideContextMenu();
    const current = notes()[idx].fret;
    const val = prompt('Fret number (0-24):', current);
    if (val === null) return;
    const fret = Math.max(0, Math.min(24, parseInt(val) || 0));
    S.history.exec(new ChangeFretCmd(idx, fret));
    draw();
}

function promptBend(idx) {
    hideContextMenu();
    const n = notes()[idx];
    const techs = n.techniques || {};
    const current = techs.bend || 0;
    const val = prompt('Bend amount in semitones (0 = none, 1 = full, 0.5 = half):', current);
    if (val === null) return;
    const bend = Math.max(0, Math.min(3, parseFloat(val) || 0));
    if (!n.techniques) n.techniques = {};
    n.techniques.bend = bend;
    draw();
}

function promptSlide(idx) {
    hideContextMenu();
    const n = notes()[idx];
    const techs = n.techniques || {};
    const current = techs.slide_to >= 0 ? techs.slide_to : '';
    const val = prompt('Slide to fret (-1 or empty = no slide):', current);
    if (val === null) return;
    if (!n.techniques) n.techniques = {};
    const fret = parseInt(val);
    n.techniques.slide_to = isNaN(fret) || fret < 0 ? -1 : Math.min(24, fret);
    draw();
}

// ════════════════════════════════════════════════════════════════════
// Add note dialog
// ════════════════════════════════════════════════════════════════════

let addNoteData = null;

function showAddNote(cx, cy, time, string, fret) {
    addNoteData = { time, string };
    const dlg = document.getElementById('editor-add-note-dialog');
    dlg.style.left = cx + 'px';
    dlg.style.top = cy + 'px';
    dlg.classList.remove('hidden');
    const inp = document.getElementById('editor-add-fret');
    inp.value = fret != null ? String(fret) : '0';
    inp.focus();
    inp.select();
}

function hideAddNote() {
    document.getElementById('editor-add-note-dialog').classList.add('hidden');
    addNoteData = null;
}

window.editorConfirmAddNote = function() {
    if (!addNoteData) return;
    const fret = Math.max(0, Math.min(24, parseInt(document.getElementById('editor-add-fret').value) || 0));
    const sustain = Math.max(0, parseFloat(document.getElementById('editor-add-sustain').value) || 0);
    const note = {
        time: addNoteData.time,
        string: addNoteData.string,
        fret,
        sustain,
        techniques: {},
    };
    S.history.exec(new AddNoteCmd(note));
    hideAddNote();
    draw();
    updateStatus();
};

window.editorHideAddNote = hideAddNote;

// Handle Enter key in add-note dialog
document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && addNoteData) {
        e.preventDefault();
        editorConfirmAddNote();
    }
    if (e.key === 'Escape') {
        hideAddNote();
        hideContextMenu();
        editorHideLoadModal();
    }
});

// ════════════════════════════════════════════════════════════════════
// Audio / Playback
// ════════════════════════════════════════════════════════════════════

async function loadAudio(url) {
    if (!url) return;
    try {
        if (!S.audioCtx) S.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const resp = await fetch(url);
        const buf = await resp.arrayBuffer();
        S.audioBuffer = await S.audioCtx.decodeAudioData(buf);
        S.duration = S.audioBuffer.duration;
        computeWaveform();
    } catch (e) {
        console.error('Audio load error:', e);
    }
}

function computeWaveform() {
    if (!S.audioBuffer) return;
    const data = S.audioBuffer.getChannelData(0);
    const buckets = 4000;
    const peaks = new Float32Array(buckets);
    const samplesPerBucket = Math.floor(data.length / buckets);
    for (let b = 0; b < buckets; b++) {
        let max = 0;
        const start = b * samplesPerBucket;
        for (let s = 0; s < samplesPerBucket; s++) {
            const v = Math.abs(data[start + s]);
            if (v > max) max = v;
        }
        peaks[b] = max;
    }
    S.waveformPeaks = peaks;
}

function startPlayback() {
    if (!S.audioBuffer || !S.audioCtx) return;
    if (S.audioCtx.state === 'suspended') S.audioCtx.resume();
    S.audioSource = S.audioCtx.createBufferSource();
    S.audioSource.buffer = S.audioBuffer;
    S.audioSource.connect(S.audioCtx.destination);
    S.audioSource.start(0, S.cursorTime);
    S.playStartWall = S.audioCtx.currentTime;
    S.playStartTime = S.cursorTime;
    S.playing = true;
    updatePlayIcon();
    playbackTick();
}

function stopPlayback() {
    if (S.audioSource) {
        try { S.audioSource.stop(); } catch (_) {}
        S.audioSource = null;
    }
    S.playing = false;
    updatePlayIcon();
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
}

function playbackTick() {
    if (!S.playing) return;
    S.cursorTime = S.playStartTime + (S.audioCtx.currentTime - S.playStartWall);
    if (S.cursorTime >= S.duration) {
        stopPlayback();
        S.cursorTime = 0;
    }

    // Auto-scroll to follow cursor
    const cx = timeToX(S.cursorTime);
    const w = canvas ? canvas.width / DPR : 800;
    if (cx > w * 0.8) {
        S.scrollX = S.cursorTime - (w * 0.3) / S.zoom;
    }

    updateTimeDisplay();
    draw();
    rafId = requestAnimationFrame(playbackTick);
}

function updatePlayIcon() {
    const icon = document.getElementById('editor-play-icon');
    if (!icon) return;
    if (S.playing) {
        icon.innerHTML = '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>';
    } else {
        icon.innerHTML = '<path d="M8 5v14l11-7z"/>';
    }
}

function updateTimeDisplay() {
    const el = document.getElementById('editor-time-display');
    if (!el) return;
    const fmt = (t) => {
        const m = Math.floor(t / 60);
        const s = Math.floor(t % 60);
        return m + ':' + String(s).padStart(2, '0');
    };
    el.textContent = fmt(S.cursorTime) + ' / ' + fmt(S.duration);
}

// ════════════════════════════════════════════════════════════════════
// File operations
// ════════════════════════════════════════════════════════════════════

async function loadCDLC(filename) {
    setStatus('Loading ' + filename + '...');
    try {
        const resp = await fetch('/api/plugins/editor/load', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename }),
        });
        const data = await resp.json();
        if (data.error) { setStatus('Error: ' + data.error); return; }

        S.title = data.title || '';
        S.artist = data.artist || '';
        S.filename = filename;
        S.sessionId = data.session_id;
        S.arrangements = data.arrangements || [];
        S.beats = data.beats || [];
        S.sections = data.sections || [];
        S.duration = data.duration || 0;
        S.offset = data.offset || 0;
        S.currentArr = 0;
        S.sel.clear();
        S.scrollX = 0;
        S.cursorTime = 0;
        S.history = new EditHistory();

        // Flatten chord notes into main notes array for unified editing
        flattenChords();
        if (isKeysMode()) updatePianoRange();

        // Update UI
        document.getElementById('editor-song-title').textContent =
            `${S.artist} — ${S.title}`;
        S.createMode = false;
        document.getElementById('editor-save-btn').disabled = false;
        document.getElementById('editor-save-btn').classList.remove('hidden');
        document.getElementById('editor-build-btn').classList.add('hidden');
        document.getElementById('editor-play-btn').disabled = !data.audio_url;
        document.getElementById('editor-sync-btn').classList.toggle('hidden', !data.audio_url);
        updateArrangementSelector();
        updateStatus();
        updateTimeDisplay();
        updateBPMDisplay();

        // Load audio
        if (data.audio_url) {
            await loadAudio(data.audio_url);
        }

        draw();
        setStatus('Loaded: ' + S.artist + ' — ' + S.title);
    } catch (e) {
        setStatus('Load failed: ' + e.message);
    }
}

function updateArrangementSelector() {
    const sel = document.getElementById('editor-arrangement');
    sel.innerHTML = '';
    S.arrangements.forEach((arr, i) => {
        const opt = document.createElement('option');
        opt.value = i;
        opt.textContent = arr.name;
        sel.appendChild(opt);
    });
    sel.style.display = S.arrangements.length > 1 ? '' : 'none';
}

// ════════════════════════════════════════════════════════════════════
// Load modal
// ════════════════════════════════════════════════════════════════════

async function showLoadModal() {
    const modal = document.getElementById('editor-load-modal');
    modal.classList.remove('hidden');
    document.getElementById('editor-load-search').value = '';

    if (!S.songsList) {
        try {
            S.songsList = await fetch('/api/plugins/editor/songs').then(r => r.json());
        } catch {
            S.songsList = [];
        }
    }
    renderSongList(S.songsList);
    document.getElementById('editor-load-search').focus();
}

function renderSongList(files) {
    const list = document.getElementById('editor-load-list');
    if (!files.length) {
        list.innerHTML = '<div class="text-xs text-gray-500 p-2">No CDLC files found</div>';
        return;
    }
    list.innerHTML = files.map(f =>
        `<button onclick="editorLoadFile('${f.replace(/'/g, "\\'")}')" class="w-full text-left px-3 py-1.5 text-xs text-gray-300 hover:bg-dark-500 rounded truncate">${f}</button>`
    ).join('');
}

function filterSongs(q) {
    if (!S.songsList) return;
    const low = q.toLowerCase();
    const filtered = S.songsList.filter(f => f.toLowerCase().includes(low));
    renderSongList(filtered);
}

// ════════════════════════════════════════════════════════════════════
// Save
// ════════════════════════════════════════════════════════════════════

async function saveCDLC() {
    if (!S.sessionId) return;
    setStatus('Saving...');

    // Reconstruct chords from notes at the same time position
    reconstructChords();

    const arr = S.arrangements[S.currentArr];
    try {
        const resp = await fetch('/api/plugins/editor/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: S.sessionId,
                arrangement_index: S.currentArr,
                notes: arr.notes,
                chords: arr.chords,
                chord_templates: arr.chord_templates,
                beats: S.beats,
                sections: S.sections,
            }),
        });
        const data = await resp.json();
        if (data.error) { setStatus('Save error: ' + data.error); return; }
        setStatus('Saved successfully');
    } catch (e) {
        setStatus('Save failed: ' + e.message);
    } finally {
        // Re-flatten so editing continues with unified notes
        flattenChords();
        draw();
    }
}

// ════════════════════════════════════════════════════════════════════
// UI Helpers
// ════════════════════════════════════════════════════════════════════

function setStatus(msg) {
    const el = document.getElementById('editor-status');
    if (el) el.textContent = msg;
}

function updateStatus() {
    const nn = notes();
    const cc = chords();
    document.getElementById('editor-note-count').textContent =
        `${nn.length} notes, ${cc.length} chords` + (S.sel.size ? ` | ${S.sel.size} selected` : '');
    setStatus('Ready');
}

function updateZoomDisplay() {
    const el = document.getElementById('editor-zoom-display');
    if (el) el.textContent = Math.round(S.zoom);
}

function updateBPMDisplay() {
    const el = document.getElementById('editor-bpm');
    if (el && S.beats.length >= 2) el.value = getTabBPM().toFixed(1);
}

function resizeCanvas() {
    if (!canvas) return;
    const wrap = document.getElementById('editor-canvas-wrap');
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    if (w <= 0 || h <= 0) return;

    // Dynamically size lanes to fill available height
    const minBeat = 20, minWave = 50;
    BEAT_H = Math.max(minBeat, Math.floor(h * 0.05));
    WAVEFORM_H = Math.max(minWave, Math.floor(h * 0.12));
    LANE_H = Math.max(30, Math.floor((h - WAVEFORM_H - BEAT_H) / LANES));

    canvas.width = w * DPR;
    canvas.height = h * DPR;
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    draw();
}

// ════════════════════════════════════════════════════════════════════
// Global API (called from HTML)
// ════════════════════════════════════════════════════════════════════

window.editorShowLoadModal = showLoadModal;
window.editorHideLoadModal = () => document.getElementById('editor-load-modal').classList.add('hidden');
window.editorFilterSongs = filterSongs;
window.editorLoadFile = (f) => { editorHideLoadModal(); loadCDLC(f); };
window.editorSave = saveCDLC;
window.editorUndo = () => S.history && S.history.doUndo();
window.editorRedo = () => S.history && S.history.doRedo();
window.editorTogglePlay = () => {
    if (S.playing) stopPlayback(); else startPlayback();
};
window.editorZoom = (dir) => {
    const factor = dir > 0 ? 1.3 : 0.77;
    S.zoom = Math.max(20, Math.min(2000, S.zoom * factor));
    updateZoomDisplay();
    draw();
};
window.editorSetSnap = (idx) => { S.snapIdx = idx; };
window.editorSetBPM = (val) => {
    const newBPM = parseFloat(val);
    if (!newBPM || newBPM <= 0 || S.beats.length < 2) return;
    const oldBPM = getTabBPM();
    const factor = oldBPM / newBPM;
    if (Math.abs(factor - 1) < 0.001) return;

    // Scale all times
    const nn = notes();
    for (const n of nn) {
        n.time *= factor;
        if (n.sustain) n.sustain *= factor;
    }
    for (const b of S.beats) b.time *= factor;
    for (const s of S.sections) s.start_time *= factor;

    draw();
    setStatus(`Tempo changed: ${oldBPM.toFixed(1)} → ${newBPM.toFixed(1)} BPM`);
};
window.editorApplyOffset = (val) => {
    const offset = parseFloat(val) || 0;
    const currentOffset = parseFloat(document.getElementById('editor-offset').dataset.applied || '0');
    const delta = offset - currentOffset;
    if (Math.abs(delta) < 0.0001) return;
    const nn = notes();
    for (const n of nn) n.time += delta;
    for (const b of S.beats) b.time += delta;
    for (const s of S.sections) s.start_time += delta;
    document.getElementById('editor-offset').dataset.applied = String(offset);
    draw();
    setStatus(`Offset: ${offset >= 0 ? '+' : ''}${(offset * 1000).toFixed(0)}ms`);
};
window.editorNudgeOffset = (delta) => {
    const el = document.getElementById('editor-offset');
    const current = parseFloat(el.value) || 0;
    el.value = (current + delta).toFixed(3);
    editorApplyOffset(el.value);
};
window.editorSelectArrangement = (val) => {
    S.currentArr = parseInt(val) || 0;
    S.sel.clear();
    flattenChords();
    if (isKeysMode()) updatePianoRange();
    draw();
    updateStatus();
};
window.editorToggleTech = (idx, tech) => {
    const n = notes()[idx];
    if (!n.techniques) n.techniques = {};
    n.techniques[tech] = !n.techniques[tech];
    hideContextMenu();
    draw();
};

// Allow loading from other plugins/screens
window.editSong = (filename) => {
    showScreen('plugin-editor');
    loadCDLC(filename);
};

// ════════════════════════════════════════════════════════════════════
// Sync Tempo — detect audio BPM and scale notes to match
// ════════════════════════════════════════════════════════════════════

let syncState = { tabBPM: 0, audioBPM: 0 };

function detectAudioBPM() {
    if (!S.audioBuffer) return 0;
    const data = S.audioBuffer.getChannelData(0);
    const sr = S.audioBuffer.sampleRate;

    // Bandpass-approximate: use short + long energy windows for spectral flux
    const winSize = 1024;
    const hopSize = 512;
    const numFrames = Math.floor((data.length - winSize) / hopSize);
    const energy = new Float32Array(numFrames);
    for (let i = 0; i < numFrames; i++) {
        let sum = 0;
        const off = i * hopSize;
        for (let j = 0; j < winSize; j++) {
            sum += data[off + j] * data[off + j];
        }
        energy[i] = Math.sqrt(sum / winSize);
    }

    // Onset: spectral flux with adaptive threshold
    const onset = new Float32Array(numFrames);
    const avgWin = 16;
    for (let i = avgWin; i < numFrames; i++) {
        const diff = Math.max(0, energy[i] - energy[i - 1]);
        // Subtract local average to suppress sustained notes
        let localAvg = 0;
        for (let j = i - avgWin; j < i; j++) localAvg += Math.max(0, energy[j] - energy[j - 1]);
        localAvg /= avgWin;
        onset[i] = Math.max(0, diff - localAvg * 1.2);
    }

    // Autocorrelation for BPM range 60-220
    const frameDur = hopSize / sr;
    const minLag = Math.floor(60 / (220 * frameDur));
    const maxLag = Math.floor(60 / (60 * frameDur));
    const useLen = Math.min(onset.length, Math.floor(30 / frameDur));

    // Collect all peaks, not just the best
    const corrs = new Float32Array(maxLag + 1);
    for (let lag = minLag; lag <= Math.min(maxLag, useLen / 2); lag++) {
        let corr = 0;
        const n = useLen - lag;
        for (let i = 0; i < n; i++) corr += onset[i] * onset[i + lag];
        corrs[lag] = corr;
    }

    // Find top peaks in autocorrelation
    const peaks = [];
    for (let lag = minLag + 1; lag < maxLag; lag++) {
        if (corrs[lag] > corrs[lag - 1] && corrs[lag] > corrs[lag + 1] && corrs[lag] > 0) {
            peaks.push({ lag, corr: corrs[lag], bpm: 60 / (lag * frameDur) });
        }
    }
    peaks.sort((a, b) => b.corr - a.corr);

    if (!peaks.length) return 120;

    // Score each candidate: prefer strong correlation + BPM in 80-180 sweet spot
    // Also check if 2x or 0.5x of a candidate has strong correlation (harmonic check)
    let bestScore = -Infinity;
    let bestBPM = peaks[0].bpm;

    for (const p of peaks.slice(0, 10)) {
        let score = p.corr;

        // Boost BPMs in the 90-180 range (most common for music)
        if (p.bpm >= 90 && p.bpm <= 180) score *= 1.5;
        else if (p.bpm >= 70 && p.bpm <= 200) score *= 1.1;

        // Check if half-tempo has strong support (penalize sub-harmonics)
        const halfLag = Math.round(p.lag / 2);
        if (halfLag >= minLag && halfLag <= maxLag && corrs[halfLag] > p.corr * 0.6) {
            // Half-lag is also strong — this candidate might be a sub-harmonic
            score *= 0.7;
        }

        // Check if double-tempo also has support (confirms this is the real beat)
        const dblLag = p.lag * 2;
        if (dblLag <= maxLag && corrs[dblLag] > p.corr * 0.3) {
            score *= 1.3;
        }

        if (score > bestScore) {
            bestScore = score;
            bestBPM = p.bpm;
        }
    }

    return bestBPM;
}

function getTabBPM() {
    if (S.beats.length < 2) return 120;
    // Find average BPM from downbeats (measure > 0)
    const downbeats = S.beats.filter(b => b.measure > 0);
    if (downbeats.length < 2) {
        // Fallback: use all consecutive beats
        let total = 0;
        for (let i = 1; i < Math.min(S.beats.length, 50); i++) {
            total += S.beats[i].time - S.beats[i - 1].time;
        }
        const avgInterval = total / (Math.min(S.beats.length, 50) - 1);
        return 60 / avgInterval;
    }
    // Measure intervals between consecutive downbeats, divide by beats per measure
    let intervals = [];
    for (let i = 1; i < downbeats.length; i++) {
        const dt = downbeats[i].time - downbeats[i - 1].time;
        // Count beats between these downbeats
        const beatsInMeasure = S.beats.filter(
            b => b.time >= downbeats[i - 1].time && b.time < downbeats[i].time
        ).length;
        if (beatsInMeasure > 0) intervals.push(dt / beatsInMeasure);
    }
    if (!intervals.length) return 120;
    const avg = intervals.reduce((a, b) => a + b, 0) / intervals.length;
    return 60 / avg;
}

window.editorSyncTempo = () => {
    if (!S.audioBuffer || S.beats.length < 2) {
        setStatus('Need audio and beats loaded for sync');
        return;
    }

    setStatus('Detecting audio BPM...');
    syncState.tabBPM = getTabBPM();
    syncState.audioBPM = detectAudioBPM();

    document.getElementById('sync-tab-bpm').textContent = syncState.tabBPM.toFixed(1);
    document.getElementById('sync-audio-bpm').textContent = syncState.audioBPM.toFixed(1);
    document.getElementById('sync-manual-bpm').value = '';
    document.getElementById('sync-offset').value = '0';
    editorSyncUpdateFactor();

    const dlg = document.getElementById('editor-sync-dialog');
    const btn = document.getElementById('editor-sync-btn');
    const rect = btn.getBoundingClientRect();
    dlg.style.left = rect.left + 'px';
    dlg.style.top = (rect.bottom + 4) + 'px';
    dlg.classList.remove('hidden');
    setStatus('Ready');
};

window.editorSyncUpdateFactor = () => {
    const manual = parseFloat(document.getElementById('sync-manual-bpm').value);
    const audioBPM = manual > 0 ? manual : syncState.audioBPM;
    const factor = audioBPM / syncState.tabBPM;
    document.getElementById('sync-factor').textContent = factor.toFixed(4);
    if (manual > 0) {
        document.getElementById('sync-audio-bpm').textContent = manual.toFixed(1) + ' (manual)';
    } else {
        document.getElementById('sync-audio-bpm').textContent = syncState.audioBPM.toFixed(1);
    }
};

window.editorHideSyncDialog = () => {
    document.getElementById('editor-sync-dialog').classList.add('hidden');
};

window.editorApplySync = () => {
    const manual = parseFloat(document.getElementById('sync-manual-bpm').value);
    const audioBPM = manual > 0 ? manual : syncState.audioBPM;
    const factor = audioBPM / syncState.tabBPM;
    const offset = parseFloat(document.getElementById('sync-offset').value) || 0;

    if (factor <= 0 || !isFinite(factor)) return;

    // Scale all note times and sustains
    const nn = notes();
    for (const n of nn) {
        n.time = n.time / factor + offset;
        if (n.sustain) n.sustain = n.sustain / factor;
    }

    // Scale beat times
    for (const b of S.beats) {
        b.time = b.time / factor + offset;
    }

    // Scale section times
    for (const s of S.sections) {
        s.start_time = s.start_time / factor + offset;
    }

    editorHideSyncDialog();
    draw();
    setStatus(`Tempo synced: scaled ${factor.toFixed(4)}x` + (offset ? `, offset ${offset}s` : ''));
};

// ════════════════════════════════════════════════════════════════════
// Create mode
// ════════════════════════════════════════════════════════════════════

let createState = {
    gpPath: null,
    tracks: null,
    audioUrl: null,
    audioMode: 'file', // 'file' or 'youtube'
    artPath: null,
};

window.editorShowCreateModal = () => {
    createState = { gpPath: null, tracks: null, audioUrl: null, audioMode: 'file', artPath: null };
    document.getElementById('editor-create-modal').classList.remove('hidden');
    document.getElementById('editor-create-tracks').classList.add('hidden');
    document.getElementById('editor-create-go').disabled = true;
    document.getElementById('editor-create-status').textContent = '';
    document.getElementById('editor-audio-status').textContent = '';
    document.getElementById('editor-create-gp').value = '';
    document.getElementById('editor-create-audio').value = '';
    document.getElementById('editor-create-yt-url').value = '';
    document.getElementById('editor-create-title').value = '';
    document.getElementById('editor-create-artist').value = '';
    document.getElementById('editor-create-album').value = '';
    document.getElementById('editor-create-year').value = '';
    editorSetAudioMode('file');
};

window.editorHideCreateModal = () => {
    document.getElementById('editor-create-modal').classList.add('hidden');
};

window.editorSetAudioMode = (mode) => {
    createState.audioMode = mode;
    document.getElementById('editor-audio-file-input').classList.toggle('hidden', mode !== 'file');
    document.getElementById('editor-audio-yt-input').classList.toggle('hidden', mode !== 'youtube');
    document.getElementById('editor-audio-mode-file').className =
        'px-3 py-1 rounded text-xs ' + (mode === 'file' ? 'bg-accent' : 'bg-dark-600 hover:bg-dark-500');
    document.getElementById('editor-audio-mode-yt').className =
        'px-3 py-1 rounded text-xs ' + (mode === 'youtube' ? 'bg-accent' : 'bg-dark-600 hover:bg-dark-500');
};

window.editorGPFileSelected = async (input) => {
    if (!input.files.length) return;
    const file = input.files[0];
    const status = document.getElementById('editor-create-status');
    status.textContent = 'Uploading Guitar Pro file...';

    const form = new FormData();
    form.append('file', file);

    try {
        const resp = await fetch('/api/plugins/editor/import-gp', { method: 'POST', body: form });
        const data = await resp.json();
        if (data.error) { status.textContent = 'Error: ' + data.error; return; }

        createState.gpPath = data.gp_path;
        createState.tracks = data.tracks;

        // Show track list
        const listEl = document.getElementById('editor-create-track-list');
        listEl.innerHTML = data.tracks.map(t => {
            const badge = t.is_percussion ? ' (percussion)'
                : t.is_piano ? ' (keys)'
                : '';
            const disabled = t.is_percussion || t.notes === 0;
            return `<label class="flex items-center gap-2 text-xs text-gray-300 py-0.5">
                <input type="checkbox" value="${t.index}" checked
                    class="accent-accent" ${disabled ? 'disabled' : ''}>
                <span class="${t.is_percussion ? 'text-gray-600' : t.is_piano ? 'text-indigo-300' : ''}">${t.name}</span>
                <span class="text-gray-600">${t.strings}str, ${t.notes} notes${badge}</span>
            </label>`;
        }).join('');
        document.getElementById('editor-create-tracks').classList.remove('hidden');

        // Auto-fill title from filename
        const stem = file.name.replace(/\.(gp[345x]?|gpx)$/i, '');
        if (!document.getElementById('editor-create-title').value) {
            document.getElementById('editor-create-title').value = stem;
        }

        status.textContent = `Parsed: ${data.tracks.length} tracks found`;
        updateCreateButton();
    } catch (e) {
        status.textContent = 'Upload failed: ' + e.message;
    }
};

async function uploadCreateAudio() {
    const audioStatus = document.getElementById('editor-audio-status');

    if (createState.audioMode === 'youtube') {
        const url = document.getElementById('editor-create-yt-url').value.trim();
        if (!url) return false;
        audioStatus.textContent = 'Downloading from YouTube...';
        try {
            const resp = await fetch('/api/plugins/editor/youtube-audio', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url }),
            });
            const data = await resp.json();
            if (data.error) { audioStatus.textContent = 'Error: ' + data.error; return false; }
            createState.audioUrl = data.audio_url;
            audioStatus.textContent = 'Audio ready: ' + (data.title || 'downloaded');
            return true;
        } catch (e) {
            audioStatus.textContent = 'Download failed: ' + e.message;
            return false;
        }
    } else {
        const input = document.getElementById('editor-create-audio');
        if (!input.files.length) return false;
        audioStatus.textContent = 'Uploading audio...';
        const form = new FormData();
        form.append('file', input.files[0]);
        try {
            const resp = await fetch('/api/plugins/editor/upload-audio', { method: 'POST', body: form });
            const data = await resp.json();
            if (data.error) { audioStatus.textContent = 'Error: ' + data.error; return false; }
            createState.audioUrl = data.audio_url;
            audioStatus.textContent = 'Audio uploaded';
            return true;
        } catch (e) {
            audioStatus.textContent = 'Upload failed: ' + e.message;
            return false;
        }
    }
}

function updateCreateButton() {
    const hasGP = !!createState.gpPath;
    const hasAudio = createState.audioMode === 'youtube'
        ? !!document.getElementById('editor-create-yt-url').value.trim()
        : !!(document.getElementById('editor-create-audio').files || []).length;
    document.getElementById('editor-create-go').disabled = !hasGP;
}

// Wire up input change events for enabling the create button
document.addEventListener('change', (e) => {
    if (e.target.id === 'editor-create-audio') updateCreateButton();
});
document.addEventListener('input', (e) => {
    if (e.target.id === 'editor-create-yt-url') updateCreateButton();
});

window.editorDoCreate = async () => {
    if (!createState.gpPath) return;
    const status = document.getElementById('editor-create-status');
    const btn = document.getElementById('editor-create-go');
    btn.disabled = true;

    // Upload/download audio first
    const hasAudioInput = createState.audioMode === 'youtube'
        ? !!document.getElementById('editor-create-yt-url').value.trim()
        : !!(document.getElementById('editor-create-audio').files || []).length;

    if (hasAudioInput && !createState.audioUrl) {
        const ok = await uploadCreateAudio();
        if (!ok) { btn.disabled = false; return; }
    }

    // Get selected track indices
    const checkboxes = document.querySelectorAll('#editor-create-track-list input[type=checkbox]:checked:not(:disabled)');
    const trackIndices = [...checkboxes].map(cb => parseInt(cb.value));

    status.textContent = 'Converting Guitar Pro to Rocksmith...';

    try {
        const resp = await fetch('/api/plugins/editor/convert-gp', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                gp_path: createState.gpPath,
                audio_url: createState.audioUrl || '',
                track_indices: trackIndices.length ? trackIndices : null,
                title: document.getElementById('editor-create-title').value || 'Untitled',
                artist: document.getElementById('editor-create-artist').value || 'Unknown',
                album: document.getElementById('editor-create-album').value || '',
                year: document.getElementById('editor-create-year').value || '',
            }),
        });
        const data = await resp.json();
        if (data.error) { status.textContent = 'Error: ' + data.error; btn.disabled = false; return; }

        // Load into editor
        editorHideCreateModal();
        S.title = data.title || '';
        S.artist = data.artist || '';
        S.filename = '';
        S.sessionId = data.session_id;
        S.arrangements = data.arrangements || [];
        S.beats = data.beats || [];
        S.sections = data.sections || [];
        S.duration = data.duration || 0;
        S.offset = data.offset || 0;
        S.currentArr = 0;
        S.sel.clear();
        S.scrollX = 0;
        S.cursorTime = 0;
        S.history = new EditHistory();
        S.createMode = true;

        flattenChords();
        if (isKeysMode()) updatePianoRange();

        document.getElementById('editor-song-title').textContent =
            `${S.artist} — ${S.title} (new)`;
        document.getElementById('editor-save-btn').classList.add('hidden');
        document.getElementById('editor-build-btn').classList.remove('hidden');
        document.getElementById('editor-play-btn').disabled = !data.audio_url;
        document.getElementById('editor-sync-btn').classList.toggle('hidden', !data.audio_url);
        updateArrangementSelector();
        updateStatus();
        updateTimeDisplay();
        updateBPMDisplay();

        if (data.audio_url) await loadAudio(data.audio_url);
        draw();
        setStatus('Imported — edit notes then click Build CDLC');
    } catch (e) {
        status.textContent = 'Import failed: ' + e.message;
        btn.disabled = false;
    }
};

window.editorBuild = async () => {
    if (!S.sessionId || !S.createMode) return;
    setStatus('Building CDLC...');

    // Reconstruct chords for ALL arrangements before sending
    const savedArr = S.currentArr;
    const allArrangements = [];
    for (let i = 0; i < S.arrangements.length; i++) {
        S.currentArr = i;
        reconstructChords();
        const arr = S.arrangements[i];
        allArrangements.push({
            name: arr.name,
            notes: arr.notes,
            chords: arr.chords,
            chord_templates: arr.chord_templates,
        });
    }
    S.currentArr = savedArr;

    // Upload album art if selected
    const artInput = document.getElementById('editor-create-art');
    if (artInput && artInput.files && artInput.files.length && !createState.artPath) {
        const form = new FormData();
        form.append('file', artInput.files[0]);
        try {
            const r = await fetch('/api/plugins/editor/upload-art', { method: 'POST', body: form });
            const d = await r.json();
            if (d.art_path) createState.artPath = d.art_path;
        } catch (_) {}
    }

    try {
        const resp = await fetch('/api/plugins/editor/build', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: S.sessionId,
                arrangements: allArrangements,
                beats: S.beats,
                sections: S.sections,
                audio_url: createState.audioUrl || '',
                art_path: createState.artPath || '',
                metadata: {
                    title: S.title,
                    artist: S.artist,
                    artistName: S.artist,
                },
            }),
        });
        const data = await resp.json();
        if (data.error) { setStatus('Build error: ' + data.error); return; }
        setStatus('CDLC built: ' + data.path);
    } catch (e) {
        setStatus('Build failed: ' + e.message);
    } finally {
        // Re-flatten current arrangement for continued editing
        flattenChords();
        draw();
    }
};

// ════════════════════════════════════════════════════════════════════
// Init
// ════════════════════════════════════════════════════════════════════

function init() {
    canvas = document.getElementById('editor-canvas');
    if (!canvas) return;
    ctx = canvas.getContext('2d');
    S.history = new EditHistory();

    canvas.addEventListener('mousedown', onMouseDown);
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    canvas.addEventListener('dblclick', onDblClick);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.addEventListener('contextmenu', onContextMenu);
    document.addEventListener('keydown', onKeyDown);

    // Prevent middle-click paste
    canvas.addEventListener('auxclick', (e) => { if (e.button === 1) e.preventDefault(); });

    resizeCanvas();
    window.addEventListener('resize', resizeCanvas);

    // Observe screen visibility for resize
    const obs = new MutationObserver(() => {
        const screen = document.getElementById('plugin-editor');
        if (screen && screen.classList.contains('active')) {
            setTimeout(resizeCanvas, 50);
        }
    });
    const screen = document.getElementById('plugin-editor');
    if (screen) obs.observe(screen, { attributes: true, attributeFilter: ['class'] });

    draw();
}

// Run init after DOM is ready
if (document.getElementById('editor-canvas')) {
    init();
} else {
    // Wait for plugin screen to be injected
    const check = setInterval(() => {
        if (document.getElementById('editor-canvas')) {
            clearInterval(check);
            init();
        }
    }, 100);
}

})();
