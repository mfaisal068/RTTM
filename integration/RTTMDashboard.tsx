/**
 * RTTMDashboard.tsx
 * 
 * Drop-in React component that adds RTTM monitoring to your existing LDS dashboard.
 * Requires: React 18+, recharts (for pressure profile chart), Tailwind CSS or
 * any CSS-in-JS (styled-components / emotion).
 * 
 * Usage in your LDS App:
 *   import { RTTMDashboard } from './integration/RTTMDashboard';
 *
 *   <RTTMDashboard
 *     serviceUrl="http://rttm-service:8000"
 *     pipelineConfig={{ length_m: 50000, diameter_m: 0.3048, ... }}
 *     initConditions={{ inlet_pressure_pa: 5e6, inlet_flow_m3s: 0.15 }}
 *   />
 */

import React, {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
} from "react";

import {
  RTTMClient,
  PipelineConfigRequest,
  InitializeRequest,
  SimulationState,
  LeakAlarm,
  AnalysisResult,
} from "./rttm-client";

// ─── State management ─────────────────────────────────────────────────────────

interface RTTMState {
  connected: boolean;
  initialized: boolean;
  simState: SimulationState | null;
  lastAnalysis: AnalysisResult | null;
  alarmHistory: LeakAlarm[];
  error: string | null;
  stepCount: number;
}

type RTTMAction =
  | { type: "SET_CONNECTED"; payload: boolean }
  | { type: "SET_INITIALIZED" }
  | { type: "UPDATE_STATE"; payload: SimulationState }
  | { type: "UPDATE_ANALYSIS"; payload: AnalysisResult }
  | { type: "ADD_ALARM"; payload: LeakAlarm }
  | { type: "SET_ERROR"; payload: string }
  | { type: "CLEAR_ERROR" };

function rttmReducer(state: RTTMState, action: RTTMAction): RTTMState {
  switch (action.type) {
    case "SET_CONNECTED":
      return { ...state, connected: action.payload };
    case "SET_INITIALIZED":
      return { ...state, initialized: true, error: null };
    case "UPDATE_STATE":
      return { ...state, simState: action.payload, stepCount: state.stepCount + 1 };
    case "UPDATE_ANALYSIS":
      return { ...state, lastAnalysis: action.payload };
    case "ADD_ALARM":
      return {
        ...state,
        alarmHistory: [action.payload, ...state.alarmHistory].slice(0, 200),
      };
    case "SET_ERROR":
      return { ...state, error: action.payload };
    case "CLEAR_ERROR":
      return { ...state, error: null };
    default:
      return state;
  }
}

const initialState: RTTMState = {
  connected: false,
  initialized: false,
  simState: null,
  lastAnalysis: null,
  alarmHistory: [],
  error: null,
  stepCount: 0,
};

// ─── Props ────────────────────────────────────────────────────────────────────

interface RTTMDashboardProps {
  serviceUrl?: string;
  pipelineConfig: PipelineConfigRequest;
  initConditions: InitializeRequest;
  /** SCADA polling interval in ms (default: 2000) */
  pollIntervalMs?: number;
  /** Optional: sensor node indices where real pressure measurements exist */
  sensorNodes?: number[];
  /** Called when a critical alarm fires — integrate with your LDS alert system */
  onCriticalAlarm?: (alarm: LeakAlarm) => void;
  className?: string;
}

// ─── Component ────────────────────────────────────────────────────────────────

