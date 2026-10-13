# Efectos de sonido sintetizados con numpy (sin licencias de terceros). 48 kHz mono float32.
import numpy as np
SR = 48000
rng = np.random.default_rng(7)

def _env(n, a, r):
    e = np.ones(n); na = max(1, int(a * SR)); nr = max(1, int(r * SR))
    e[:na] = np.linspace(0, 1, na) ** 2; e[-nr:] *= np.linspace(1, 0, nr) ** 2
    return e

def _banda(x, f_lo, f_hi, frame=2048):
    """Filtro paso banda variable en el tiempo por STFT (f_lo/f_hi: arrays por trama o escalares)."""
    hop = frame // 4; win = np.hanning(frame); n = len(x)
    out = np.zeros(n + frame); norm = np.zeros(n + frame)
    nfr = 1 + n // hop; freqs = np.fft.rfftfreq(frame, 1 / SR)
    lo = np.broadcast_to(np.atleast_1d(f_lo), (nfr,)) if np.ndim(f_lo) else np.full(nfr, f_lo)
    hi = np.broadcast_to(np.atleast_1d(f_hi), (nfr,)) if np.ndim(f_hi) else np.full(nfr, f_hi)
    xp = np.concatenate([x, np.zeros(frame)])
    for i in range(nfr):
        s = i * hop; seg = xp[s:s + frame] * win
        X = np.fft.rfft(seg)
        m = np.exp(-0.5 * ((np.log(freqs + 1) - np.log((lo[i] * hi[i]) ** 0.5)) / (0.35 + np.log(hi[i] / lo[i]) / 4)) ** 2)
        out[s:s + frame] += np.fft.irfft(X * m) * win; norm[s:s + frame] += win ** 2
    return (out / np.maximum(norm, 1e-6))[:n]

def whoosh(d=0.55, sube=True):
    n = int(d * SR); x = rng.standard_normal(n)
    nfr = 1 + n // 512; t = np.linspace(0, 1, nfr)
    c = (350 + 2600 * t ** 1.5) if sube else (2900 - 2500 * t)
    y = _banda(x, c * 0.6, c * 1.6)
    e = np.sin(np.pi * np.linspace(0, 1, n)) ** 1.6
    y = y * e; return (y / (np.abs(y).max() + 1e-9) * 0.5).astype(np.float32)

def impacto(d=1.6, grave=48.0):
    n = int(d * SR); t = np.arange(n) / SR
    f = grave * (1 + 1.2 * np.exp(-t * 18)); ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-t * 3.2)
    ruido = _banda(rng.standard_normal(n), 80, 900) * np.exp(-t * 14) * 0.6
    y = body + ruido; y = np.tanh(y * 1.4); return (y / np.abs(y).max() * 0.6).astype(np.float32)

def tic(d=0.05):
    n = int(d * SR); t = np.arange(n) / SR
    y = (np.sin(2 * np.pi * 2300 * t) + 0.5 * rng.standard_normal(n)) * np.exp(-t * 160)
    return (y / np.abs(y).max() * 0.35).astype(np.float32)

def escaneo(d=2.5):
    n = int(d * SR); t = np.arange(n) / SR
    f = 180 + 520 * (t / d); y = 0.5 * np.sin(2 * np.pi * np.cumsum(f) / SR) * (0.5 + 0.5 * np.sin(2 * np.pi * 7 * t))
    y += 0.4 * _banda(rng.standard_normal(n), 1500, 6000)
    y *= _env(n, d * 0.4, d * 0.35); return (y / np.abs(y).max() * 0.3).astype(np.float32)

def pulso(d=0.18):
    n = int(d * SR); t = np.arange(n) / SR
    y = np.sin(2 * np.pi * 1450 * t) * np.exp(-t * 35); return (y * 0.22).astype(np.float32)

def ambiente_selva(d):
    """Aire de selva: ruido rosa grave + zumbido de insectos modulado + trinos FM aleatorios."""
    n = int(d * SR); t = np.arange(n) / SR
    rosa = np.cumsum(rng.standard_normal(n)); rosa -= np.convolve(rosa, np.ones(4800) / 4800, 'same'); rosa /= np.abs(rosa).max() + 1e-9
    insectos = _banda(rng.standard_normal(n), 3800, 6200) * (0.55 + 0.45 * np.sin(2 * np.pi * 0.13 * t)) * (0.6 + 0.4 * np.sin(2 * np.pi * 23 * t))
    y = 0.35 * rosa + 0.25 * insectos / (np.abs(insectos).max() + 1e-9)
    for _ in range(int(d / 2.2)):
        s = rng.uniform(0, d - 0.5); L = rng.uniform(0.06, 0.16); k = int(L * SR); tt = np.arange(k) / SR
        f0 = rng.uniform(2200, 3800); sw = rng.uniform(-900, 1200)
        ch = np.sin(2 * np.pi * (f0 * tt + sw * tt ** 2 / (2 * L))) * np.sin(np.pi * tt / L) ** 2
        for rep in range(rng.integers(1, 4)):
            a = int((s + rep * L * 1.3) * SR)
            if a + k < n: y[a:a + k] += ch * rng.uniform(0.08, 0.2)
    return (y / np.abs(y).max() * 0.5).astype(np.float32)


def ambiente_aire(d):
    """Aire neutro (viento suave): sirve para cualquier lugar, a diferencia de la selva."""
    n = int(d * SR); t = np.arange(n) / SR
    x = _banda(rng.standard_normal(n), 120, 900)
    x *= 0.6 + 0.4 * np.sin(2 * np.pi * 0.07 * t + 1.0) * np.sin(2 * np.pi * 0.023 * t)
    return (x / (np.abs(x).max() + 1e-9) * 0.5).astype(np.float32)
