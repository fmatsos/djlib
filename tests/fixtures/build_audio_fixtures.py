#!/usr/bin/env python3
"""Builds the deterministic, synthetic-audio fixture library used by
`tests/integration/test_end_to_end.py` (Task 16).

Run as a plain script:

    python tests/fixtures/build_audio_fixtures.py

Every file under `tests/fixtures/library/` is generated from scratch by this
script, using only `ffmpeg` (synthetic tone/noise sources, real encoders) and
`exiftool` (writing a couple of deliberately blank tags for the
`malformed-tags/` fixture) -- there is no copyrighted music anywhere in this
repository, matching `.claude/rules/source-read-only.md`'s spirit and design
Sec.32's "no copyrighted music in the fixture library". `tests/fixtures/library/`
itself is listed in `.gitignore`: only this generator is committed, the
generated tree is procedurally regenerable and never a binary blob in git.

## Determinism

Every source is either a fixed-seed `ffmpeg` `anoisesrc` (white noise) or a
fixed-frequency `sine` lavfi source, always encoded with explicit,
fixed encoder options (bitrate/sample format) and never anything
timestamp- or environment-dependent. Re-running this script on the same
machine reproduces byte-identical output every time -- confirmed by hand
(`cmp` across two runs) for every container this script writes (wav, mp3,
flac, m4a) before this file was committed; `main()` below always deletes and
regenerates the whole tree rather than trusting stale partial output, which
is itself part of what makes repeated runs byte-identical.

## The seven fixture categories (design Sec.32, implementation plan Task 16)

- `exact-copy/`       -- byte-identical audio at two different paths -> `EXACT`.
- `mp3-vs-flac/`       -- one PCM source, two containers/codecs, same filename
                         stem -> `AUDIO_EQUIVALENT` (never `EXACT`: the bytes
                         genuinely differ, only the audio content matches).
- `remix/`             -- two genuinely different (different-seed) audio files
                         sharing an artist/title but an explicit,
                         incompatible version marker ("Original Mix" vs a
                         remix) -- must never be auto-merged
                         (`.claude/rules/duplicate-detection.md`).
- `radio-edit/`        -- same idea as `remix/`: "Original Mix" vs "Radio
                         Edit", explicitly version-incompatible.
- `malformed-tags/`    -- embedded tags are *present* but blank after
                         trimming (a real-world stand-in for corrupted/
                         unusable tag frames), paired with a filename that is
                         also ambiguous -- the resolver must not invent
                         anything and must resolve to `UNKNOWN` (design
                         Sec.10 "never invent metadata").
- `filename-fallback/` -- no embedded tags at all, but a clean
                         `Artist - Title.ext` filename -- the resolver's
                         filename-fallback path.
- `corrupt/`           -- a real, validly-headered mp3 whose middle third is
                         scrambled and which is then truncated (the exact
                         "scramble the middle third and truncate" technique
                         from `tests/unit/test_quality.py`, Task 9): `ffprobe`
                         still reads basic technical metadata (so scanning it
                         doesn't crash -- per-file fault isolation), but a
                         real decode attempt fails partway through, which is
                         exactly Task 9's integrity-check `FAILED` path.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
from pathlib import Path

LIBRARY_ROOT = Path(__file__).resolve().parent / 'library'

REQUIRED_EXECUTABLES = ('ffmpeg', 'exiftool')


def _require_executables() -> None:
    missing = [name for name in REQUIRED_EXECUTABLES if shutil.which(name) is None]
    if missing:
        raise SystemExit(
            f'build_audio_fixtures.py: required executable(s) not found on PATH: '
            f'{", ".join(missing)}'
        )


def _run(argv: list[str]) -> None:
    subprocess.run(argv, check=True)


def _noise_wav(path: Path, seed: int, duration: float) -> None:
    """A deterministic-per-seed, full-spectrum white-noise PCM source.

    `duration` must be >= ~3.0s for any file this is later fingerprinted with
    `fpcalc` (confirmed empirically: fpcalc 1.5.1 returns "Empty fingerprint"
    for this noise source below ~2.9s) -- `exact-copy/`, `malformed-tags/`
    and `filename-fallback/` never get fingerprinted (no duplicate candidate
    ever forms for them), so they may use a shorter duration to keep the
    fixture tree small and generation fast.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            'ffmpeg', '-y', '-v', 'error',
            '-f', 'lavfi', '-i', f'anoisesrc=duration={duration}:color=white:seed={seed}',
            '-ar', '44100', '-sample_fmt', 's16', str(path),
        ]
    )


