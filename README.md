# RTTM Engine — Integration Guide

**Real-Time Transient Model for Pipeline Monitoring & Leak Detection**
Built by INTECH Process Automation | API 1130 / API 1149 aligned

---

## Architecture

```
  ┌─────────────────────────────────────────────────────────────┐
  │                     NGINX (port 80/443)                     │
  └──────────────┬──────────────────────────────┬──────────────┘
                 │                              │
  ┌──────────────▼───────────┐   ┌─────────────▼──────────────┐
  │   LDS Web App (port 3000)│   │ RTTM Engine (port 8000)    │
  │                          │   │                             │
  │  React/Next.js frontend  │◄──►  FastAPI + MOC Solver      │
  │  RTTMDashboard.tsx       │WS │  LeakDetectionEngine       │
  │  rttm-client.ts          │   │  Volume Balance Detector   │
  └──────────────────────────┘   │  Pressure Deviation Det.   │
                                 └──────────────┬─────────────┘
  ┌──────────────────────────┐                  │
  │  SCADA / OPC-UA / MQTT   │──────────────────┘
  │  (pressure, flow, temp)  │    Real-time boundary conditions
  └──────────────────────────┘
                    │
  ┌─────────────────▼────────┐
  │  Redis (pub/sub + cache) │
  └──────────────────────────┘
```

---

## Quick Start

### 1. Copy files into your project

```
your-lds-app/
├── backend/
│   └── rttm_engine/          ← copy this folder here
│       ├── api/main.py
│       ├── core/solver.py
│       ├── detection/leak_detector.py
│       ├── requirements.txt
│       └── Dockerfile
├── frontend/src/
│   └── integration/          ← copy these two files
│       ├── rttm-client.ts
│       └── RTTMDashboard.tsx
└── docker-compose.yml        ← merge with your existing compose
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your pipeline parameters
```

### 3. Launch

```bash
docker-compose up -d
```

### 4. Verify

```bash
curl http://localhost:8000/health
# Expected: {"status":"ok","engine":"rttm-moc-v1","configured":false,...}
```

---

## API Quick Reference

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Service health |
| POST | `/pipeline/configure` | Set pipe geometry + fluid |
| POST | `/simulation/initialize` | Set steady-state IC |
| POST | `/simulation/step` | Advance one MOC step |
| GET | `/simulation/state` | Current H, Q, P arrays |
| POST | `/leakdetection/analyze` | Run VB + PD detection |
| GET | `/leakdetection/history` | Alarm log |
| WS | `/ws/stream` | Real-time state push |

Full interactive docs at: **http://localhost:8000/docs**

---

## Frontend Integration (React)

```tsx
// In your LDS dashboard page:
import { RTTMDashboard } from './integration/RTTMDashboard';

function LDSDashboard() {
  return (
    <div>
      {/* Your existing LDS components */}
      <PipelineMap />
      <AlarmPanel />

      {/* Add RTTM monitoring */}
      <RTTMDashboard
        serviceUrl={process.env.REACT_APP_RTTM_URL}
        pipelineConfig={{
          length_m: 50000,
          diameter_m: 0.3048,
          wall_thickness_m: 0.008,
          fluid_type: "liquid",
          density_kg_m3: 850,
          nominal_flow_m3s: 0.15,
        }}
        initConditions={{
          inlet_pressure_pa: 5_000_000,
          inlet_flow_m3s: 0.15,
        }}
        sensorNodes={[0, 5, 10, 15, 20]}
        onCriticalAlarm={(alarm) => {
          // Connect to your existing LDS alarm system
          yourAlarmSystem.raise(alarm);
        }}
      />
    </div>
  );
}
```

---

## Integration Checklist

### Infrastructure
- [ ] RTTM service container running and healthy (`/health` returns 200)
- [ ] Redis running and reachable from both services
- [ ] Nginx routing: `/api/rttm/*` → `rttm-engine:8000`
- [ ] Environment variables set in `.env`

### Backend
- [ ] `POST /pipeline/configure` called once at startup with correct pipe geometry
- [ ] `POST /simulation/initialize` called after configure
- [ ] SCADA adapter posting boundary conditions to `POST /simulation/step` every scan cycle
- [ ] Leak detection scheduled (recommend every 5–10 SCADA scans)

### Frontend
- [ ] `RTTMClient` instantiated with correct `serviceUrl`
- [ ] `RTTMDashboard` mounted in LDS dashboard
- [ ] `onCriticalAlarm` callback wired to LDS alarm bus
- [ ] WebSocket URL in environment: `REACT_APP_RTTM_WS_URL=ws://...`

### SCADA Integration
- [ ] Flow meters (inlet + outlet) feeding `measured_inlet_flow_m3s` / `measured_outlet_flow_m3s`
- [ ] Pressure transmitters mapped to `sensor_node_indices` in `POST /leakdetection/analyze`
- [ ] Instrument uncertainties (`sigma_flow_m3s`, `sigma_pressure_pa`) tuned to actual meters

### Commissioning
- [ ] Steady-state validation: RTTM pressure profile matches field measurements ± 2%
- [ ] Volume balance baseline established over 24h (no leak)
- [ ] Alarm thresholds tuned to < 1% false alarm rate
- [ ] Leak simulation test performed (inject synthetic leak via `simulated_leak_node`)
- [ ] End-to-end alarm: RTTM alarm → LDS alert → operator notification

---

## Environment Variables (.env.example)

```env
# RTTM Service
RTTM_PORT=8000
RTTM_LOG_LEVEL=info

# LDS App
LDS_PORT=3000
LDS_APP_IMAGE=your-lds-app:latest
LDS_DB_URL=postgresql://user:pass@db:5432/lds
LDS_SECRET_KEY=change-me-in-production

# Redis
REDIS_URL=redis://redis:6379/0

# SSL (optional)
SSL_CERT_PATH=./ssl

# Frontend env (set in your LDS app build)
REACT_APP_RTTM_URL=http://localhost:8000
REACT_APP_RTTM_WS_URL=ws://localhost:8000/ws/stream
```

---

## Governing Equations Reference

| Equation | Formula |
|---|---|
| Continuity | `∂H/∂t + (a²/g)·(∂V/∂x) = 0` |
| Momentum | `∂V/∂t + g·(∂H/∂x) + f·V\|V\|/(2D) = 0` |
| Wave speed | `a = √(K/ρ) / √(1 + KD/Ee)` |
| MOC C+ | `Cp = Hₐ + Qₐ·Bp − R·Qₐ\|Qₐ\|` |
| MOC C− | `Cm = H_b − Q_b·Bp + R·Q_b\|Q_b\|` |
| Volume balance | `Imbalance = Q_in − Q_out − dV/dt` |
| VB threshold | `3·√(σ_in² + σ_out²)` |
| Pressure residual | `R(x,t) = P_meas − P_sim` |

---

## References

1. Wylie & Streeter — *Fluid Transients in Systems* (1993)
2. API RP 1130 — *Computational Pipeline Monitoring for Liquids* (2022 ed.)
3. API TR 1149 — *Pipeline Variable Uncertainties and Leak Detectability*
4. Chaudhry — *Applied Hydraulic Transients* (2014)
5. TSNet — Open-source MOC Python package: https://tsnet.readthedocs.io
6. Academia.edu — *RTTM Based Gas Pipeline Leak Detection: A Tutorial* (2015)
