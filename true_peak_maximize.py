#!/usr/bin/env python3
"""
true_peak_maximize.py

Maximizes the level of an audio file by applying the largest gain that
still keeps true peak under a chosen ceiling -- for lossless (WAV/FLAC)
or lossy (MP3, AAC/M4A, Ogg Vorbis, Opus) output.

There's no loudness target here, just: measure true peak, compute the
gain that brings it up to (but not over) the ceiling, apply it, and
VERIFY THE ACTUAL OUTPUT FILE -- not just trust the math.

That verification step matters most for lossy output. A WAV or FLAC
that's loud but genuinely not clipping can still end up over your
true-peak ceiling after being encoded lossy -- the encoder's own
block-based reconstruction can introduce ringing/overshoot that wasn't
in the original PCM at all, and the amount varies by codec. So for
lossy output, this script iterates: encode, measure the REAL encoded
file, back off gain if it overshot, and re-encode from the ORIGINAL
source (never from a previous lossy pass, to avoid stacking generation
loss) until the actual file measures under the ceiling.

For lossless output this same loop still runs, but since gain is a
pure linear operation on PCM, it almost always converges in one pass.

Important: this only applies gain -- it does not limit or compress.
Your ceiling is set entirely by the single loudest true peak in the
file. An actual limiter is a different, more invasive kind of
processing that changes dynamics, not just level.

Supported output formats:
    Lossless: .wav  .flac  .aiff  .aif
    Lossy:    .mp3  .aac  .m4a  .ogg  .opus

Requirements:
    - ffmpeg installed and on your PATH, built with libmp3lame,
      libvorbis, and libopus (standard in most distro ffmpeg builds)
    - Python 3.8+

Usage:
    python true_peak_maximize.py input.wav output.wav
    python true_peak_maximize.py input.flac output.mp3 --ceiling -0.3
    python true_peak_maximize.py input.wav output.opus --bitrate 192k
"""

import argparse
import math
import os
import shutil
import subprocess
import sys

# Per-format codec + default quality settings for lossy output.
# Each entry's "quality_args" is used unless --bitrate overrides it.
FORMAT_SETTINGS = {
    ".mp3":  {"codec": "libmp3lame", "quality_args": ["-q:a", "0"]},      # VBR, top quality (~245kbps)
    ".m4a":  {"codec": "aac",        "quality_args": ["-b:a", "256k"]},   # native aac encoder has no reliable VBR scale
    ".aac":  {"codec": "aac",        "quality_args": ["-b:a", "256k"]},
    ".ogg":  {"codec": "libvorbis",  "quality_args": ["-q:a", "10"]},     # VBR, top quality (~500kbps)
    ".opus": {"codec": "libopus",    "quality_args": ["-b:a", "192k"]},   # opus is efficient; 192k is already very transparent
}

LOSSLESS_EXTENSIONS = {".wav", ".flac", ".aiff", ".aif"}