def _sine_wav(path: Path, frequency: int, duration: float) -> None:
    """A deterministic pure-tone PCM source.

    Used only for `mp3-vs-flac/`: a lossy MP3 pass over full-spectrum white
    noise loses enough chroma-relevant structure that Chromaprint similarity
    between the MP3 and a lossless re-encode of the *same* noise drops well
    below `auto_equivalent` (confirmed empirically: ~0.67, similar to two
    genuinely different noise sources) -- real lossy codecs preserve tonal
    content far more faithfully than broadband noise, which is exactly why a
    plain sine tone is the fixture that actually proves the
    AUDIO_EQUIVALENT-not-EXACT classification this category exists for.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            'ffmpeg', '-y', '-v', 'error',
            '-f', 'lavfi', '-i', f'sine=frequency={frequency}:duration={duration}',
            '-ar', '44100', '-sample_fmt', 's16', str(path),
        ]
    )


def _encode(src: Path, dst: Path, *codec_args: str) -> None:
    _run(['ffmpeg', '-y', '-v', 'error', '-i', str(src), *codec_args, str(dst)])


def _blank_tags(path: Path, *tags: str) -> None:
    args = ['exiftool', '-overwrite_original', *(f'-{tag}= ' for tag in tags), str(path)]
    subprocess.run(args, check=True, capture_output=True, text=True)


def _corrupt_copy_of(source_mp3: Path, dest: Path, seed: int) -> None:
    """Scramble the middle third and truncate (Task 9's `test_quality.py`
    technique, reused verbatim per Task 16's own spec): real ffmpeg-produced
    audio, then genuinely fails to decode cleanly partway through, while
    `ffprobe` can still read basic technical metadata from the still-intact
    leading header/frames.
    """
    data = bytearray(source_mp3.read_bytes())
    n = len(data)
    rnd = random.Random(seed)
    for i in range(n // 3, 2 * n // 3):
        data[i] = rnd.randint(0, 255)
    dest.write_bytes(bytes(data[: int(n * 0.5)]))


def _build_exact_copy() -> None:
    base = LIBRARY_ROOT / 'exact-copy'
    original = base / 'crate_a' / 'Aurora Vale - Parallel Skies.wav'
    duplicate = base / 'crate_b' / 'Aurora Vale - Parallel Skies.wav'
    _noise_wav(original, seed=101, duration=1.5)
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(original.read_bytes())


def _build_mp3_vs_flac() -> None:
    base = LIBRARY_ROOT / 'mp3-vs-flac'
    source = base / '_source.wav'
    _sine_wav(source, frequency=220, duration=3.0)
    _encode(source, base / 'Nova Kessler - Night Current.mp3', '-codec:a', 'libmp3lame', '-b:a', '192k')
    _encode(source, base / 'Nova Kessler - Night Current.flac', '-codec:a', 'flac')
    source.unlink()


def _build_remix() -> None:
    base = LIBRARY_ROOT / 'remix'
    _noise_wav(base / 'Solace Drift - Glass Horizon (Original Mix).wav', seed=201, duration=3.0)
    _noise_wav(base / 'Solace Drift - Glass Horizon (Midnight Remix).wav', seed=202, duration=3.0)


def _build_radio_edit() -> None:
    base = LIBRARY_ROOT / 'radio-edit'
    _noise_wav(base / 'Halcyon Reef - Tidal Bloom (Original Mix).wav', seed=301, duration=3.0)
    _noise_wav(base / 'Halcyon Reef - Tidal Bloom (Radio Edit).wav', seed=302, duration=3.0)


def _build_malformed_tags() -> None:
    """Embedded `Title`/`Artist` tags are *present* but blank after trimming
    (`exiftool` can only reliably write tags into m4a in this environment --
    see `tests/integration/test_track_curation.py`'s own note), and the
    filename itself has no `Artist - Title` separator either -- both the tag
    path and the filename-fallback path are simultaneously unusable, so the
    resolver must land on `UNKNOWN` rather than invent anything.
    """
    base = LIBRARY_ROOT / 'malformed-tags'
    path = base / 'session_final_mix.m4a'
    _noise_wav(base / '_source.wav', seed=401, duration=1.5)
    _encode(base / '_source.wav', path, '-c:a', 'aac')
    (base / '_source.wav').unlink()
    _blank_tags(path, 'Title', 'Artist')


def _build_filename_fallback() -> None:
    base = LIBRARY_ROOT / 'filename-fallback'
    path = base / 'Juno Ashford - Velvet Static.m4a'
    _noise_wav(base / '_source.wav', seed=501, duration=1.5)
    _encode(base / '_source.wav', path, '-c:a', 'aac')
    (base / '_source.wav').unlink()


def _build_corrupt() -> None:
    base = LIBRARY_ROOT / 'corrupt'
    source_wav = base / '_source.wav'
    valid_mp3 = base / '_valid.mp3'
    _noise_wav(source_wav, seed=601, duration=4.0)
    _encode(source_wav, valid_mp3, '-codec:a', 'libmp3lame', '-b:a', '192k')
    _corrupt_copy_of(valid_mp3, base / 'Corrupt Archive - Static Fragment.mp3', seed=602)
    source_wav.unlink()
    valid_mp3.unlink()


def build() -> None:
    _require_executables()
    if LIBRARY_ROOT.exists():
        shutil.rmtree(LIBRARY_ROOT)
    LIBRARY_ROOT.mkdir(parents=True)

    _build_exact_copy()
    _build_mp3_vs_flac()
    _build_remix()
    _build_radio_edit()
    _build_malformed_tags()
    _build_filename_fallback()
    _build_corrupt()


def main() -> int:
    build()
    generated = sorted(p for p in LIBRARY_ROOT.rglob('*') if p.is_file())
    print(f'built {len(generated)} fixture file(s) under {LIBRARY_ROOT}:')
    for path in generated:
        print(f'  {path.relative_to(LIBRARY_ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
