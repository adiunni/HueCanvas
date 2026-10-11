"""Listen to a DJ controller's MIDI alongside rekordbox (CoreMIDI shares inputs)."""
import time

# Pioneer's position query; the controller answers with every fader position.
# rekordbox sends the same message, so it changes nothing on the controller.
POSITION_QUERY=[0xF0,0x00,0x40,0x05,0x00,0x00,0x04,0x05,0x00,0x50,0x02,0xF7]


class ControllerInput:
    def __init__(self,mixer,name='DDJ-FLX4',*,retry=5,clock=time.monotonic,midi=None):
        self.mixer=mixer;self.name=name;self.retry=retry;self.clock=clock
        self.midi=midi;self.port=None;self.checked_at=None;self.reason='Not connected yet'

    def _module(self):
        if self.midi is None:
            import rtmidi
            self.midi=rtmidi
        return self.midi

    def poll(self):
        """Connect or notice a disconnect; cheap enough for the history poll, throttled."""
        now=self.clock()
        if self.checked_at is not None and now-self.checked_at<self.retry:return
        self.checked_at=now
        try:midi=self._module()
        except ImportError:
            self.reason="Install controller support: .venv/bin/python -m pip install -e '.[rekordbox]'";return
        ports=midi.MidiIn().get_ports()
        index=next((i for i,port in enumerate(ports) if self.name.lower() in port.lower()),None)
        if index is None:
            self.close();self.reason=f'{self.name} is not connected';return
        if self.port is not None:return
        port=midi.MidiIn()
        try:
            port.ignore_types(sysex=False,timing=True,active_sense=True)
            port.open_port(index)
            port.set_callback(lambda event,data=None:self.mixer.handle(event[0]))
        except Exception as exc:
            port.close_port();self.reason=f'Cannot open {self.name}: {exc}';return
        self.port=port;self.reason=None
        try:
            out=midi.MidiOut();out_index=next(i for i,p in enumerate(out.get_ports()) if self.name.lower() in p.lower())
            out.open_port(out_index);out.send_message(POSITION_QUERY);out.close_port()
        except Exception:pass  # Positions then arrive as soon as each control moves.

    @property
    def connected(self):return self.port is not None

    def close(self):
        if self.port is not None:
            try:self.port.cancel_callback();self.port.close_port()
            finally:self.port=None
