"""
STORM Failover Module - Resolver health tracking and automatic failover

Implements EWMA-based health scoring and dual-resolver redundancy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional


@dataclass
class ResolverHealth:
    """Health metrics for a single resolver"""
    resolver_ip: str
    success_count: int = 0
    fail_count: int = 0
    timeout_count: int = 0
    last_success_at: float = field(default_factory=time.time)
    last_fail_at: float = field(default_factory=time.time)
    blacklist_until: float = 0.0
    latency_ms_ewma: float = 700.0  # Exponential weighted moving average
    success_ewma: float = 0.5  # Success rate EWMA
    
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        if total == 0:
            return 0.5
        return self.success_count / total
    
    def is_blacklisted(self, now: Optional[float] = None) -> bool:
        if now is None:
            now = time.time()
        return now < self.blacklist_until
    
    def overall_score(self, now: Optional[float] = None) -> float:
        """Combined score: success_ewma - blacklist_penalty"""
        if now is None:
            now = time.time()
        penalty = 1000 if self.is_blacklisted(now) else 0
        return self.success_ewma - penalty * 0.01


class ResolverSelector:
    """Manages multiple resolvers with health tracking and failover"""
    
    def __init__(
        self,
        resolvers: list[str],
        ewma_alpha: float = 0.2,
        blacklist_cooldown: float = 10.0,
        fail_threshold: int = 3,
    ):
        if not resolvers:
            raise ValueError("at least one resolver required")
        
        self.resolvers = list(resolvers)
        self.ewma_alpha = max(0.01, min(0.5, ewma_alpha))
        self.blacklist_cooldown = max(1.0, blacklist_cooldown)
        self.fail_threshold = max(1, fail_threshold)
        
        self._lock = Lock()
        self._health: dict[str, ResolverHealth] = {
            r: ResolverHealth(resolver_ip=r)
            for r in self.resolvers
        }
        self._current_primary = self.resolvers[0]
        self._fail_streak: dict[str, int] = {r: 0 for r in self.resolvers}
    
    def _select_primary_locked(self, now: float) -> str:
        """Select primary while caller holds lock."""
        candidates = [
            r for r in self.resolvers
            if not self._health[r].is_blacklisted(now)
        ]
        if not candidates:
            # All blacklisted, pick least-bad
            candidates = self.resolvers
        
        best = max(
            candidates,
            key=lambda r: self._health[r].overall_score(now)
        )
        self._current_primary = best
        return best
    
    def select_primary(self) -> str:
        """Select best resolver for next query"""
        now = time.time()
        with self._lock:
            return self._select_primary_locked(now)
    
    def select_pair(self) -> tuple[str, str]:
        """
        Select primary and secondary for parallel queries.
        Returns (primary, secondary).
        """
        now = time.time()
        with self._lock:
            # Primary: best available
            primary = self._select_primary_locked(now)
            
            # Secondary: next best, different from primary
            secondary = next(
                (r for r in self.resolvers if r != primary),
                primary,
            )
            
            return primary, secondary
    
    def report_success(self, resolver: str, latency_ms: float) -> None:
        """Report successful query"""
        now = time.time()
        with self._lock:
            if resolver not in self._health:
                return
            
            h = self._health[resolver]
            h.success_count += 1
            h.last_success_at = now
            h.blacklist_until = 0.0
            self._fail_streak[resolver] = 0
            
            # Update EWMA
            h.success_ewma = (
                (1.0 - self.ewma_alpha) * h.success_ewma + 
                self.ewma_alpha * 1.0
            )
            h.latency_ms_ewma = (
                (1.0 - self.ewma_alpha) * h.latency_ms_ewma +
                self.ewma_alpha * max(1.0, latency_ms)
            )
    
    def report_failure(
        self,
        resolver: str,
        is_timeout: bool = False,
        latency_ms: Optional[float] = None,
    ) -> None:
        """Report failed query"""
        now = time.time()
        with self._lock:
            if resolver not in self._health:
                return
            
            h = self._health[resolver]
            h.fail_count += 1
            h.last_fail_at = now
            
            if is_timeout:
                h.timeout_count += 1
            
            # Update EWMA
            h.success_ewma = (
                (1.0 - self.ewma_alpha) * h.success_ewma + 
                self.ewma_alpha * 0.0
            )
            if latency_ms is not None:
                h.latency_ms_ewma = (
                    (1.0 - self.ewma_alpha) * h.latency_ms_ewma +
                    self.ewma_alpha * max(1.0, latency_ms)
                )
            
            # Track fail streak
            self._fail_streak[resolver] = self._fail_streak.get(resolver, 0) + 1
            
            # Blacklist on threshold
            if self._fail_streak[resolver] >= self.fail_threshold:
                h.blacklist_until = now + self.blacklist_cooldown
                self._fail_streak[resolver] = 0
    
    def get_health(self, resolver: Optional[str] = None) -> dict:
        """Get health snapshot"""
        with self._lock:
            if resolver:
                if resolver not in self._health:
                    return {}
                h = self._health[resolver]
                return {
                    "resolver": resolver,
                    "success_count": h.success_count,
                    "fail_count": h.fail_count,
                    "timeout_count": h.timeout_count,
                    "success_rate": h.success_rate(),
                    "success_ewma": round(h.success_ewma, 4),
                    "latency_ms_ewma": round(h.latency_ms_ewma, 2),
                    "blacklisted": h.is_blacklisted(),
                    "blacklist_until": h.blacklist_until,
                }
            else:
                return {
                    r: {
                        "resolver": r,
                        "success_count": self._health[r].success_count,
                        "fail_count": self._health[r].fail_count,
                        "timeout_count": self._health[r].timeout_count,
                        "success_rate": self._health[r].success_rate(),
                        "success_ewma": round(self._health[r].success_ewma, 4),
                        "latency_ms_ewma": round(self._health[r].latency_ms_ewma, 2),
                        "blacklisted": self._health[r].is_blacklisted(),
                    }
                    for r in self.resolvers
                }
