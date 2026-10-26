#!/usr/bin/env python3
"""One-shot Gemini TTS narration for the explainer-video pipeline.

Calls the Gemini Interactions API (gemini-3.8-flash-tts) ONCE for the whole
script: the script file is split into acts by blank lines, each act gets a
style instruction, all acts share one voice. One call matters: free-tier
quota is counted per generation.

This is the exact request shape used in production (solid-state battery
video, 2026-10-21). Stdlib only, no SDK needed.

Usage:
  GOOGLE_TTS_API_KEY=... python3 tts_gemini.py \
      --script script.txt --voice Charon \
      --style "confident tech explainer, brisk pace, clear emphasis on numbers" \
      --out narration_raw.wav

  # per-act styles: one style per line in a styles file (must match act count)
  ... --styles styles.txt

  # validate the payload without calling the API (burns no quota):
  ... --dry-run

Errors: 400 = payload schema (fix the request, not the key);
401/403 = key problem or wrong key source (stop, check the key);
429 = quota exhausted (stop, wait for reset). Never blind-retry: every
retry counts against the quota.
"""
import argparse, base64, json, os, sys, urllib.request, urllib.error

API = "https://generativelanguage.googleapis.com/v1beta/interactions"
MODEL = "gemini-3.8-flash-tts"


def main():
    ap = argparse.ArgumentParser(description="One-shot Gemini TTS (see module docstring)")
    ap.add_argument("--script", required=True, help="plain-text script; acts separated by blank lines")
    ap.add_argument("--voice", required=True, help="voice name, e.g. Charon (check GET /v1beta/voices for zh voices)")
    ap.add_argument("--style", default=None, help="one style instruction applied to every act")
    ap.add_argument("--styles", default=None, help="file with one style per line, matching the act count")
    ap.add_argument("--out", default="narration_raw.wav")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--dry-run", action="store_true", help="print the payload JSON and exit (no API call)")
    args = ap.parse_args()

    acts = [a.strip() for a in open(args.script, encoding="utf-8").read().split("\n\n") if a.strip()]
    if args.styles:
        styles = [s.strip() for s in open(args.styles, encoding="utf-8") if s.strip()]
    elif args.style:
        styles = [args.style] * len(acts)
    else:
        ap.error("give --style or --styles")
    assert len(acts) == len(styles), f"acts={len(acts)} styles={len(styles)}: counts must match"

    content = []
    for text, style in zip(acts, styles):
        if not text.endswith("<short pause>"):
            text += " <short pause>"
        content.append({"type": "text", "text": text,
                        "annotations": [{"type": "speech_metadata", "style": style}]})
    payload = {"model": args.model,
               "input": [{"type": "user_input", "content": content}],
               "response_format": {"type": "audio"},
               "generation_config": {"speech_config": [{"voice": args.voice}]}}
    json.dumps(payload, ensure_ascii=False)  # offline validation before any call

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        print(f"# dry run: {len(acts)} acts, no API call made", file=sys.stderr)
        return

    key = os.environ.get("GOOGLE_TTS_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("set GOOGLE_TTS_API_KEY (or GEMINI_API_KEY) in the environment; never hardcode it")
    req = urllib.request.Request(API, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:2000]
        hint = {400: "payload schema error - fix the request",
                401: "key rejected - check the key source (AI Studio)",
                403: "key/region problem - check the key source and region",
                429: "quota exhausted - stop and wait for reset"}.get(e.code, "")
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
        sys.exit(1)

    audio_b64 = None
    for step in resp.get("steps", []):
        for c in step.get("content", []):
            if c.get("type") == "audio" and c.get("data"):
                audio_b64 = c["data"]
    if not audio_b64:
        sys.exit("no audio in response - inspect the raw response before retrying (quota!)")
    open(args.out, "wb").write(base64.b64decode(audio_b64))
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()
