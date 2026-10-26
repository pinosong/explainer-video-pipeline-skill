#!/usr/bin/env python3
"""Narration post-production for the explainer-video pipeline.

Takes the one-shot TTS wav plus the sentence boundaries from
slice_sentences.py, and produces everything the edit needs:

  narration_final.wav  pauses between sentences compressed to --keep seconds
  timing.json          per-sentence onsets on the FINAL track (the edit table)
  subs.ass             full-sentence subtitles timed by timing.json
  mix.wav              narration + licensed music bed (only with --music)

Usage:
  python3 prep_audio.py --raw narration_raw.wav \
      --slice slice_output.json --sentences sentences.json \
      --music music.mp3 --outdir audio/

sentences.json: [{"id": "s01", "text": "..."}, ...] (ids in narration order).
--slice is the JSON written by slice_sentences.py (uses its onsets, which are
on the RAW track). Alternatively --boundaries '[4.76, 9.63, ...]' with the
raw-track sentence-end times.

Pause intervals are re-detected on the waveform (10 ms envelope) around each
boundary; a boundary whose pause cannot be found is reported, not guessed.
Validated against production: reproduces the solid-state battery timing
table (88.47 s track) from the raw 95.24 s narration.
"""
import argparse, json, math, os, subprocess, sys, wave

import contextlib


def envelope(path, hop_s=0.01):
    with contextlib.closing(wave.open(path, "rb")) as w:
        sr, n, sw = w.getframerate(), w.getnframes(), w.getsampwidth()
        ch = w.getnchannels()
        raw = w.readframes(n)
    if sw != 2:
        raise SystemExit("expected 16-bit PCM wav")
    import array
    a = array.array("h"); a.frombytes(raw)
    if ch > 1:
        a = a[::ch]
    hop = max(1, int(sr * hop_s))
    env = []
    for i in range(0, len(a), hop):
        seg = a[i:i + hop]
        env.append(math.sqrt(sum(x * x for x in seg) / len(seg)) / 32768.0)
    return env, hop_s


def find_pause(env, hop_s, boundary, thr=0.012, win=2.0):
    """Low-energy run whose END is closest to boundary (the pause end)."""
    lo = max(0, int((boundary - win) / hop_s))
    hi = min(len(env), int((boundary + 0.3) / hop_s))
    runs, start = [], None
    for i in range(lo, hi):
        if env[i] < thr and start is None:
            start = i
        elif env[i] >= thr and start is not None:
            runs.append((start * hop_s, i * hop_s)); start = None
    if start is not None:
        runs.append((start * hop_s, hi * hop_s))
    runs = [r for r in runs if r[1] - r[0] >= 0.25]
    if not runs:
        return None
    return min(runs, key=lambda r: abs(r[1] - boundary))


def wrap(text, width=23):
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= width and ch in "，。；：、——":
            lines.append(cur); cur = ""
    if cur:
        lines.append(cur)
    return lines


def ts(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK SC,46,&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,3,10,0,2,140,140,44,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def main():
    ap = argparse.ArgumentParser(description="Narration post-production (see module docstring)")
    ap.add_argument("--raw", required=True)
    ap.add_argument("--slice", default=None, help="slice_sentences.py output JSON")
    ap.add_argument("--boundaries", default=None, help="JSON list of raw-track sentence-end times")
    ap.add_argument("--sentences", required=True)
    ap.add_argument("--music", default=None)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--keep", type=float, default=0.45, help="pause length kept between sentences (s)")
    ap.add_argument("--tail", type=float, default=0.93, help="music tail after narration ends (s)")
    ap.add_argument("--music-volume", type=float, default=0.10)
    args = ap.parse_args()

    sentences = json.load(open(args.sentences, encoding="utf-8"))
    ids = [s["id"] for s in sentences]
    texts = {s["id"]: s["text"] for s in sentences}
    if args.slice:
        sl = json.load(open(args.slice, encoding="utf-8"))
        boundaries = [round(s["onset"], 2) for s in sl["sentences"]][1:]
    elif args.boundaries:
        boundaries = json.loads(args.boundaries)
    else:
        ap.error("give --slice or --boundaries")
    assert len(boundaries) == len(ids) - 1, f"{len(boundaries)} boundaries for {len(ids)} sentences"

    env, hop_s = envelope(args.raw)
    total_src = len(env) * hop_s
    pauses, missing = [], []
    for b in boundaries:
        p = find_pause(env, hop_s, b)
        if p is None:
            missing.append(b); p = (b - args.keep, b)
        pauses.append(p)
    if missing:
        print(f"WARNING: no pause found near {missing} - timing there needs a manual check", file=sys.stderr)

    os.makedirs(args.outdir, exist_ok=True)
    # 1) pause-compressed narration
    parts, prev = [], 0.0
    for ps, pe in pauses:
        parts.append((prev, ps + args.keep)); prev = pe
    parts.append((prev, total_src))
    fc = [f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[p{i}]" for i, (a, b) in enumerate(parts)]
    fc.append("".join(f"[p{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[out]")
    final_wav = os.path.join(args.outdir, "narration_final.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", args.raw, "-filter_complex",
                    ";".join(fc), "-map", "[out]", final_wav], check=True)

    # 2) timing table on the final track
    orig_onsets = [0.0] + [pe for _, pe in pauses]
    cuts = [pe - (ps + args.keep) for ps, pe in pauses]
    onsets, cum = [], 0.0
    for i, o in enumerate(orig_onsets):
        if i > 0:
            cum += cuts[i - 1]
        onsets.append(round(o - cum, 3))
    track_dur = round(total_src - sum(cuts), 2)
    total = round(track_dur + args.tail, 2)
    table = {"onsets": dict(zip(ids, onsets)), "track_dur": track_dur, "total": total}
    with open(os.path.join(args.outdir, "timing.json"), "w") as f:
        json.dump(table, f, indent=1)
    print("onsets:", table["onsets"]); print("track:", track_dur, "total:", total)

    # 3) subtitles
    lines = [ASS_HEAD]
    for i, sid in enumerate(ids):
        start = onsets[i]
        end = onsets[i + 1] if i + 1 < len(ids) else track_dur - 0.15
        text = "\\N".join(wrap(texts[sid]))
        lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{text}")
    subs_path = os.path.join(args.outdir, "subs.ass")
    open(subs_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("subs written:", subs_path)

    # 4) music mix
    if args.music:
        fc = (f"[0:a]volume=1.0[nar];"
              f"[1:a]atrim=0:{total},volume={args.music_volume},afade=t=in:st=0:d=1.5,"
              f"afade=t=out:st={total - 4.5}:d=4.5[mus];"
              f"[nar][mus]amix=inputs=2:duration=first:normalize=0[out]")
        mix_path = os.path.join(args.outdir, "mix.wav")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", final_wav, "-i", args.music,
                        "-filter_complex", fc, "-map", "[out]", mix_path], check=True)
        print("mix written:", mix_path)


if __name__ == "__main__":
    main()
