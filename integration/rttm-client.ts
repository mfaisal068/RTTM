/**
 * RTTM API Client — TypeScript
 * Drop into your existing LDS web app (React/Next.js/Vue).
 *
 * Usage:
 *   const rttm = new RTTMClient('http://localhost:8000');
 *   await rttm.configure({ ... });
 *   await rttm.initialize({ ... });
 *   rttm.connectStream(onStateUpdate, onAlarm);
 */

// ─── Types ────────────────────────────────────────────────────────────────────

export interface PipelineConfigRequest {
  length_m: number;
  diameter_m: number;
  wall_thickness_m: number;
  roughness_m?: number;
  elevation_in_m?: number;
  elevation_out_m?: number;
  n_reaches?: number;
  fluid_type?: "liquid" | "gas";
  density_kg_m3?: number;
  viscosity_pa_s?: number;
  bulk_modulus_pa?: number;
  temperature_k?: number;
  z_factor?: number;
  nominal_flow_m3s?: number;
  sigma_flow_m3s?: number;
  sigma_pressure_pa?: number;
}

export interface InitializeRequest {
  inlet_pressure_pa: number;
  inlet_flow_m3s: number;
}

export interface StepRequest {
  inlet_pressure_pa?: number;
  inlet_flow_m3s?: number;
  outlet_pressure_pa?: number;
  outlet_flow_m3s?: number;
  simulated_leak_node?: number;
  simulated_leak_flow_m3s?: number;
}

export interface AnalyzeRequest {
  measured_inlet_flow_m3s: number;
  measured_outlet_flow_m3s: number;
  measured_pressures_pa: number[];
  sensor_node_indices: number[];
}

export interface SimulationState {
  time: number;
  step: number;
  head_m: number[];
  flow_m3s: number[];
  pressure_kPa: number[];
  pressures_pa?: number[];
}

export interface LeakAlarm {
  method: string;
  severity: "warning" | "alarm" | "critical";
  timestamp: number;
  imbalance_m3s: number;
  imbalance_pct: number;
  location_m: number | null;
  estimated_leak_m3s: number | null;
  confidence: number;
  message: string;
}

export interface AnalysisResult {
  t: number;
  any_alarm: boolean;
  highest_severity: "none" | "warning" | "alarm" | "critical";
  fused_confidence: number;
  alarms: LeakAlarm[];
  alarm_count_session: number;
}

export interface HealthStatus {
  status: string;
  engine: string;
  configured: boolean;
  initialized: boolean;
  sim_time_s: number | null;
  sim_step: number | null;
}

// ─── Client ───────────────────────────────────────────────────────────────────

export class RTTMClient {
  private baseUrl: string;
  private ws: WebSocket | null = null;
  private reconnectDelay = 2000;
  private _onState?: (state: SimulationState) => void;
  private _onAlarm?: (alarm: LeakAlarm) => void;

  constructor(baseUrl: string = "http://localhost:8000") {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  // ── HTTP helpers ──────────────────────────────────────────────────────────

  private async _get<T>(path: string): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(`RTTM API [${res.status}]: ${err.detail}`);
    }
    return res.json() as Promise<T>;
  }

  private async _post<T>(path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(`RTTM API [${res.status}]: ${err.detail}`);
    }
    return res.json() as Promise<T>;
  }

  // ── Public API ────────────────────────────────────────────────────────────

  /** Check RTTM service availability. */
  async health(): Promise<HealthStatus> {
    return this._get<HealthStatus>("/health");
  }

  /** Configure pipeline geometry and fluid properties. */
  async configure(req: PipelineConfigRequest): Promise<Record<string, unknown>> {
    return this._post("/pipeline/configure", req);
  }

  /** Initialise MOC grid from steady-state conditions. */
  async initialize(req: InitializeRequest): Promise<Record<string, unknown>> {
    return this._post("/simulation/initialize", req);
  }

  /** Advance the MOC solver by one time step. */
  async step(req: StepRequest): Promise<SimulationState> {
    return this._post<SimulationState>("/simulation/step", req);
  }

  /** Get current RTTM simulation state. */
  async getState(): Promise<SimulationState> {
    return this._get<SimulationState>("/simulation/state");
  }

  /** Run leak detection against current SCADA measurements. */
  async analyze(req: AnalyzeRequest): Promise<AnalysisResult> {
    return this._post<AnalysisResult>("/leakdetection/analyze", req);
  }

  /** Retrieve alarm history (last N entries). */
  async alarmHistory(lastN = 50): Promise<{ alarms: LeakAlarm[]; total_session_alarms: number }> {
    return this._get(`/leakdetection/history?last_n=${lastN}`);
  }

  // ── WebSocket streaming ──────────────────────────────────────────────────

  /**
   * Connect to the RTTM real-time stream.
   * Automatically reconnects on disconnect.
   *
   * @param onState  Called every time a new simulation state arrives
   * @param onAlarm  Called when an alarm is contained in the state
   */
  connectStream(
    onState: (state: SimulationState) => void,
    onAlarm?: (alarm: LeakAlarm) => void
  ): void {
    this._onState = onState;
    this._onAlarm = onAlarm;
    this._openWS();
  }

  disconnectStream(): void {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  private _openWS(): void {
    const wsUrl = this.baseUrl.replace(/^http/, "ws") + "/ws/stream";
    this.ws = new WebSocket(wsUrl);

    this.ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data) as SimulationState;
        this._onState?.(data);
      } catch {
        console.warn("RTTM WS: could not parse message", evt.data);
      }
    };

    this.ws.onclose = () => {
      console.warn("RTTM WS: disconnected. Reconnecting...");
      setTimeout(() => this._openWS(), this.reconnectDelay);
    };

    this.ws.onerror = (e) => {
      console.error("RTTM WS error:", e);
    };
  }
}

// ─── React Hook ───────────────────────────────────────────────────────────────
// Optional: drop this into your LDS React app.

/**
 * useRTTM — React hook for live RTTM integration.
 *
 * Example:
 *   const { state, alarms, isConnected } = useRTTM('http://rttm-service:8000');
 */
export function useRTTM(
  serviceUrl: string,
  config: PipelineConfigRequest,
  initConditions: InitializeRequest,
  pollingIntervalMs = 2000
) {
  // This hook lives in a .tsx file in your LDS app.
  // Paste this into your src/hooks/useRTTM.ts and import React there.
  // The implementation below is framework-agnostic pseudocode.
  throw new Error(
    "Import this hook into a .tsx file with React imported. " +
    "See integration/RTTMDashboard.tsx for a complete React example."
  );
}
