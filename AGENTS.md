# HueCanvas
Use .venv for Python commands. Keep hardware access opt-in through MOCK_MODE=false.
Local bridge HTTPS defaults to IP + application key with certificate verification off, per user request.
Preserve the optional HUE_TLS_VERIFY=true mode and verified TLS for cloud discovery.
Never send unsupported light properties.
Keep REST animation updates slow and transitions at least one second.
Beat pulses and high/low flashes use Entertainment DTLS at 20 fps for color bulbs only. Warm-white lamps follow slow music brightness over REST (at most one update per bulb every two seconds, transitions at least one second), or optional steady fill. Keep their worker separate from fast color streaming.
Audio capture starts only on an explicit Listen action; do not save recordings.
Run .venv/bin/python -m pytest before completing functional changes.
Do not claim hardware, Music scripting, or Entertainment streaming has been validated by mock tests.
