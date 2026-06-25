"""
RTTM Core Solver v2.0 — Method of Characteristics (MOC)
Adds: temperature profile, density profile, equation logging, product integration.
Supports liquid (hydrocarbon / pure fluid) and gas pipelines.
"""
from __future__ import annotations
import numpy as np
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List
import logging, time

logger = logging.getLogger(__name__)

# Global equation log (shared with API layer)
EQUATION_LOG: deque = deque(maxlen=2000)

def _log_eq(step, name, formula, inputs, result, unit, node=None):
    EQUATION_LOG.append({
        "timestamp": round(time.time(), 3),
        "step": step,
        "equation": name,
        "formula": formula,
        "inputs": inputs,
        "result": round(float(result), 6) if np.isfinite(float(result)) else 0.0,
        "unit": unit,
        "node": node,
    })


class FluidType(str, Enum):
    LIQUID = "liquid"
    GAS    = "gas"


@dataclass
class PipelineConfig:
    # Geometry
    length: float           = 50000.0
    diameter: float         = 0.3048
    wall_thickness: float   = 0.008
    roughness: float        = 46e-6
    elevation_in: float     = 0.0
    elevation_out: float    = 0.0
    n_reaches: int          = 20
    # Fluid
    fluid_type: FluidType   = FluidType.LIQUID
    density: float          = 850.0
    viscosity: float        = 0.01
    bulk_modulus: float     = 1.7e9
    young_modulus: float    = 200e9
    temperature_in: float   = 333.15      # K (60°C default)
    specific_heat: float    = 2100.0      # J/(kg·K)
    thermal_expansion: float = 7.2e-4    # 1/K
    z_factor: float         = 0.9
    molar_mass: float       = 0.016
    gamma: float            = 1.3
    # Product metadata
    product_category: str   = "hydrocarbon"
    product_name: str       = "Crude Oil (Arabian Light)"

    def __post_init__(self):
        self.area = np.pi * self.diameter**2 / 4
        self.R_specific = 8.314 / max(self.molar_mass, 1e-6)

    @property
    def wave_speed(self) -> float:
        if self.fluid_type == FluidType.GAS:
            return np.sqrt(self.z_factor * self.R_specific * self.temperature_in)
        K = self.bulk_modulus
        E = self.young_modulus
        return np.sqrt(K / self.density) / np.sqrt(1 + (K * self.diameter) / (E * self.wall_thickness))

    @property
    def dx(self) -> float:
        return self.length / self.n_reaches

    @property
    def dt(self) -> float:
        return self.dx / self.wave_speed


@dataclass
class SimulationState:
    H:       np.ndarray          # Piezometric head [m]
    Q:       np.ndarray          # Flow rate [m³/s]
    T_prof:  np.ndarray          # Temperature profile [K]
    rho_prof: np.ndarray         # Density profile [kg/m³]
    t:       float = 0.0
    step:    int   = 0

    @property
    def n_nodes(self): return len(self.H)

    def to_dict(self, cfg: PipelineConfig) -> dict:
        rho = cfg.density
        g   = 9.81
        P_pa = self.H * rho * g
        x_km = np.linspace(0, cfg.length / 1000, self.n_nodes).tolist()
        return {
            "time":           round(self.t, 4),
            "step":           self.step,
            "x_km":           x_km,
            "pressure_bar":   (P_pa / 1e5).tolist(),
            "head_m":         self.H.tolist(),
            "flow_ls":        (self.Q * 1000).tolist(),
            "temperature_c":  (self.T_prof - 273.15).tolist(),
            "density_kgm3":   self.rho_prof.tolist(),
            "pressures_pa":   P_pa.tolist(),
        }


