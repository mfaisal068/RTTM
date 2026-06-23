"""
RTTM Core Solver — Method of Characteristics (MOC)
Supports both liquid and gas (isothermal) pipelines.
Based on Wylie & Streeter (1993) and API 1130/1149 guidelines.
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class FluidType(str, Enum):
    LIQUID = "liquid"
    GAS = "gas"


@dataclass
class PipelineConfig:
    """Physical configuration of the monitored pipeline segment."""
    # Geometry
    length: float           # Total pipe length [m]
    diameter: float         # Internal diameter [m]
    wall_thickness: float   # Pipe wall thickness [m]
    roughness: float = 46e-6  # Absolute roughness [m] (commercial steel default)
    elevation_in: float = 0.0   # Inlet elevation [m]
    elevation_out: float = 0.0  # Outlet elevation [m]
    n_reaches: int = 20     # Number of MOC spatial segments

    # Fluid properties
    fluid_type: FluidType = FluidType.LIQUID
    density: float = 850.0          # [kg/m³]  (crude oil default)
    viscosity: float = 0.01         # Dynamic viscosity [Pa·s]
    bulk_modulus: float = 1.7e9     # [Pa] liquid bulk modulus
    young_modulus: float = 200e9    # [Pa] pipe steel Young's modulus
    temperature: float = 293.15     # [K] for gas pipelines
    z_factor: float = 0.9           # Gas compressibility factor
    gamma: float = 1.3              # Cp/Cv for gas
    molar_mass: float = 0.016       # [kg/mol] methane default
    R_gas: float = 8.314            # Universal gas constant [J/mol·K]

    def __post_init__(self):
        self.area = np.pi * self.diameter**2 / 4   # [m²]
        self.R_specific = self.R_gas / self.molar_mass  # [J/kg·K]

    @property
    def wave_speed(self) -> float:
        """Acoustic wave speed [m/s]."""
        if self.fluid_type == FluidType.LIQUID:
            # Wylie & Streeter formulation
            K = self.bulk_modulus
            E = self.young_modulus
            D = self.diameter
            e = self.wall_thickness
            rho = self.density
            return np.sqrt(K / rho) / np.sqrt(1 + (K * D) / (E * e))
        else:
            # Isothermal gas: a = sqrt(Z·R_specific·T)
            return np.sqrt(self.z_factor * self.R_specific * self.temperature)

    @property
    def dx(self) -> float:
        return self.length / self.n_reaches

    @property
    def dt(self) -> float:
        """Time step satisfying CFL = 1."""
        return self.dx / self.wave_speed


@dataclass
class SimulationState:
    """Holds the current state of the MOC grid."""
    H: np.ndarray   # Piezometric head at each node [m]
    Q: np.ndarray   # Flow rate at each node [m³/s]
    t: float = 0.0  # Current simulation time [s]
    step: int = 0

    @property
    def n_nodes(self) -> int:
        return len(self.H)

    def to_dict(self) -> dict:
        return {
            "time": round(self.t, 4),
            "step": self.step,
            "head_m": self.H.tolist(),
            "flow_m3s": self.Q.tolist(),
            "pressure_kPa": (self.H * 9.81 * 850 / 1000).tolist(),  # approx
        }


class MOCSolver:
    """
    Method of Characteristics solver for 1-D pipeline transient flow.

    Solves the water-hammer / gas-flow PDEs:
        Continuity:  ∂H/∂t + (a²/g)·(∂V/∂x) = 0
        Momentum:    ∂V/∂t + g·(∂H/∂x) + f·V|V|/(2D) = 0

    Discretized along C+ and C- characteristic lines with Δx = a·Δt.
    """

    def __init__(self, config: PipelineConfig):
        self.cfg = config
        self.g = 9.81
        self._validate_config()

        # Pre-compute constants
        a   = config.wave_speed
        A   = config.area
        D   = config.diameter
        dx  = config.dx
        g   = self.g

        self.Bp = a / (g * A)           # Pipe impedance [m·s/m³]
        self._a = a
        self._A = A

        # Friction resistance (updated per step based on local f)
        self._dx = dx
        self._D = D

        logger.info(
            f"MOC solver initialised | a={a:.1f} m/s | dx={dx:.0f} m | "
            f"dt={config.dt:.3f} s | nodes={config.n_reaches + 1}"
        )

    def _validate_config(self):
        c = self.cfg
        if c.n_reaches < 2:
            raise ValueError("n_reaches must be >= 2")
        if c.diameter <= 0 or c.length <= 0:
            raise ValueError("Diameter and length must be positive")

    def _friction_factor(self, Q: np.ndarray) -> np.ndarray:
        """Colebrook-White friction factor per node (iterative)."""
        V = np.abs(Q) / self._A + 1e-9
        Re = self.cfg.density * V * self._D / self.cfg.viscosity
        eps = self.cfg.roughness
        D = self._D

        # Swamee-Jain as initial guess
        f = 0.25 / (np.log10(eps / (3.7 * D) + 5.74 / (Re**0.9 + 1e-9)))**2

        # Two Newton iterations of Colebrook-White
        for _ in range(3):
            lhs = 1.0 / np.sqrt(f + 1e-9)
            rhs = -2.0 * np.log10(eps / (3.7 * D) + 2.51 / (Re * np.sqrt(f) + 1e-9))
            f = 1.0 / (rhs**2 + 1e-9)

        return np.clip(f, 0.008, 0.1)

    def steady_state(self, P_in: float, Q_in: float) -> SimulationState:
        """
        Initialise the MOC grid from steady-state conditions.

        Args:
            P_in:  Inlet pressure [Pa]
            Q_in:  Inlet flow rate [m³/s]

        Returns:
            SimulationState with linearly interpolated head and uniform Q.
        """
        n = self.cfg.n_reaches + 1
        Q = np.ones(n) * Q_in
        f = self._friction_factor(Q)

        # Darcy-Weisbach pressure drop per reach
        V = Q_in / self._A
        dP_dx = f[0] * self.cfg.density * V * abs(V) / (2 * self._D)
        dH_dx = dP_dx / (self.cfg.density * self.g) + \
                (self.cfg.elevation_out - self.cfg.elevation_in) / self.cfg.length

        H_in = P_in / (self.cfg.density * self.g)
        x_nodes = np.linspace(0, self.cfg.length, n)
        H = H_in - dH_dx * x_nodes

        return SimulationState(H=H, Q=Q.copy())

    def step(
        self,
        state: SimulationState,
        P_in: Optional[float] = None,
        Q_in: Optional[float] = None,
        P_out: Optional[float] = None,
        Q_out: Optional[float] = None,
        leak_node: Optional[int] = None,
        leak_flow: float = 0.0,
    ) -> SimulationState:
        """
        Advance the MOC solution by one time step Δt.

        Boundary conditions (supply at least one per end):
          Upstream:   P_in [Pa] OR Q_in [m³/s]
          Downstream: P_out [Pa] OR Q_out [m³/s]

        Args:
            state:      Current SimulationState
            P_in:       Upstream pressure BC [Pa]
            Q_in:       Upstream flow BC [m³/s]
            P_out:      Downstream pressure BC [Pa]
            Q_out:      Downstream flow BC [m³/s]
            leak_node:  Node index where a leak is injected (optional)
            leak_flow:  Leak outflow [m³/s]

        Returns:
            New SimulationState at t + Δt
        """
        H = state.H.copy()
        Q = state.Q.copy()
        n = len(H)
        g = self.g
        Bp = self.Bp
        dx = self._dx
        D = self._D
        A = self._A

        f = self._friction_factor(Q)
        R = f * dx / (2 * g * D * A**2)   # friction resistance [m·s²/m⁶]

        H_new = np.zeros(n)
        Q_new = np.zeros(n)

        # ── Interior nodes (MOC) ─────────────────────────────────────────
        for i in range(1, n - 1):
            # C+ from node i-1, C- from node i+1
            Cp = H[i-1] + Q[i-1] * Bp - R[i-1] * Q[i-1] * abs(Q[i-1])
            Cm = H[i+1] - Q[i+1] * Bp + R[i+1] * Q[i+1] * abs(Q[i+1])

            Q_new[i] = (Cp - Cm) / (2 * Bp)
            H_new[i] = Cp - Bp * Q_new[i]

            # Inject leak at specified node
            if leak_node is not None and i == leak_node:
                Q_new[i] -= leak_flow

        # ── Upstream boundary (node 0) ────────────────────────────────────
        Cm0 = H[1] - Q[1] * Bp + R[1] * Q[1] * abs(Q[1])
        if P_in is not None:
            H_new[0] = P_in / (self.cfg.density * g)
            Q_new[0] = (H_new[0] - Cm0) / Bp
        elif Q_in is not None:
            Q_new[0] = Q_in
            H_new[0] = Cm0 + Bp * Q_new[0]
        else:
            # Keep last known value
            Q_new[0] = Q[0]
            H_new[0] = Cm0 + Bp * Q_new[0]

        # ── Downstream boundary (node n-1) ────────────────────────────────
        CpN = H[n-2] + Q[n-2] * Bp - R[n-2] * Q[n-2] * abs(Q[n-2])
        if P_out is not None:
            H_new[n-1] = P_out / (self.cfg.density * g)
            Q_new[n-1] = (CpN - H_new[n-1]) / Bp
        elif Q_out is not None:
            Q_new[n-1] = Q_out
            H_new[n-1] = CpN - Bp * Q_new[n-1]
        else:
            Q_new[n-1] = Q[n-1]
            H_new[n-1] = CpN - Bp * Q_new[n-1]

        return SimulationState(
            H=H_new, Q=Q_new,
            t=state.t + self.cfg.dt,
            step=state.step + 1
        )
