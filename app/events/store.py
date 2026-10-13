"""Atomic, track-keyed JSON. Persist features and annotations, never input audio."""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from app.events.models import Cue


class CueStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, track_id):
        return self.directory / (hashlib.sha256(track_id.encode()).hexdigest() + '.json')

    def read(self, track_id):
        path = self.path(track_id)
        if not path.exists():
            return {'schema_version': 1, 'track_id': track_id, 'events': [], 'analysis': None}
        data = json.loads(path.read_text())
        if data.get('schema_version') != 1 or data.get('track_id') != track_id:
            raise ValueError('Unsupported cue cache version or track identity')
        data['events'] = [Cue.model_validate(c).model_dump(mode='json') for c in data['events']]
        return data

    def write(self, data):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix='.events-')
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out, allow_nan=False)
            os.replace(temporary, self.path(data['track_id']))
        finally:
            Path(temporary).unlink(missing_ok=True)

    def cues(self, track_id):
        return [Cue.model_validate(c) for c in self.read(track_id)['events']]

    def put(self, cue):
        data = self.read(cue.track_id)
        old = next((c for c in data['events'] if c['id'] == cue.id), None)
        if old:
            cue.original_position = old.get('original_position') if old.get('original_position') is not None else old['position']
        cue.user_edited = True
        cue.override = True
        data['events'] = [c for c in data['events'] if c['id'] != cue.id] + [cue.model_dump(mode='json')]
        data['events'].sort(key=lambda c: c['position'])
        self.write(data)
        return cue

    def merge_analysis(self, track_id, candidates, analysis):
        data = self.read(track_id)
        protected = [Cue.model_validate(c) for c in data['events']
                     if c['source'] == 'manual' or c['user_edited'] or c['override']]
        events = list(protected)
        for candidate in candidates:
            # Match original timing as well as edited timing, including rejections.
            if any(c.type == candidate.type and (c.id == candidate.id or
                   abs((c.original_position if c.original_position is not None else c.position) - candidate.position) <= 2 or
                   abs(c.position - candidate.position) <= 2) for c in protected):
                continue
            events.append(candidate)
        data['events'] = [c.model_dump(mode='json') for c in sorted(events, key=lambda c: c.position)]
        # Keep large arrays out of the cue document read by the scheduler/UI.
        data['analysis'] = {k:v for k,v in analysis.items() if k not in ('features','normalized','onset_indices','beat_candidates','downbeat_candidates')}
        self.write(data)
        return data

    def cache_features(self, track_id, analysis):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        target=self.path(track_id).with_suffix('.features.json')
        fd, temporary=tempfile.mkstemp(dir=self.directory,prefix='.features-')
        try:
            with os.fdopen(fd,'w') as out:json.dump(analysis,out,allow_nan=False)
            os.replace(temporary,target)
        finally:Path(temporary).unlink(missing_ok=True)
