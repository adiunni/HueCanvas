"""Compare measured cues with annotations for an explicitly supplied local WAV.

Run from the repository root with .venv/bin/python. Never downloads audio,
opens capture, or controls lights. Null timestamps are intentionally rejected.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.events.analysis import analyze_wav, MAX_BYTES
from app.events.models import EventType


def evaluate(data, annotation):
    digest = hashlib.sha256(data).hexdigest()
    if annotation.get('schema_version') != 1 or annotation.get('audio_sha256') != digest:
        raise ValueError('Fill the annotation schema/version and exact WAV SHA-256 before comparing')
    tolerance = annotation.get('tolerance_seconds', .25)
    if not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or not 0 < tolerance <= 5:
        raise ValueError('Choose a comparison tolerance in (0, 5] seconds')
    expected = annotation.get('events', [])
    for cue in expected:
        EventType(cue['type'])
        at = cue.get('position_seconds')
        if not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0:
            raise ValueError('Annotate each event time; do not guess missing timestamps')
    candidates, analysis = analyze_wav(data, 'reference:' + digest)
    duration = annotation.get('duration_seconds')
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or abs(duration-analysis['duration']) > .1:
        raise ValueError('Annotated duration must match this exact WAV to within 0.1 seconds')
    matches = []
    used = set()
    for cue in expected:
        choices = [c for c in candidates if c.type.value == cue['type'] and c.id not in used]
        nearest = min(choices, key=lambda c: abs(c.position-cue['position_seconds'])) if choices else None
        error = nearest.position-cue['position_seconds'] if nearest else None
        accepted = nearest is not None and abs(error) <= tolerance
        if accepted:used.add(nearest.id)
        matches.append({**cue, 'detected_seconds': nearest.position if nearest else None, 'error_seconds': error, 'matched': accepted})
    return {'audio_sha256': digest, 'algorithm_version': analysis['algorithm_version'], 'matches': matches,
            'unmatched_candidates': [c.model_dump(mode='json') for c in candidates if c.id not in used]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wav', type=Path)
    parser.add_argument('annotations', type=Path)
    args = parser.parse_args()
    try:
        if args.wav.stat().st_size > MAX_BYTES:raise ValueError('WAV exceeds 128 MB')
        print(json.dumps(evaluate(args.wav.read_bytes(), json.loads(args.annotations.read_text())), indent=2, allow_nan=False))
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
