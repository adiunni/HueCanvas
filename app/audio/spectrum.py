"""Short-window spectrum summaries; raw audio stays in the capture buffer."""
import numpy as np

WINDOW_SIZE = 8192
FLOOR_DB = -90.0
BINS = 72
BANDS = (("bass", "Bass", 20, 250),
         ("mids", "Midrange", 250, 2000),
         ("treble", "Treble", 2000, 20000))


def decibels(amplitude):
    return np.clip(20 * np.log10(np.maximum(amplitude, 10 ** (FLOOR_DB / 20))), FLOOR_DB, 0)


def band_levels(transform, frequencies, window):
    """Integrated RMS for the three shared display/trigger frequency bands."""
    power = 2 * np.abs(transform) ** 2 / (WINDOW_SIZE * np.sum(window ** 2))
    power[[0, -1]] *= .5
    return [float(np.sqrt(power[(frequencies >= low) & (frequencies < min(high, frequencies[-1]))].sum()))
            for _, _, low, high in BANDS]


def analyze_spectrum(samples, sample_rate):
    """Return peak-amplitude bars and integrated RMS per frequency band.

    Hann window correction preserves amplitude; band power uses Parseval's
    normalization. Quiet input therefore stays quiet instead of auto-scaling.
    """
    samples = np.asarray(samples, dtype=np.float64)[-WINDOW_SIZE:]
    if sample_rate < 100 or len(samples) < WINDOW_SIZE or not np.isfinite(samples).all():
        raise ValueError("Spectrum requires a full window of finite audio samples")
    samples = samples - np.mean(samples)
    window = np.hanning(WINDOW_SIZE)
    transform = np.fft.rfft(samples * window)
    frequencies = np.fft.rfftfreq(WINDOW_SIZE, 1 / sample_rate)
    amplitude = 2 * np.abs(transform) / window.sum()
    amplitude[[0, -1]] *= .5
    rms_values = band_levels(transform, frequencies, window)
    upper = min(20000, sample_rate / 2)
    edges = np.geomspace(20, upper, BINS + 1)
    centers = np.sqrt(edges[:-1] * edges[1:])
    values = []
    for low, high, center in zip(edges[:-1], edges[1:], centers):
        selected = amplitude[(frequencies >= low) & (frequencies < high)]
        # Narrow low-frequency display bands can fall between FFT samples.
        values.append(float(selected.max()) if selected.size else float(np.interp(center, frequencies, amplitude)))
    bands = []
    for (key, label, low, high), rms in zip(BANDS, rms_values):
        high = min(high, upper)
        bands.append(dict(id=key, label=label, low=low, high=high, rms=rms,
                          db=round(float(decibels(rms)), 1)))
    audible = (frequencies >= 20) & (frequencies <= upper)
    peak_index = np.flatnonzero(audible)[np.argmax(amplitude[audible])]
    rms = float(np.sqrt(np.mean(samples ** 2)))
    silent = rms < .00005
    return dict(frequencies=centers.round(1).tolist(), edges=edges.round(1).tolist(),
                levels_db=np.round(decibels(values), 1).tolist(), bands=bands,
                floor_db=FLOOR_DB, max_frequency=upper, window_ms=round(WINDOW_SIZE / sample_rate * 1000),
                rms=rms, silent=silent,
                peak_frequency=None if silent else round(float(frequencies[peak_index]), 1),
                peak_db=round(float(decibels(amplitude[peak_index])), 1))
