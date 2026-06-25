"""RTTM FastAPI v2.1 — MOC solver, configurable thresholds, equation log, multi-step, history."""
from __future__ import annotations
import asyncio, logging, time
from typing import Optional
import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.solver import PipelineConfig, MOCSolver, FluidType, SimulationState, EQUATION_LOG
from core.products import PRODUCTS, get_product, list_products, ProductCategory
from detection.leak_detector import LeakDetectionEngine, DetectionThresholds

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rttm.api")

app = FastAPI(title="RTTM Pipeline Monitoring API", version="2.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Singleton state ────────────────────────────────────────────────────────────
_solver:   Optional[MOCSolver]           = None
_state:    Optional[SimulationState]     = None
_detector: Optional[LeakDetectionEngine] = None
_config:   Optional[PipelineConfig]      = None
_runtime:  dict = {"max_duration_s": 3600.0, "step_interval_ms": 1000}
_ws_clients: list[WebSocket]             = []
_history:  list[dict]                    = []


# ── Schemas ───────────────────────────────────────────────────────────────────
class PipelineConfigRequest(BaseModel):
    length_m:           float = Field(50000, gt=0)
    diameter_m:         float = Field(0.3048, gt=0)
    wall_thickness_m:   float = Field(0.008, gt=0)
    roughness_m:        float = 46e-6
    elevation_in_m:     float = 0.0
    elevation_out_m:    float = 0.0
    n_reaches:          int   = Field(20, ge=5, le=500)
    fluid_type:         str   = "liquid"
    density_kg_m3:      float = Field(850.0, gt=0)
    viscosity_pa_s:     float = Field(0.01, gt=0)
    bulk_modulus_pa:    float = 1.7e9
    young_modulus_pa:   float = 200e9
    temperature_in_k:   float = 333.15
    specific_heat:      float = 2100.0
    thermal_expansion:  float = 7.2e-4
    z_factor:           float = 0.9
    molar_mass_kg_mol:  float = 0.016
    gamma:              float = 1.3
    product_category:   str   = "hydrocarbon"
    product_name:       str   = "Crude Oil (Arabian Light)"
    nominal_flow_m3s:   float = 0.15
    sigma_flow_m3s:     float = 0.0003
    sigma_pressure_pa:  float = 5000.0

class InitRequest(BaseModel):
    inlet_pressure_pa: float = Field(..., gt=0)
    inlet_flow_m3s:    float = Field(..., gt=0)

class StepRequest(BaseModel):
    inlet_pressure_pa:         Optional[float] = None
    inlet_flow_m3s:            Optional[float] = None
    outlet_pressure_pa:        Optional[float] = None
    outlet_flow_m3s:           Optional[float] = None
    simulated_leak_node:       Optional[int]   = None
    simulated_leak_flow_m3s:   float = 0.0

class MultiStepRequest(BaseModel):
    n_steps:                   int   = Field(5, ge=1, le=100)
    inlet_pressure_pa:         Optional[float] = None
    inlet_flow_m3s:            Optional[float] = None
    outlet_pressure_pa:        Optional[float] = None
    outlet_flow_m3s:           Optional[float] = None
    simulated_leak_node:       Optional[int]   = None
    simulated_leak_flow_m3s:   float = 0.0

class AnalyzeRequest(BaseModel):
    measured_inlet_flow_m3s:   float
    measured_outlet_flow_m3s:  float
    measured_pressures_pa:     list[float]
    sensor_node_indices:       list[int]

class ThresholdRequest(BaseModel):
    sigma_flow_m3s:    float = Field(0.0003, gt=0)
    sigma_pressure_pa: float = Field(5000.0, gt=0)
    k_warn:            float = Field(2.0, gt=0)
    k_alarm:           float = Field(3.0, gt=0)
    k_critical:        float = Field(5.0, gt=0)
    vb_window_size:    int   = Field(10, ge=3, le=60)
    pd_window_size:    int   = Field(5,  ge=2, le=30)
    min_leak_pct:      float = Field(0.5, gt=0)

class RuntimeRequest(BaseModel):
    max_duration_s:   float = Field(3600.0, gt=0)
    step_interval_ms: int   = Field(1000, ge=100, le=10000)

class ProductSelectRequest(BaseModel):
    category: str
    name:     str


# ── Helpers ───────────────────────────────────────────────────────────────────
def _require():
    if _solver is None or _state is None:
        raise HTTPException(400, "Not initialized. Call /pipeline/configure then /simulation/initialize.")

def _save_history(result: dict):
    _history.append(result)
    if len(_history) > 120: _history.pop(0)

async def _broadcast(payload: dict):
    import json
    dead = []
    for ws in _ws_clients:
        try: await ws.send_text(json.dumps(payload))
        except: dead.append(ws)
    for ws in dead: _ws_clients.remove(ws)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["system"])
