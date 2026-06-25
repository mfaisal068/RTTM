"""
RTTM Leak Detection Module v2.0
Fully configurable thresholds via API. Three methods: VB, PD, combined.
"""
from __future__ import annotations
import numpy as np
from collections import deque
from dataclasses import dataclass
from typing import Optional
import logging, time

logger = logging.getLogger(__name__)


@dataclass
class DetectionThresholds:
    """All leak detection thresholds — configurable via POST /config/thresholds."""
    sigma_flow_m3s:    float = 0.0003   # Flow meter 1σ uncertainty
    sigma_pressure_pa: float = 5000.0   # Pressure sensor 1σ noise
    k_warn:            float = 2.0      # Warning threshold multiplier
    k_alarm:           float = 3.0      # Alarm threshold multiplier
    k_critical:        float = 5.0      # Critical threshold multiplier
    vb_window_size:    int   = 10       # VB sliding window samples
    pd_window_size:    int   = 5        # PD sliding window samples
    min_leak_pct:      float = 0.5      # Min detectable leak (% of flow)

    @property
    def vb_warn(self):     return self.k_warn     * np.sqrt(2) * self.sigma_flow_m3s
    @property
    def vb_alarm(self):    return self.k_alarm    * np.sqrt(2) * self.sigma_flow_m3s
    @property
    def vb_critical(self): return self.k_critical * np.sqrt(2) * self.sigma_flow_m3s
    @property
    def pd_alarm(self):    return self.k_alarm    * self.sigma_pressure_pa


@dataclass
class LeakAlarm:
    method: str
    severity: str
    timestamp: float
    imbalance_m3s: float
    imbalance_pct: float
    estimated_location_m: Optional[float]
    estimated_flow_m3s: Optional[float]
    confidence: float
    message: str

    def to_dict(self):
        return {
            "method":              self.method,
            "severity":            self.severity,
            "timestamp":           self.timestamp,
            "imbalance_m3s":       round(self.imbalance_m3s, 6),
            "imbalance_pct":       round(self.imbalance_pct, 4),
            "location_m":          round(self.estimated_location_m, 1) if self.estimated_location_m else None,
            "estimated_leak_m3s":  round(self.estimated_flow_m3s, 6) if self.estimated_flow_m3s else None,
            "confidence":          round(self.confidence, 3),
            "message":             self.message,
        }


class VolumeBalanceDetector:
    def __init__(self, thresholds: DetectionThresholds, nominal_flow: float):
        self.thr = thresholds
        self.nominal = nominal_flow
        self._win: deque = deque(maxlen=thresholds.vb_window_size)

    def update_thresholds(self, thr: DetectionThresholds):
        self.thr = thr
        self._win = deque(maxlen=thr.vb_window_size)

    def update(self, t, Q_in, Q_out, dV_dt=0.0) -> Optional[LeakAlarm]:
        imb = Q_in - Q_out - dV_dt
        self._win.append(imb)
        if len(self._win) < self.thr.vb_window_size:
            return None
        avg = float(np.mean(list(self._win)))
        pct = 100.0 * avg / (self.nominal + 1e-9)

        if abs(avg) >= self.thr.vb_critical:
            sev, conf = "critical", 0.95
        elif abs(avg) >= self.thr.vb_alarm:
            sev, conf = "alarm", 0.78
        elif abs(avg) >= self.thr.vb_warn:
            sev, conf = "warning", 0.52
        else:
            return None

        return LeakAlarm(
            method="volume_balance", severity=sev, timestamp=t,
            imbalance_m3s=avg, imbalance_pct=pct,
            estimated_location_m=None,
            estimated_flow_m3s=abs(avg) if avg > 0 else None,
            confidence=conf,
            message=f"Vol. balance imbalance {avg*1000:.2f} L/s ({pct:.2f}%) — {sev.upper()}",
        )


class PressureDeviationDetector:
    def __init__(self, thresholds: DetectionThresholds, pipe_length: float, n_nodes: int):
        self.thr = thresholds
        self.pipe_length = pipe_length
        self.n_nodes = n_nodes
        self._win: deque = deque(maxlen=thresholds.pd_window_size)

    def update_thresholds(self, thr: DetectionThresholds):
        self.thr = thr
        self._win = deque(maxlen=thr.pd_window_size)

    def update(self, t, P_meas, P_sim, Q_nominal=0.15) -> Optional[LeakAlarm]:
        res = P_meas - P_sim
        self._win.append(res)
        if len(self._win) < self.thr.pd_window_size:
            return None
        avg_res    = np.mean(list(self._win), axis=0)
        max_res    = float(np.max(np.abs(avg_res)))
        threshold  = self.thr.pd_alarm

        if max_res < threshold:
            return None

        leak_node = int(np.argmin(avg_res))
        leak_x    = leak_node * self.pipe_length / (self.n_nodes - 1)
        Q_est     = abs(float(avg_res[leak_node])) / (850 * 1229 * 0.05)

        sev  = "critical" if max_res > 3*threshold else "alarm"
        conf = min(0.95, 0.5 + 0.45*(max_res/threshold - 1))

        return LeakAlarm(
            method="pressure_deviation", severity=sev, timestamp=t,
            imbalance_m3s=Q_est, imbalance_pct=100*Q_est/(Q_nominal+1e-9),
            estimated_location_m=leak_x,
            estimated_flow_m3s=Q_est, confidence=conf,
            message=f"Pressure residual {max_res/1000:.1f} kPa at node {leak_node} (x≈{leak_x/1000:.1f} km) — {sev.upper()}",
        )


class LeakDetectionEngine:
    def __init__(self, pipe_length=50000, n_nodes=21,
                 nominal_flow=0.15, thresholds: Optional[DetectionThresholds]=None):
        self.thresholds = thresholds or DetectionThresholds()
        self.vb  = VolumeBalanceDetector(self.thresholds, nominal_flow)
        self.pd  = PressureDeviationDetector(self.thresholds, pipe_length, n_nodes)
        self._log: list[LeakAlarm] = []

    def update_thresholds(self, thr: DetectionThresholds):
        self.thresholds = thr
        self.vb.update_thresholds(thr)
        self.pd.update_thresholds(thr)

    def analyze(self, t, Q_in, Q_out, P_meas, P_sim, nominal_flow=0.15) -> dict:
        alarms = []
        a1 = self.vb.update(t, Q_in, Q_out)
        if a1: alarms.append(a1); self._log.append(a1)
        a2 = self.pd.update(t, P_meas, P_sim, nominal_flow)
        if a2: alarms.append(a2); self._log.append(a2)

        rank = {"warning":1,"alarm":2,"critical":3}
        highest = max((a.severity for a in alarms), key=lambda s: rank.get(s,0), default="none")
        conf = 0.0
        if len(alarms)==2: conf = min(1.0, alarms[0].confidence + alarms[1].confidence*0.5)
        elif len(alarms)==1: conf = alarms[0].confidence

        return {
            "t": t, "any_alarm": bool(alarms),
            "highest_severity": highest,
            "fused_confidence": round(conf, 3),
            "alarms": [a.to_dict() for a in alarms],
            "alarm_count_session": len(self._log),
            "thresholds": {
                "vb_warn_ls":  round(self.thresholds.vb_warn*1000, 3),
                "vb_alarm_ls": round(self.thresholds.vb_alarm*1000, 3),
                "pd_alarm_kpa": round(self.thresholds.pd_alarm/1000, 2),
            }
        }

    def get_alarm_history(self, last_n=100):
        return [a.to_dict() for a in self._log[-last_n:]]