class MOCSolver:
    def __init__(self, config: PipelineConfig):
        self.cfg = config
        self.g   = 9.81
        a = config.wave_speed
        A = config.area
        self.Bp  = a / (self.g * A)
        self._a  = a
        self._A  = A
        logger.info(f"MOC v2 | a={a:.1f} m/s | dx={config.dx:.0f} m | dt={config.dt:.3f} s")

    def _friction_factor(self, Q: np.ndarray) -> np.ndarray:
        V   = np.abs(Q) / self._A + 1e-9
        Re  = self.cfg.density * V * self.cfg.diameter / self.cfg.viscosity
        eps = self.cfg.roughness
        D   = self.cfg.diameter
        f   = 0.25 / (np.log10(eps/(3.7*D) + 5.74/(Re**0.9 + 1e-9)))**2
        for _ in range(3):
            f = 1.0 / (-2.0*np.log10(eps/(3.7*D) + 2.51/(Re*np.sqrt(f)+1e-9)))**2
        return np.clip(f, 0.008, 0.1)

    def _temperature_profile(self, Q: np.ndarray, f_arr: np.ndarray) -> np.ndarray:
        """Simplified friction-heating temperature rise along pipe."""
        V   = np.abs(Q) / self._A + 1e-9
        D   = self.cfg.diameter
        dx  = self.cfg.dx
        Cp  = self.cfg.specific_heat
        rho = self.cfg.density
        T   = np.zeros(self.cfg.n_reaches + 1)
        T[0] = self.cfg.temperature_in
        for i in range(1, len(T)):
            fi = f_arr[min(i, len(f_arr)-1)]
            vi = V[min(i, len(V)-1)]
            dT = fi * vi**2 * dx / (2.0 * D * Cp)  # Friction heating [K]
            T[i] = T[i-1] + dT
        return T

    def _density_profile(self, H: np.ndarray, T: np.ndarray) -> np.ndarray:
        rho0 = self.cfg.density
        if self.cfg.fluid_type == FluidType.GAS:
            R    = self.cfg.R_specific
            Z    = self.cfg.z_factor
            P    = H * rho0 * self.g
            return P * self.cfg.molar_mass / (Z * 8.314 * T)
        else:
            K    = self.cfg.bulk_modulus
            beta = self.cfg.thermal_expansion
            T0   = self.cfg.temperature_in
            P    = H * rho0 * self.g
            P0   = 101325.0
            return rho0 * (1.0 - beta*(T - T0)) * (1.0 + (P - P0)/K)

    def steady_state(self, P_in: float, Q_in: float) -> SimulationState:
        n   = self.cfg.n_reaches + 1
        Q   = np.ones(n) * Q_in
        f   = self._friction_factor(Q)
        V   = Q_in / self._A
        dH_dx = (f[0] * self.cfg.density * V * abs(V) / (2 * self.cfg.diameter)
                 / (self.cfg.density * self.g)
                 + (self.cfg.elevation_out - self.cfg.elevation_in) / self.cfg.length)
        H_in = P_in / (self.cfg.density * self.g)
        H    = H_in - dH_dx * np.linspace(0, self.cfg.length, n)

        T_prof   = self._temperature_profile(Q, f)
        rho_prof = self._density_profile(H, T_prof)

        # Log steady-state equations
        _log_eq(0, "Wave Speed",
                "a = √(K/ρ) / √(1 + KD/(Ee))" if self.cfg.fluid_type==FluidType.LIQUID else "a = √(ZR_s·T)",
                {"K": self.cfg.bulk_modulus, "rho": self.cfg.density,
                 "D": self.cfg.diameter, "E": self.cfg.young_modulus},
                self._a, "m/s")
        _log_eq(0, "Pipe Impedance", "Bp = a/(g·A)",
                {"a": self._a, "g": self.g, "A": self._A}, self.Bp, "m·s/m³")
        _log_eq(0, "Darcy-Weisbach", "ΔH/Δx = f·V|V|/(2gD) + Δz/L",
                {"f": float(f[0]), "V": V, "D": self.cfg.diameter}, dH_dx, "m/m")
        _log_eq(0, "Colebrook-White", "1/√f = -2log(ε/3.7D + 2.51/(Re√f))",
                {"eps": self.cfg.roughness, "D": self.cfg.diameter,
                 "Re": self.cfg.density*V*self.cfg.diameter/self.cfg.viscosity},
                float(f[0]), "-")
        return SimulationState(H=H, Q=Q.copy(), T_prof=T_prof, rho_prof=rho_prof)

    def step(self, state: SimulationState,
             P_in=None, Q_in=None, P_out=None, Q_out=None,
             leak_node=None, leak_flow=0.0) -> SimulationState:
        H, Q = state.H.copy(), state.Q.copy()
        n    = len(H)
        g, Bp = self.g, self.Bp
        dx, D, A = self._dx_val, self.cfg.diameter, self._A

        f = self._friction_factor(Q)
        R = f * dx / (2 * g * D * A**2)

        H_new, Q_new = np.zeros(n), np.zeros(n)

        for i in range(1, n-1):
            Cp = H[i-1] + Q[i-1]*Bp - R[i-1]*Q[i-1]*abs(Q[i-1])
            Cm = H[i+1] - Q[i+1]*Bp + R[i+1]*Q[i+1]*abs(Q[i+1])
            Q_new[i] = (Cp - Cm) / (2*Bp)
            H_new[i] = Cp - Bp*Q_new[i]
            if leak_node is not None and i == leak_node:
                Q_new[i] -= leak_flow
            if i == n//2:  # Log one interior node per step
                _log_eq(state.step, "MOC C+ characteristic",
                        "Cp = H[i-1] + Q[i-1]·Bp - R·Q|Q|",
                        {"H_a": round(H[i-1],2), "Q_a": round(Q[i-1],5), "Bp": round(Bp,4), "R": round(float(R[i-1]),6)},
                        Cp, "m", node=i)
                _log_eq(state.step, "MOC C- characteristic",
                        "Cm = H[i+1] - Q[i+1]·Bp + R·Q|Q|",
                        {"H_b": round(H[i+1],2), "Q_b": round(Q[i+1],5)},
                        Cm, "m", node=i)

        # Upstream BC
        Cm0 = H[1] - Q[1]*Bp + R[1]*Q[1]*abs(Q[1])
        if P_in is not None:
            H_new[0] = P_in / (self.cfg.density*g)
            Q_new[0] = (H_new[0] - Cm0) / Bp
        elif Q_in is not None:
            Q_new[0] = Q_in
            H_new[0] = Cm0 + Bp*Q_new[0]
        else:
            Q_new[0] = Q[0]; H_new[0] = Cm0 + Bp*Q_new[0]

        # Downstream BC
        CpN = H[n-2] + Q[n-2]*Bp - R[n-2]*Q[n-2]*abs(Q[n-2])
        if P_out is not None:
            H_new[n-1] = P_out / (self.cfg.density*g)
            Q_new[n-1] = (CpN - H_new[n-1]) / Bp
        elif Q_out is not None:
            Q_new[n-1] = Q_out
            H_new[n-1] = CpN - Bp*Q_new[n-1]
        else:
            Q_new[n-1] = Q[n-1]; H_new[n-1] = CpN - Bp*Q_new[n-1]

        # Friction factor log (every 10 steps)
        if state.step % 10 == 0:
            V_mid = abs(Q[n//2]) / A + 1e-9
            Re_mid = self.cfg.density * V_mid * D / self.cfg.viscosity
            _log_eq(state.step, "Reynolds Number", "Re = ρVD/μ",
                    {"rho": self.cfg.density, "V": round(V_mid,4), "D": D, "mu": self.cfg.viscosity},
                    Re_mid, "-", node=n//2)
            _log_eq(state.step, "Friction Resistance", "R = f·Δx/(2g·D·A²)",
                    {"f": round(float(f[n//2]),5), "dx": dx, "D": D, "A": A},
                    float(R[n//2]), "m·s²/m⁶", node=n//2)

        T_prof   = self._temperature_profile(Q_new, f)
        rho_prof = self._density_profile(H_new, T_prof)

        # Log temp and density at midpoint
        if state.step % 5 == 0:
            mid = n//2
            _log_eq(state.step, "Friction Heating", "ΔT = f·V²·Δx/(2D·Cp)",
                    {"f": round(float(f[mid]),5), "V": round(abs(Q_new[mid])/A,4), "Cp": self.cfg.specific_heat},
                    T_prof[mid]-273.15, "°C", node=mid)
            _log_eq(state.step, "Density (T,P correction)",
                    "ρ = ρ₀·(1−β(T−T₀))·(1+(P−P₀)/K)",
                    {"rho0": self.cfg.density, "beta": self.cfg.thermal_expansion,
                     "T": round(T_prof[mid]-273.15,2), "T0": round(self.cfg.temperature_in-273.15,2)},
                    rho_prof[mid], "kg/m³", node=mid)

        return SimulationState(H=H_new, Q=Q_new, T_prof=T_prof, rho_prof=rho_prof,
                               t=state.t+self.cfg.dt, step=state.step+1)

    @property
    def _dx_val(self): return self.cfg.dx