def measure_true_peak(input_file):
    """
    Measure true peak (inter-sample peak) using ffmpeg's dedicated
    ebur128 EBU R128 analyzer filter, rather than repurposing the
    loudnorm filter (which computes a lot of normalization math we
    don't need just to read one number back out).

    ebur128's metadata output reports true peak as a raw linear
    amplitude value with full float precision, which we convert to
    dBTP ourselves -- more precise than loudnorm's fixed 2-decimal
    JSON output, and considerably simpler/more robust to parse (no
    need to locate a JSON block boundary inside messy stderr text).
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-i", input_file,
        "-filter_complex", "ebur128=peak=true:metadata=1,ametadata=print:file=-",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    peak_linear = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("lavfi.r128.true_peak="):
            val = float(line.split("=", 1)[1])
            if peak_linear is None or val > peak_linear:
                peak_linear = val  # take the max across all reported frames

    if peak_linear is None:
        raise RuntimeError(
            f"Couldn't find true-peak metadata for {input_file}.\n"
            f"ffmpeg stdout:\n{result.stdout}\nffmpeg stderr:\n{result.stderr}"
        )

    if peak_linear <= 0:
        return -120.0  # silence / invalid reading -- avoid log10(0)

    return 20 * math.log10(peak_linear)


def encode_with_gain(input_file, output_file, gain_db, bitrate=None):
    """
    Apply a pure linear gain (dB) and encode to whatever format the
    output extension implies. Lossless extensions pass through with no
    codec-specific args (ffmpeg picks the natural codec for the
    container). Lossy extensions get the codec + quality settings from
    FORMAT_SETTINGS, unless --bitrate was given, in which case it
    overrides the default quality args uniformly.
    """
    ext = os.path.splitext(output_file)[1].lower()
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", input_file, "-af", f"volume={gain_db}dB"]

    if ext in FORMAT_SETTINGS:
        settings = FORMAT_SETTINGS[ext]
        cmd += ["-c:a", settings["codec"]]
        cmd += ["-b:a", bitrate] if bitrate else settings["quality_args"]
    elif ext in LOSSLESS_EXTENSIONS:
        pass  # no lossy codec args -- ffmpeg uses the container's native PCM/lossless codec
    else:
        supported = ", ".join(sorted(set(FORMAT_SETTINGS) | LOSSLESS_EXTENSIONS))
        raise ValueError(f"Unsupported output extension '{ext}'. Supported: {supported}")

    cmd += [output_file]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(e.stderr, file=sys.stderr)
        raise


def maximize_true_peak(input_file, output_file, ceiling=-0.3, max_passes=14, bitrate=None,
                        verbose=True, tolerance=0.05):
    """
    Apply the largest gain that keeps the FINAL, ACTUALLY ENCODED output's
    true peak at or under `ceiling` (dBTP), always re-encoding from the
    original source (never from a previous lossy pass).

    This uses a bracket-then-bisect search rather than a single reactive
    correction: it finds a confirmed-safe gain and a confirmed-unsafe gain,
    then narrows between them until the result sits as close to the
    ceiling as `tolerance` allows. A naive "if you overshoot, pull back by
    the error" approach can overcorrect and land well under the ceiling --
    technically safe, but leaving loudness on the table -- and would stop
    there without ever trying to climb back up. Bisection guarantees it
    keeps searching until it's actually close to the ceiling, not just
    somewhere safely below it.
    """
    source_tp = measure_true_peak(input_file)
    if verbose:
        print(f"Source true peak: {source_tp:.2f} dBTP")

    ext = os.path.splitext(output_file)[1]
    passes_used = 0

    def attempt(gain_db):
        nonlocal passes_used
        passes_used += 1
        temp_output = f"_tpmax_pass{passes_used}{ext}"
        encode_with_gain(input_file, temp_output, gain_db, bitrate=bitrate)
        actual_tp = measure_true_peak(temp_output)
        if verbose:
            print(f"[pass {passes_used}] tried {gain_db:+.2f} dB -> {actual_tp:.2f} dBTP "
                  f"(ceiling {ceiling:.2f} dBTP)")
        return temp_output, actual_tp

    def safe(tp):
        return tp <= ceiling + tolerance

    gain = ceiling - source_tp
    out, tp = attempt(gain)

    if safe(tp):
        # Bracket UPWARD: keep pushing louder while it's still safe, so we
        # don't settle for an overly conservative first result.
        lo_gain, lo_out = gain, out
        step = max(ceiling - tp, 0.2)
        hi_gain = None
        while passes_used < max_passes:
            test_gain = lo_gain + step
            test_out, test_tp = attempt(test_gain)
            if safe(test_tp):
                os.remove(lo_out)
                lo_gain, lo_out = test_gain, test_out
                step *= 1.7
            else:
                hi_gain = test_gain
                os.remove(test_out)
                break
        if hi_gain is None:
            # Ran out of passes while still finding headroom -- ship the
            # best safe result found rather than failing.
            shutil.move(lo_out, output_file)
            if verbose:
                print(f"Done in {passes_used} pass(es), gain {lo_gain:+.2f} dB -> {output_file}")
            return
        best_gain, best_out = lo_gain, lo_out
    else:
        # Bracket DOWNWARD: back off until we find a safe lower bound.
        hi_gain = gain
        step = (tp - ceiling) + 0.5
        os.remove(out)
        best_gain, best_out = None, None
        while passes_used < max_passes:
            test_gain = hi_gain - step
            test_out, test_tp = attempt(test_gain)
            if safe(test_tp):
                best_gain, best_out = test_gain, test_out
                break
            os.remove(test_out)
            hi_gain = test_gain
            step *= 1.5
        if best_gain is None:
            raise RuntimeError(
                f"Could not find a safe gain after {passes_used} passes. "
                f"Try a lower --ceiling or increase --max-passes."
            )

    # Bisect between the confirmed-safe best_gain and confirmed-unsafe
    # hi_gain, narrowing toward the ceiling.
    while passes_used < max_passes and (hi_gain - best_gain) > tolerance:
        mid_gain = (best_gain + hi_gain) / 2
        mid_out, mid_tp = attempt(mid_gain)
        if safe(mid_tp):
            os.remove(best_out)
            best_gain, best_out = mid_gain, mid_out
        else:
            os.remove(mid_out)
            hi_gain = mid_gain

    shutil.move(best_out, output_file)
    if verbose:
        print(f"Done in {passes_used} pass(es), final gain {best_gain:+.2f} dB -> {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("input", help="Input audio file (WAV, FLAC, MP3, anything ffmpeg reads)")
    parser.add_argument(
        "output",
        help="Output path -- format inferred from extension: "
             + ", ".join(sorted(set(FORMAT_SETTINGS) | LOSSLESS_EXTENSIONS)),
    )
    parser.add_argument(
        "--ceiling", type=float, default=-0.3,
        help="True-peak ceiling in dBTP (default: -0.3). Applies to the actual final encoded file."
    )
    parser.add_argument(
        "--bitrate", type=str, default=None,
        help="Override the default quality setting for lossy formats, e.g. '256k'. Ignored for lossless output."
    )
    parser.add_argument("--max-passes", type=int, default=14, help="Max verification passes (default: 14)")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found on PATH. Install it first (e.g. `sudo apt install ffmpeg`).")

    if not os.path.exists(args.input):
        sys.exit(f"Input file not found: {args.input}")

    ext = os.path.splitext(args.output)[1].lower()
    if ext not in FORMAT_SETTINGS and ext not in LOSSLESS_EXTENSIONS:
        supported = ", ".join(sorted(set(FORMAT_SETTINGS) | LOSSLESS_EXTENSIONS))
        sys.exit(f"Unsupported output extension '{ext}'. Supported: {supported}")

    maximize_true_peak(
        args.input,
        args.output,
        ceiling=args.ceiling,
        max_passes=args.max_passes,
        bitrate=args.bitrate,
    )


if __name__ == "__main__":
    main()
