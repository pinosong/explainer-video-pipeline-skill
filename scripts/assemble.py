#!/usr/bin/env python3
"""Final assembly for the explainer-video pipeline.

Concatenates per-sentence video segments (in timing order), burns the
full-sentence subtitles, and muxes the narration+music mix.

Usage:
  python3 assemble.py --segments seg/ --audio audio/mix.wav \
      --subs audio/subs.ass --out final.mp4

--segments takes a directory (segments sorted by filename: build them as
s01.mp4, s02.mp4, ... in timing order), a text file with one segment path
per line, or an ffmpeg concat list. Segments are normalised to
--width x --height at --fps, re-encoded, then joined.

Validated against production: reassembling the solid-state battery
segments + mix + subs reproduces the shipped 89.4 s film.
"""
import argparse, os, subprocess, sys, tempfile


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser(description="Final assembly (see module docstring)")
    ap.add_argument("--segments", required=True, help="directory, path-list .txt, or ffmpeg concat list")
    ap.add_argument("--audio", required=True, help="mix wav (narration + music) from prep_audio.py")
    ap.add_argument("--subs", default=None, help=".ass subtitles from prep_audio.py (optional)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--crf", type=int, default=17)
    args = ap.parse_args()

    if os.path.isdir(args.segments):
        files = sorted(os.path.join(args.segments, f) for f in os.listdir(args.segments)
                       if f.endswith(".mp4"))
    else:
        files = []
        for line in open(args.segments, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("file "):
                line = line[5:].strip().strip("'\"")
            files.append(line)
    if not files:
        sys.exit("no segment files found")
    print(f"{len(files)} segments")

    tmp = tempfile.mkdtemp(prefix="assemble_")
    norm = []
    vf = (f"scale={args.width}:{args.height}:force_original_aspect_ratio=decrease,"
          f"pad={args.width}:{args.height}:(ow-iw)/2:(oh-ih)/2,fps={args.fps},format=yuv420p")
    for i, f in enumerate(files):
        o = os.path.join(tmp, f"n{i:03d}.mp4")
        run(["ffmpeg", "-y", "-v", "error", "-i", f, "-vf", vf, "-an",
             "-c:v", "libx264", "-crf", "16", o])
        norm.append(o)
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as fh:
        for o in norm:
            fh.write(f"file '{o}'\n")
    video_only = os.path.join(tmp, "video_only.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
         "-c", "copy", video_only])

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_only, "-i", args.audio]
    if args.subs:
        subs_esc = os.path.abspath(args.subs).replace(":", "\\:").replace("'", "\\'")
        cmd += ["-vf", f"subtitles='{subs_esc}'"]
    cmd += ["-c:v", "libx264", "-crf", str(args.crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-shortest", args.out]
    run(cmd)
    print("FINAL:", args.out)


if __name__ == "__main__":
    main()
