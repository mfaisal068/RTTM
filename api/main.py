"""
RTTM FastAPI Application
Exposes the MOC solver and leak detection engine as REST + WebSocket endpoints.

Endpoints:
  POST /pipeline/configure       — Configure pipeline geometry and fluid
  POST /simulation/initialize    — Set initial steady-state conditions
  POST /simulation/step          — Advance one MOC time step
  GET  /simulation/state         — Get current simulation state
  POST /leakdetection/analyze    — Run leak detection on current data
  GET  /leakdetection/history    — Retrieve alarm history
  GET  /health                   — Service health check
  WS   /ws/stream                — WebSocket real-time stream
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Internal modules
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.solver import PipelineConfig, MOCSolver, FluidType, SimulationState
from detection.leak_detector import LeakDetectionEngine

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rttm.api")

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="RTTM Pipeline Monitoring API",
    description=(
        "Real-Time Transient Model engine for gas and liquid pipeline "
        "leak detection. Implements Method of Characteristics (MOC) solver "
        "with API 1130-aligned volume balance and pressure deviation detectors."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Shared engine state (singleton per worker) ───────────────────────────────
_solver: Optional[MOCSolver] = None
_state: Optional[SimulationState] = None
_detector: Optional[LeakDetectionEngine] = None
_config: Optional[PipelineConfig] = None
_ws_clients: list[WebSocket] = []


# ─── Request / Response Schemas ───────────────────────────────────────────────

class PipelineConfigRequest(BaseModel):
    length_m: float = Field(..., gt=0, description="Pipe length [m]")
    diameter_m: float = Field(..., gt=0, description="Internal diameter [m]")
    wall_thickness_m: float = Field(..., gt=0, description="Wall thickness [m]")
    roughness_m: float = Field(46e-6, description="Absolute roughness [m]")
    elevation_in_m: float = Field(0.0, description="Inlet elevation [m]")
    elevation_out_m: float = Field(0.0, description="Outlet elevation [m]")
    n_reaches: int = Field(20, ge=5, le=200, description="MOC spatial segments")
    fluid_type: str = Field("liquid", description="'liquid' or 'gas'")
    density_kg_m3: float = Field(850.0, gt=0, description="Fluid density [kg/m³]")
    viscosity_pa_s: float = Field(0.01, gt=0, description="Dynamic viscosity [Pa·s]")
    bulk_modulus_pa: float = Field(1.7e9, description="Bulk modulus [Pa] (liquid)")
    young_modulus_pa: float = Field(200e9, description="Steel Young's modulus [Pa]")
    temperature_k: float = Field(293.15, description="Temperature [K] (gas)")
    z_factor: float = Field(0.9, description="Gas compressibility factor")
    nominal_flow_m3s: float = Field(0.15, description="Nominal flow rate [m³/s]")
    sigma_flow_m3s: float = Field(0.0003, description="Flow meter uncertainty [m³/s]")
    sigma_pressure_pa: float = Field(5000.0, description="Pressure sensor noise [Pa]")


class InitializeRequest(BaseModel):
    inlet_pressure_pa: float = Field(..., gt=0, description="Inlet pressure [Pa]")
    inlet_flow_m3s: float = Field(..., gt=0, description="Inlet flow [m³/s]")


class StepRequest(BaseModel):
    inlet_pressure_pa: Optional[float] = Field(None, description="Upstream BC: pressure [Pa]")
    inlet_flow_m3s: Optional[float] = Field(None, description="Upstream BC: flow [m³/s]")
    outlet_pressure_pa: Optional[float] = Field(None, description="Downstream BC: pressure [Pa]")
    outlet_flow_m3s: Optional[float] = Field(None, description="Downstream BC: flow [m³/s]")
    simulated_leak_node: Optional[int] = Field(None, description="Node index for injected leak (testing)")
    simulated_leak_flow_m3s: float = Field(0.0, description="Leak flow to inject [m³/s]")


class AnalyzeRequest(BaseModel):
    measured_inlet_flow_m3s: float = Field(..., description="SCADA inlet flow [m³/s]")
    measured_outlet_flow_m3s: float = Field(..., description="SCADA outlet flow [m³/s]")
    measured_pressures_pa: list[float] = Field(
        ..., description="Pressures at sensor nodes [Pa]"
    )
    sensor_node_indices: list[int] = Field(
        ..., description="Node indices corresponding to measured pressures"
    )


# ─── Helper ───────────────────────────────────────────────────────────────────

def _require_initialized():
    if _solver is None or _state is None:
        raise HTTPException(
            status_code=400,
            detail="Pipeline not configured. Call POST /pipeline/configure then POST /simulation/initialize."
        )


def _rho_g_H_to_pa(H: np.ndarray) -> np.ndarray:
    """Convert piezometric head [m] to pressure [Pa]."""
    rho = _config.density if _config else 850.0
    return H * rho * 9.81


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
def health():
    """Service health check."""
    return {
        "status": "ok",
        "engine": "rttm-moc-v1",
        "configured": _solver is not None,
        "initialized": _state is not None,
        "sim_time_s": round(_state.t, 3) if _state else None,
        "sim_step": _state.step if _state else None,
        "timestamp": time.time(),
    }


@app.post("/pipeline/configure", tags=["configuration"])
def configure_pipeline(req: PipelineConfigRequest):
    """
    Configure pipeline geometry and fluid properties.
    Must be called before /simulation/initialize.
    """
    global _solver, _state, _detector, _config

    try:
        fluid = FluidType.LIQUID if req.fluid_type == "liquid" else FluidType.GAS
        cfg = PipelineConfig(
            length=req.length_m,
            diameter=req.diameter_m,
            wall_thickness=req.wall_thickness_m,
            roughness=req.roughness_m,
            elevation_in=req.elevation_in_m,
            elevation_out=req.elevation_out_m,
            n_reaches=req.n_reaches,
            fluid_type=fluid,
            density=req.density_kg_m3,
            viscosity=req.viscosity_pa_s,
            bulk_modulus=req.bulk_modulus_pa,
            young_modulus=req.young_modulus_pa,
            temperature=req.temperature_k,
            z_factor=req.z_factor,
        )

        _config = cfg
        _solver = MOCSolver(cfg)
        _state = None   # Reset state until initialized
        _detector = LeakDetectionEngine(
            pipe_length=req.length_m,
            n_nodes=req.n_reaches + 1,
            nominal_flow=req.nominal_flow_m3s,
            sigma_flow=req.sigma_flow_m3s,
            sigma_pressure=req.sigma_pressure_pa,
        )

        return {
            "status": "configured",
            "wave_speed_m_s": round(cfg.wave_speed, 2),
            "dx_m": round(cfg.dx, 2),
            "dt_s": round(cfg.dt, 4),
            "n_nodes": cfg.n_reaches + 1,
            "pipe_area_m2": round(cfg.area, 6),
            "fluid_type": req.fluid_type,
        }
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/simulation/initialize", tags=["simulation"])
def initialize_simulation(req: InitializeRequest):
    """
    Set steady-state initial conditions for the MOC grid.
    """
    global _state
    if _solver is None:
        raise HTTPException(status_code=400, detail="Configure pipeline first.")

    _state = _solver.steady_state(
        P_in=req.inlet_pressure_pa,
        Q_in=req.inlet_flow_m3s,
    )

    return {
        "status": "initialized",
        "n_nodes": _state.n_nodes,
        "head_inlet_m": round(_state.H[0], 2),
        "head_outlet_m": round(_state.H[-1], 2),
        "flow_m3s": round(_state.Q[0], 5),
        "sim_time_s": _state.t,
    }


@app.post("/simulation/step", tags=["simulation"])
async def simulation_step(req: StepRequest):
    """
    Advance the MOC simulation by one time step (Δt = a·Δx).
    Provide at least one boundary condition per pipe end.
    """
    global _state
    _require_initialized()

    try:
        _state = _solver.step(
            state=_state,
            P_in=req.inlet_pressure_pa,
            Q_in=req.inlet_flow_m3s,
            P_out=req.outlet_pressure_pa,
            Q_out=req.outlet_flow_m3s,
            leak_node=req.simulated_leak_node,
            leak_flow=req.simulated_leak_flow_m3s,
        )

        result = _state.to_dict()
        result["dt_s"] = round(_config.dt, 4)

        # Broadcast to WebSocket clients
        if _ws_clients:
            asyncio.create_task(_broadcast_state(result))

        return result

    except Exception as e:
        logger.error(f"Step failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/simulation/state", tags=["simulation"])
def get_state():
    """Retrieve the current RTTM simulation state."""
    _require_initialized()
    result = _state.to_dict()
    result["pressures_pa"] = _rho_g_H_to_pa(_state.H).tolist()
    return result


@app.post("/leakdetection/analyze", tags=["leak_detection"])
def analyze_leak(req: AnalyzeRequest):
    """
    Run leak detection using both volume balance and pressure deviation methods.

    Compares SCADA measurements against current RTTM simulation state.
    """
    _require_initialized()

    if _detector is None:
        raise HTTPException(status_code=400, detail="Detector not initialized.")

    # Build full pressure arrays aligned with RTTM nodes
    n_nodes = _state.n_nodes
    P_simulated = _rho_g_H_to_pa(_state.H)

    # Map measured pressures to nodes
    P_measured = P_simulated.copy()   # default = no deviation
    for idx, node_i in enumerate(req.sensor_node_indices):
        if 0 <= node_i < n_nodes and idx < len(req.measured_pressures_pa):
            P_measured[node_i] = req.measured_pressures_pa[idx]

    result = _detector.analyze(
        t=_state.t,
        Q_in=req.measured_inlet_flow_m3s,
        Q_out=req.measured_outlet_flow_m3s,
        P_measured=P_measured,
        P_simulated=P_simulated,
        nominal_flow=req.measured_inlet_flow_m3s,
    )

    return result


@app.get("/leakdetection/history", tags=["leak_detection"])
def get_alarm_history(last_n: int = 50):
    """Retrieve recent alarm history."""
    if _detector is None:
        raise HTTPException(status_code=400, detail="Detector not initialized.")
    return {
        "alarms": _detector.get_alarm_history(last_n),
        "total_session_alarms": len(_detector._alarm_log),
    }


# ─── WebSocket streaming ───────────────────────────────────────────────────────

async def _broadcast_state(payload: dict):
    import json
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    """
    WebSocket endpoint for real-time RTTM state streaming.
    Receives simulation steps and pushes state to connected clients.
    """
    await websocket.accept()
    _ws_clients.append(websocket)
    logger.info(f"WS client connected. Total clients: {len(_ws_clients)}")

    try:
        while True:
            # Keep connection alive; state is pushed via _broadcast_state
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        _ws_clients.remove(websocket)
        logger.info("WS client disconnected.")


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