export const RTTMDashboard: React.FC<RTTMDashboardProps> = ({
  serviceUrl = "http://localhost:8000",
  pipelineConfig,
  initConditions,
  pollIntervalMs = 2000,
  sensorNodes = [0, 5, 10, 15, 20],
  onCriticalAlarm,
  className = "",
}) => {
  const [state, dispatch] = useReducer(rttmReducer, initialState);
  const clientRef = useRef<RTTMClient | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [inletFlow, setInletFlow] = useState(initConditions.inlet_flow_m3s);
  const [outletFlow, setOutletFlow] = useState(initConditions.inlet_flow_m3s);

  // ── Initialise client ──────────────────────────────────────────────────────
  const initRTTM = useCallback(async () => {
    const client = new RTTMClient(serviceUrl);
    clientRef.current = client;

    try {
      await client.health();
      dispatch({ type: "SET_CONNECTED", payload: true });

      await client.configure(pipelineConfig);
      await client.initialize(initConditions);
      dispatch({ type: "SET_INITIALIZED" });

      // Connect WebSocket stream
      client.connectStream((newState) => {
        dispatch({ type: "UPDATE_STATE", payload: newState });
      });
    } catch (err) {
      dispatch({ type: "SET_ERROR", payload: String(err) });
    }
  }, [serviceUrl, pipelineConfig, initConditions]);

  // ── Polling loop: advance MOC + run leak detection ─────────────────────────
  const poll = useCallback(async () => {
    const client = clientRef.current;
    if (!client || !state.initialized) return;

    try {
      // Advance one MOC step using current boundary conditions
      await client.step({
        inlet_pressure_pa: initConditions.inlet_pressure_pa,
        outlet_pressure_pa: initConditions.inlet_pressure_pa * 0.6, // default 60% back-pressure
      });

      // Read updated state
      const newState = await client.getState();
      dispatch({ type: "UPDATE_STATE", payload: newState });

      // Run leak detection
      const pressures = sensorNodes.map(
        (i) => (newState.pressures_pa ?? newState.pressure_kPa.map((p) => p * 1000))[i] ?? 0
      );

      const analysis = await client.analyze({
        measured_inlet_flow_m3s: inletFlow,
        measured_outlet_flow_m3s: outletFlow,
        measured_pressures_pa: pressures,
        sensor_node_indices: sensorNodes,
      });

      dispatch({ type: "UPDATE_ANALYSIS", payload: analysis });

      // Bubble up critical alarms
      if (analysis.any_alarm) {
        analysis.alarms.forEach((alarm) => {
          dispatch({ type: "ADD_ALARM", payload: alarm });
          if (alarm.severity === "critical") {
            onCriticalAlarm?.(alarm);
          }
        });
      }
    } catch (err) {
      console.warn("RTTM poll error:", err);
    }
  }, [
    state.initialized,
    initConditions,
    inletFlow,
    outletFlow,
    sensorNodes,
    onCriticalAlarm,
  ]);

  // ── Mount / unmount ────────────────────────────────────────────────────────
  useEffect(() => {
    initRTTM();
    return () => {
      clientRef.current?.disconnectStream();
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [initRTTM]);

  useEffect(() => {
    if (state.initialized) {
      pollRef.current = setInterval(poll, pollIntervalMs);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [state.initialized, poll, pollIntervalMs]);

  // ── Helpers ────────────────────────────────────────────────────────────────
  const severityColor = (s?: string) => {
    switch (s) {
      case "critical": return "#ef4444";
      case "alarm":    return "#f97316";
      case "warning":  return "#eab308";
      default:         return "#22c55e";
    }
  };

  const { simState, lastAnalysis, alarmHistory, connected, initialized, error } = state;

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div className={`rttm-dashboard ${className}`} style={{ fontFamily: "system-ui, sans-serif" }}>

      {/* ── Status bar ── */}
      <div style={{
        display: "flex", alignItems: "center", gap: "12px",
        padding: "8px 16px", background: "#1e293b", borderRadius: "8px",
        color: "#f1f5f9", fontSize: "13px", marginBottom: "12px",
      }}>
        <span style={{
          width: 8, height: 8, borderRadius: "50%",
          background: connected ? "#22c55e" : "#ef4444", display: "inline-block",
        }} />
        <strong>RTTM Engine</strong>
        <span>{connected ? "Connected" : "Disconnected"}</span>
        {initialized && simState && (
          <>
            <span style={{ marginLeft: "auto" }}>t = {simState.time.toFixed(1)} s</span>
            <span>Step {simState.step}</span>
          </>
        )}
        {lastAnalysis?.any_alarm && (
          <span style={{
            padding: "2px 10px", borderRadius: "4px",
            background: severityColor(lastAnalysis.highest_severity),
            color: "#fff", fontWeight: 700, letterSpacing: "0.05em",
          }}>
            ⚠ {lastAnalysis.highest_severity?.toUpperCase()}
          </span>
        )}
      </div>

      {error && (
        <div style={{ color: "#ef4444", padding: "8px 12px", background: "#fef2f2",
          borderRadius: "6px", marginBottom: "12px", fontSize: "13px" }}>
          Error: {error}
        </div>
      )}

      {/* ── Main grid ── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>

        {/* Pressure profile */}
        <div style={{ background: "#f8fafc", border: "1px solid #e2e8f0",
          borderRadius: "8px", padding: "14px" }}>
          <h3 style={{ margin: "0 0 10px", fontSize: "14px", fontWeight: 600 }}>
            Pressure Profile
          </h3>
          {simState ? (
            <div>
              {simState.pressure_kPa.map((p, i) => (
                <div key={i} style={{ display: "flex", alignItems: "center",
                  gap: "8px", marginBottom: "3px", fontSize: "12px" }}>
                  <span style={{ width: "60px", color: "#64748b" }}>Node {i}</span>
                  <div style={{
                    flex: 1, height: "14px", background: "#e2e8f0", borderRadius: "3px", overflow: "hidden",
                  }}>
                    <div style={{
                      width: `${Math.max(0, Math.min(100, p / 7000 * 100))}%`,
                      height: "100%",
                      background: lastAnalysis?.alarms.some(
                        (a) => Math.abs((a.location_m ?? -1) - i * (pipelineConfig.length_m / 20)) < 2600
                      ) ? "#ef4444" : "#3b82f6",
                      transition: "width 0.3s ease",
                    }} />
                  </div>
                  <span style={{ width: "70px", textAlign: "right", color: "#334155" }}>
                    {p.toFixed(0)} kPa
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p style={{ color: "#94a3b8", fontSize: "13px" }}>
              {initialized ? "Waiting for data…" : "Not initialized"}
            </p>
          )}
        </div>

        {/* Leak detection status */}
        <div style={{ background: "#f8fafc", border: "1px solid #e2e8f0",
          borderRadius: "8px", padding: "14px" }}>
          <h3 style={{ margin: "0 0 10px", fontSize: "14px", fontWeight: 600 }}>
            Leak Detection Status
          </h3>
          {lastAnalysis ? (
            <div>
              <div style={{
                padding: "10px 14px", borderRadius: "6px", marginBottom: "10px",
                background: lastAnalysis.any_alarm
                  ? severityColor(lastAnalysis.highest_severity) + "22"
                  : "#dcfce7",
                border: `1px solid ${lastAnalysis.any_alarm
                  ? severityColor(lastAnalysis.highest_severity)
                  : "#86efac"}`,
              }}>
                <div style={{ fontWeight: 700, fontSize: "15px",
                  color: lastAnalysis.any_alarm
                    ? severityColor(lastAnalysis.highest_severity)
                    : "#16a34a" }}>
                  {lastAnalysis.any_alarm
                    ? `⚠ ${lastAnalysis.highest_severity?.toUpperCase()} — Confidence ${(lastAnalysis.fused_confidence * 100).toFixed(0)}%`
                    : "✓ No Leak Detected"}
                </div>
              </div>
              {lastAnalysis.alarms.map((alarm, i) => (
                <div key={i} style={{ fontSize: "12px", padding: "6px 10px",
                  background: "#fff", border: "1px solid #e2e8f0", borderRadius: "5px",
                  marginBottom: "6px" }}>
                  <strong style={{ color: severityColor(alarm.severity) }}>
                    [{alarm.method}]
                  </strong>{" "}
                  {alarm.message}
                  {alarm.location_m && (
                    <div style={{ color: "#64748b", marginTop: "2px" }}>
                      📍 Est. location: {(alarm.location_m / 1000).toFixed(2)} km
                    </div>
                  )}
                </div>
              ))}
              <div style={{ fontSize: "11px", color: "#94a3b8", marginTop: "6px" }}>
                Session alarms: {lastAnalysis.alarm_count_session}
              </div>
            </div>
          ) : (
            <p style={{ color: "#94a3b8", fontSize: "13px" }}>Awaiting first analysis…</p>
          )}
        </div>
      </div>

      {/* SCADA overrides for testing */}
      <div style={{ background: "#fffbeb", border: "1px solid #fde68a",
        borderRadius: "8px", padding: "12px", marginTop: "12px" }}>
        <h3 style={{ margin: "0 0 8px", fontSize: "13px", fontWeight: 600, color: "#92400e" }}>
          SCADA Input Override (Testing)
        </h3>
        <div style={{ display: "flex", gap: "16px", fontSize: "13px" }}>
          <label style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            Inlet flow (L/s):
            <input type="number" step="0.5"
              value={(inletFlow * 1000).toFixed(1)}
              onChange={(e) => setInletFlow(Number(e.target.value) / 1000)}
              style={{ width: "80px", padding: "3px 6px", border: "1px solid #d1d5db",
                borderRadius: "4px", fontSize: "13px" }}
            />
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            Outlet flow (L/s):
            <input type="number" step="0.5"
              value={(outletFlow * 1000).toFixed(1)}
              onChange={(e) => setOutletFlow(Number(e.target.value) / 1000)}
              style={{ width: "80px", padding: "3px 6px", border: "1px solid #d1d5db",
                borderRadius: "4px", fontSize: "13px" }}
            />
          </label>
          <span style={{ color: "#78716c", fontSize: "12px", alignSelf: "center" }}>
            Imbalance: {((inletFlow - outletFlow) * 1000).toFixed(2)} L/s
          </span>
        </div>
      </div>

      {/* Alarm history */}
      {alarmHistory.length > 0 && (
        <div style={{ marginTop: "12px" }}>
          <h3 style={{ margin: "0 0 8px", fontSize: "13px", fontWeight: 600 }}>
            Alarm Log ({alarmHistory.length})
          </h3>
          <div style={{ maxHeight: "160px", overflowY: "auto",
            border: "1px solid #e2e8f0", borderRadius: "6px" }}>
            {alarmHistory.slice(0, 20).map((alarm, i) => (
              <div key={i} style={{
                padding: "6px 12px", borderBottom: "1px solid #f1f5f9",
                fontSize: "12px", display: "flex", gap: "10px", alignItems: "center",
              }}>
                <span style={{
                  padding: "1px 7px", borderRadius: "3px", fontWeight: 600,
                  background: severityColor(alarm.severity) + "20",
                  color: severityColor(alarm.severity), whiteSpace: "nowrap",
                }}>
                  {alarm.severity}
                </span>
                <span style={{ color: "#64748b", whiteSpace: "nowrap" }}>
                  t={alarm.timestamp.toFixed(0)}s
                </span>
                <span style={{ color: "#334155" }}>{alarm.message}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export default RTTMDashboard;