def health():
    return {"status":"ok","version":"2.1.0","configured":_solver is not None,
            "initialized":_state is not None,
            "sim_time_s":round(_state.t,3) if _state else None,
            "sim_step":_state.step if _state else None,
            "product":_config.product_name if _config else None,
            "runtime":_runtime,"timestamp":time.time()}

@app.get("/products/list", tags=["configuration"])
def products_list():
    return list_products()

@app.post("/products/select", tags=["configuration"])
def select_product(req: ProductSelectRequest):
    prod = get_product(req.category, req.name)
    if not prod: raise HTTPException(404, f"Product '{req.name}' not found")
    return {"name":prod.name,"category":prod.category,"density_kg_m3":prod.density,
            "viscosity_pa_s":prod.viscosity,"bulk_modulus_pa":prod.bulk_modulus,
            "specific_heat":prod.specific_heat,"thermal_expansion":prod.thermal_expansion,
            "z_factor":prod.z_factor,"molar_mass":prod.molar_mass,"gamma":prod.gamma,
            "fluid_type":"gas" if prod.category==ProductCategory.GAS_MIXTURE else "liquid"}

@app.post("/pipeline/configure", tags=["configuration"])
def configure(req: PipelineConfigRequest):
    global _solver, _state, _detector, _config
    try:
        ft = FluidType.GAS if req.fluid_type=="gas" else FluidType.LIQUID
        cfg = PipelineConfig(
            length=req.length_m, diameter=req.diameter_m,
            wall_thickness=req.wall_thickness_m, roughness=req.roughness_m,
            elevation_in=req.elevation_in_m, elevation_out=req.elevation_out_m,
            n_reaches=req.n_reaches, fluid_type=ft,
            density=req.density_kg_m3, viscosity=req.viscosity_pa_s,
            bulk_modulus=req.bulk_modulus_pa, young_modulus=req.young_modulus_pa,
            temperature_in=req.temperature_in_k, specific_heat=req.specific_heat,
            thermal_expansion=req.thermal_expansion, z_factor=req.z_factor,
            molar_mass=req.molar_mass_kg_mol, gamma=req.gamma,
            product_category=req.product_category, product_name=req.product_name,
        )
        _config  = cfg
        _solver  = MOCSolver(cfg)
        _state   = None
        _detector = LeakDetectionEngine(
            pipe_length=req.length_m, n_nodes=req.n_reaches+1,
            nominal_flow=req.nominal_flow_m3s,
            thresholds=DetectionThresholds(sigma_flow_m3s=req.sigma_flow_m3s,
                                           sigma_pressure_pa=req.sigma_pressure_pa))
        return {"status":"configured","product":req.product_name,
                "wave_speed_m_s":round(cfg.wave_speed,2),"dx_m":round(cfg.dx,2),
                "dt_s":round(cfg.dt,4),"n_nodes":cfg.n_reaches+1,"fluid_type":req.fluid_type}
    except Exception as e:
        raise HTTPException(422, str(e))

@app.post("/simulation/initialize", tags=["simulation"])
def initialize(req: InitRequest):
    global _state, _history
    if not _solver: raise HTTPException(400,"Configure pipeline first.")
    _state = _solver.steady_state(req.inlet_pressure_pa, req.inlet_flow_m3s)
    _history = []
    return {"status":"initialized","n_nodes":_state.n_nodes,
            "head_inlet_m":round(_state.H[0],2),"head_outlet_m":round(_state.H[-1],2),
            "flow_ls":round(_state.Q[0]*1000,2),
            "temp_inlet_c":round(_state.T_prof[0]-273.15,2),
            "density_inlet_kgm3":round(_state.rho_prof[0],2)}

