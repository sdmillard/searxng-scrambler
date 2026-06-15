import random
import time
from dataclasses import dataclass, field


@dataclass
class _Health:
    healthy: bool = True
    cooldown_until: float = 0.0
    failures: int = 0       # consecutive failures since last success (resets on success)
    total_failures: int = 0 # all-time failure count (never resets)
    hits: int = 0
    ema_seconds: float = 5.0  # initial neutral estimate


class InstanceSelector:
    def __init__(self, instances: list, cooldown: int = 300, weighted: bool = True):
        self._instances = list(instances)
        self._cooldown = cooldown
        self._health: dict = {u: _Health() for u in instances}
        self._engine_map: dict = {}  # url → set of lowercase engine names
        self.weighted = weighted

    def set_engine_map(self, engine_map: dict) -> None:
        self._engine_map = {
            k.rstrip("/"): {e.lower() for e in v}
            for k, v in engine_map.items()
        }

    def pick_with_engines(self, wanted: list, exclude: set | None = None) -> str | None:
        """Pick the healthiest available instance that covers the most wanted engines.
        Falls back to normal pick() when no engine metadata is available."""
        exclude = exclude or set()
        now = time.monotonic()
        available = [u for u in self._instances if u not in exclude and self._is_healthy(u, now)]
        if not available:
            self._tick_cooldowns(now)
            available = [u for u in self._instances if u not in exclude and self._is_healthy(u, now)]
        if not available:
            return None
        if not self._engine_map:
            return self.pick(exclude)
        wanted_lower = {e.lower() for e in wanted}
        scored = [(u, len(wanted_lower & (self._engine_map.get(u) or set()))) for u in available]
        best = max(s for _, s in scored)
        if best == 0:
            return self.pick(exclude)
        candidates = [u for u, s in scored if s == best]
        if not self.weighted or len(candidates) == 1:
            return random.choice(candidates)
        weights = [1.0 / self._health[u].ema_seconds for u in candidates]
        return random.choices(candidates, weights=weights, k=1)[0]

    def pick(self, exclude: set | None = None) -> str | None:
        exclude = exclude or set()
        now = time.monotonic()
        available = [u for u in self._instances if u not in exclude and self._is_healthy(u, now)]
        if not available:
            self._tick_cooldowns(now)
            available = [u for u in self._instances if u not in exclude and self._is_healthy(u, now)]
        if not available:
            return None
        if not self.weighted or len(available) == 1:
            return random.choice(available)
        weights = [1.0 / self._health[u].ema_seconds for u in available]
        return random.choices(available, weights=weights, k=1)[0]

    def record_time(self, url: str, elapsed: float) -> None:
        h = self._health.get(url)
        if h:
            h.ema_seconds = 0.3 * elapsed + 0.7 * h.ema_seconds

    def mark_unhealthy(self, url: str, cooldown: int | None = None) -> None:
        h = self._health.get(url)
        if h:
            h.healthy = False
            h.failures += 1
            h.total_failures += 1
            h.cooldown_until = time.monotonic() + (cooldown if cooldown is not None else self._cooldown)

    def mark_healthy(self, url: str) -> None:
        h = self._health.get(url)
        if h:
            h.healthy = True
            h.failures = 0
            h.hits += 1

    def get_stats(self) -> list:
        now = time.monotonic()
        return [
            {
                "url": url,
                "healthy": self._is_healthy(url, now),
                "hits": h.hits,
                "failures": h.failures,
                "avg_ms": round(h.ema_seconds * 1000),
            }
            for url, h in self._health.items()
            if url in self._instances
        ]

    def load_stats(self, data: dict) -> None:
        """Restore persisted health data for known URLs. Safe to call at any time."""
        for url, s in data.items():
            if url not in self._health:
                self._health[url] = _Health()
            h = self._health[url]
            h.ema_seconds = float(s.get("ema_seconds", h.ema_seconds))
            h.hits = int(s.get("hits", h.hits))
            h.total_failures = int(s.get("total_failures", h.total_failures))

    def dump_stats(self) -> dict:
        """Serialize health state for persistence."""
        return {
            url: {
                "ema_seconds": h.ema_seconds,
                "hits": h.hits,
                "total_failures": h.total_failures,
            }
            for url, h in self._health.items()
        }

    def update_instances(self, instances: list) -> None:
        self._instances = list(instances)
        for u in instances:
            if u not in self._health:
                self._health[u] = _Health()

    def _is_healthy(self, url: str, now: float) -> bool:
        h = self._health.get(url)
        if h is None:
            return False
        if h.healthy:
            return True
        if now >= h.cooldown_until:
            h.healthy = True
            h.failures = 0
            return True
        return False

    def _tick_cooldowns(self, now: float) -> None:
        for h in self._health.values():
            if not h.healthy and now >= h.cooldown_until:
                h.healthy = True
                h.failures = 0
