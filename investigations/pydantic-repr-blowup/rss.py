import os, threading, time
_PAGE = 4096
def _rss():
    with open("/proc/self/statm", "rb") as f:
        return int(f.read().split()[1]) * _PAGE

class Trace:
    """Sample RSS in a background thread; abort the process if it gets dangerous."""
    def __init__(self, interval=0.02, ceiling_gb=9.0):
        self.interval, self.ceiling = interval, ceiling_gb * 2**30
        self.samples, self._stop = [], threading.Event()
    def __enter__(self):
        self.base = _rss(); self.t0 = time.monotonic()
        self.th = threading.Thread(target=self._run, daemon=True); self.th.start(); return self
    def _run(self):
        while not self._stop.is_set():
            r = _rss(); self.samples.append((time.monotonic() - self.t0, r))
            if r > self.ceiling:
                os.write(2, b"\n!! RSS ceiling exceeded, aborting to protect the box\n"); os._exit(9)
            self._stop.wait(self.interval)
    def __exit__(self, *a):
        self._stop.set(); self.th.join(timeout=1); self.wall = time.monotonic() - self.t0
    @property
    def peak(self): return max(r for _, r in self.samples) if self.samples else self.base
    def sparkline(self, width=60):
        if not self.samples: return ""
        lo, hi = min(r for _,r in self.samples), self.peak
        span = max(hi - lo, 1); bars = "▁▂▃▄▅▆▇█"
        step = max(1, len(self.samples)//width)
        pts = self.samples[::step][:width]
        return "".join(bars[min(7, int((r-lo)/span*7.99))] for _, r in pts)
def gb(n): return "%.2f GB" % (n / 2**30)