@app.post("/simulation/step", tags=["simulation"])
async def sim_step(req: StepRequest):
    global _state
    _require()
    if _state.t >= _runtime["max_duration_s"]:
        return {"status":"max_duration_reached","sim_time_s":_state.t}
    _state = _solver.step(_state,
        P_in=req.inlet_pressure_pa, Q_in=req.inlet_flow_m3s,
        P_out=req.outlet_pressure_pa, Q_out=req.outlet_flow_m3s,
        leak_node=req.simulated_leak_node, leak_flow=req.simulated_leak_flow_m3s)
    result = _state.to_dict(_config)
    _save_history(result)
    if _ws_clients: asyncio.create_task(_broadcast(result))
    return result

@app.post("/simulation/multi_step", tags=["simulation"])
async def multi_step(req: MultiStepRequest):
    """Run N MOC steps in one API call — enables fast-forward simulation."""
    global _state
    _require()
    for _ in range(req.n_steps):
        if _state.t >= _runtime["max_duration_s"]: break
        _state = _solver.step(_state,
            P_in=req.inlet_pressure_pa, Q_in=req.inlet_flow_m3s,
            P_out=req.outlet_pressure_pa, Q_out=req.outlet_flow_m3s,
            leak_node=req.simulated_leak_node, leak_flow=req.simulated_leak_flow_m3s)
    result = _state.to_dict(_config)
    _save_history(result)
    if _ws_clients: asyncio.create_task(_broadcast(result))
    return result

@app.get("/simulation/state", tags=["simulation"])
def get_state():
    _require()
    return _state.to_dict(_config)

@app.get("/simulation/profiles", tags=["simulation"])
def get_profiles():
    _require()
    d = _state.to_dict(_config)
    return {**d, "pipeline_length_km":round(_config.length/1000,2),
            "product":_config.product_name,"sim_time_s":round(_state.t,2)}

@app.get("/simulation/history", tags=["simulation"])
def get_history(last_n: int = 60):
    return {"states":_history[-last_n:],"total":len(_history)}

@app.get("/simulation/equations", tags=["simulation"])
def get_equations(last_n: int = 100, equation: Optional[str] = None):
    log = list(EQUATION_LOG)[-last_n:]
    if equation: log = [e for e in log if equation.lower() in e["equation"].lower()]
    return {"equations": log, "total": len(EQUATION_LOG)}

@app.post("/leakdetection/analyze", tags=["leak_detection"])
def analyze(req: AnalyzeRequest):
    _require()
    if not _detector: raise HTTPException(400,"Detector not initialized.")
    n = _state.n_nodes
    P_sim  = _state.H * _config.density * 9.81
    P_meas = P_sim.copy()
    for idx, ni in enumerate(req.sensor_node_indices):
        if 0 <= ni < n and idx < len(req.measured_pressures_pa):
            P_meas[ni] = req.measured_pressures_pa[idx]
    return _detector.analyze(_state.t, req.measured_inlet_flow_m3s,
                             req.measured_outlet_flow_m3s, P_meas, P_sim,
                             req.measured_inlet_flow_m3s)

@app.get("/leakdetection/history", tags=["leak_detection"])
def alarm_history(last_n: int = 100):
    if not _detector: raise HTTPException(400,"Not initialized.")
    return {"alarms":_detector.get_alarm_history(last_n),"total":len(_detector._log)}

@app.post("/config/thresholds", tags=["configuration"])
def set_thresholds(req: ThresholdRequest):
    if not _detector: raise HTTPException(400,"Not initialized.")
    thr = DetectionThresholds(**req.model_dump())
    _detector.update_thresholds(thr)
    return {"status":"updated","vb_alarm_ls":round(thr.vb_alarm*1000,3),
            "pd_alarm_kpa":round(thr.pd_alarm/1000,2)}

@app.post("/config/runtime", tags=["configuration"])
def set_runtime(req: RuntimeRequest):
    global _runtime
    _runtime.update(req.model_dump())
    return {"status":"updated",**_runtime}

@app.get("/config/current", tags=["configuration"])
def current_config():
    if not _config: raise HTTPException(400,"Not configured.")
    return {"pipeline":{"length_km":round(_config.length/1000,2),"diameter_m":_config.diameter,
            "n_reaches":_config.n_reaches},"fluid":{"product":_config.product_name,
            "density":_config.density,"viscosity":_config.viscosity},
            "derived":{"wave_speed_m_s":round(_config.wave_speed,2),"dt_s":round(_config.dt,4),
            "dx_m":round(_config.dx,2)},"runtime":_runtime}

@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True: await asyncio.sleep(1)
    except WebSocketDisconnect:
        if ws in _ws_clients: _ws_clients.remove(ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
