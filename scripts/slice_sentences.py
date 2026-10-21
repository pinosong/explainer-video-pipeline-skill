#!/usr/bin/env python3
"""Slice a one-shot TTS narration into sentences.

Usage:
  python3 slice_sentences.py <narration.wav> <sentences.txt> [out.json]

sentences.txt: one sentence per line (the approved script).
Method (two-pass):
  1. Find candidate pauses: 10ms envelope < 0.012, >= 0.12s, fragments
     merged across < 0.45s of sound.
  2. Pass 1: predict each sentence onset from the cumulative character
     share of the script; pick the nearest candidate pause end.
  3. Pass 2 (x2): estimate total pause time from the pass-1 picks,
     re-predict onsets in speech-time coordinates (character share of the
     speech-only duration, plus the pauses before each onset), re-pick.
Remaining candidates are reported as clause pauses (edit cut points).
Warnings flag boundaries whose final residual is > 0.8s, and boundaries
with a rival candidate within 1.5s that is itself sentence-length
(>= 0.65s — a short rival is just a clause pause, not a plausible
boundary). Honest flagging beats false precision: check the warnings.
"""
import json, subprocess, sys

import numpy as np

wav, sfile = sys.argv[1], sys.argv[2]
out = sys.argv[3] if len(sys.argv) > 3 else 'timing.json'
sentences = [l.strip() for l in open(sfile, encoding='utf8') if l.strip()]

pcm = subprocess.run(['ffmpeg', '-v', 'error', '-i', wav, '-f', 's16le',
                      '-ar', '24000', '-ac', '1', '-'], capture_output=True).stdout
a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
win = 240
n = len(a) // win
env = np.abs(a[:n * win].reshape(n, win)).max(axis=1)
THR = 0.012
raw, start = [], None
for i, v in enumerate(env):
    if v < THR and start is None:
        start = i
    elif v >= THR and start is not None:
        if (i - start) / 100 >= 0.12:
            raw.append([start / 100, i / 100])
        start = None
cands = []
for p in raw:
    if cands and p[0] - cands[-1][1] < 0.45:
        cands[-1][1] = p[1]
    else:
        cands.append(list(p))

dur = round(len(a) / 24000, 2)
total_chars = sum(len(s) for s in sentences)


def pick(preds):
    """Pick the nearest unused candidate pause end for each prediction."""
    used, chosen = set(), []
    for pred in preds:
        best = min((c for k, c in enumerate(cands) if k not in used),
                   key=lambda c: abs(c[1] - pred), default=None)
        if best is not None:
            used.add(cands.index(best))
        chosen.append(best)
    return chosen, used


def raw_preds():
    cum, preds = 0, []
    for s in sentences[:-1]:
        cum += len(s)
        preds.append(dur * cum / total_chars)
    return preds


def speech_preds(chosen):
    pause_total = sum((c[1] - c[0]) for c in chosen if c)
    speech = dur - pause_total
    cum, preds, pi = 0, [], 0
    for s in sentences[:-1]:
        cum += len(s)
        p_before = sum((c[1] - c[0]) for c in chosen[:pi] if c)
        preds.append(speech * cum / total_chars + p_before)
        pi += 1
    return preds


# pass 1: raw char proportion; pass 2 (x2): speech-time coordinates
preds = raw_preds()
chosen, used = pick(preds)
for _ in range(2):
    preds = speech_preds(chosen)
    chosen, used = pick(preds)

bounds = []
for i, (pred, c) in enumerate(zip(preds, chosen)):
    residual = round(abs(c[1] - pred), 2) if c else None
    rival = None
    if c:
        rivals = [o for k, o in enumerate(cands)
                  if k not in used and abs(o[1] - c[1]) <= 1.5
                  and o[1] - o[0] >= 0.65]
        rival = rivals[0] if rivals else None
    bounds.append({'pred': round(pred, 2), 'pause': c,
                   'residual': residual, 'rival': rival})

onsets = [0.0] + [b['pause'][1] if b['pause'] else b['pred'] for b in bounds]
ends = [b['pause'][0] if b['pause'] else None for b in bounds] + [dur]

warnings = []
for i, b in enumerate(bounds):
    if b['pause'] is None:
        warnings.append(f"sentence {i + 2} onset: no candidate pause found — verify manually")
    elif b['residual'] > 0.8:
        warnings.append(f"sentence {i + 2} onset residual {b['residual']}s — verify manually")
    elif b['rival']:
        warnings.append(f"sentence {i + 2} onset ambiguous: rival pause ending "
                        f"{b['rival'][1]:.2f}s near chosen {b['pause'][1]:.2f}s — verify manually")

table = {'duration': dur,
         'sentences': [{'index': i + 1, 'text': sentences[i], 'onset': round(onsets[i], 2),
                        'end': round(ends[i], 2) if ends[i] else None} for i in range(len(sentences))],
         'clause_pauses': [c for k, c in enumerate(cands) if k not in used],
         'warnings': warnings}
with open(out, 'w') as f:
    json.dump(table, f, indent=1, ensure_ascii=False)
print(f"{len(sentences)} sentences -> {out}; warnings: {warnings or 'none'}")
