[README.md](https://github.com/user-attachments/files/31108951/README.md)
# True Peak Maximize

A true-peak-safe loudness maximizer for lossless and lossy audio. Pushes a file as loud as possible without exceeding a true-peak (inter-sample peak) ceiling — verifying the *actual encoded output* on every pass, rather than trusting a pre-encode prediction.

> Made by [240z] — I mix and master tracks professionally. [https://www.fiverr.com/s/kLdd0bA](FIVERR_LINK_HERE).

## The problem this solves

Most loudness tools measure true peak on your source PCM and apply gain based on that one measurement. That's fine for lossless output, but lossy encoders (MP3, AAC, Vorbis, Opus) can introduce *new* peaks during their own reconstruction that weren't in the original signal at all — meaning a file that measured safely under your ceiling before encoding can end up over it after encoding. This tool catches that by re-measuring the real, final encoded file on every pass and adjusting until it's actually correct — not just predicted to be.

## Features

- Works on **WAV, FLAC, AIFF** (lossless) and **MP3, AAC/M4A, Ogg Vorbis, Opus** (lossy)
- Pure gain maximization — no compression or limiting, so your source's dynamics are untouched
- Verifies against the actual encoded output every pass, catching lossy-codec overshoot other tools miss
- Bracket-and-bisect search converges as close to your ceiling as possible, instead of settling for the first safe-but-conservative result
- High-precision true-peak measurement via ffmpeg's dedicated `ebur128` EBU R128 analyzer

## Requirements

- [ffmpeg](https://ffmpeg.org/) on your PATH, built with `libmp3lame`, `libvorbis`, and `libopus` (standard in most distro builds)
- Python 3.8+ (no third-party packages required)

## Installation

```bash
git clone https://github.com/internetuser22/truepeak_multipass_audio_encoder.git
cd truepeak_multipass_audio_encoder
```

## Usage

```bash
python true_peak_maximize.py input.wav output.wav
python true_peak_maximize.py input.flac output.mp3 --ceiling -0.3
python true_peak_maximize.py input.wav output.opus --bitrate 192k
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--ceiling` | `-0.3` | True-peak ceiling in dBTP. Closer to 0 = louder, less headroom. |
| `--bitrate` | format default | Override the quality setting for lossy formats, e.g. `256k`. Ignored for lossless output. |
| `--max-passes` | `14` | Safety cap on verification passes. |

### Supported formats

- **Lossless:** `.wav` `.flac` `.aiff` `.aif`
- **Lossy:** `.mp3` `.aac` `.m4a` `.ogg` `.opus`

## How it works

1. Measures the source's true peak using ffmpeg's `ebur128` filter.
2. Computes the gain needed to bring that peak up to your ceiling, applies it, and encodes.
3. Re-measures the **actual encoded output**, not the prediction — lossy codecs can overshoot the source-based estimate due to their own reconstruction artifacts.
4. Brackets a confirmed-safe gain and a confirmed-unsafe gain, then bisects between them until the result lands as close to the ceiling as the tolerance allows.
5. Always re-encodes from the **original source** on every pass — never from a previous lossy pass — to avoid stacking generation loss.

## Limitations

- Pure gain only — no limiting or compression. Your loudness ceiling is set by the single loudest true peak in the file, so very dynamic material won't get dramatically "louder" from this alone. For that, you want an actual limiter (not this tool).
- Command-line only, no GUI.
- Bisection assumes a roughly monotonic relationship between gain and resulting true peak. In practice this holds well, but extremely unusual lossy-encoder behavior could in theory produce a non-monotonic result — worth spot-checking the final file's true peak on anything unusual.

## Mixing & Mastering Services

This tool came out of my own mastering workflow. If you'd like an actual human doing your mix or master — not just a loudness pass — I take on mixing and mastering work on Fiverr: **[https://www.fiverr.com/s/kLdd0bA]**

## License

MIT — see [LICENSE](LICENSE).
