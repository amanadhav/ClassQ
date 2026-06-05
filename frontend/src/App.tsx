import React, { useState, useEffect, useRef } from "react";
import { Activity, Database, Zap, Server } from "lucide-react";

// API + WebSocket targets are configurable at build time via Vite env vars
// (VITE_API_BASE / VITE_WS_URL). Defaults point at the production ALB; override
// to http://localhost:8000 / ws://localhost:8000/ws/metrics for local dev.
const PROD_ALB = "classq-prod-alb-1896872101.us-east-1.elb.amazonaws.com";
const API_BASE =
  import.meta.env.VITE_API_BASE ?? `http://${PROD_ALB}`;
const WS_URL =
  import.meta.env.VITE_WS_URL ?? `ws://${PROD_ALB}/ws/metrics`;

interface MetricCardProps {
  label: string;
  value: string | number;
  unit?: string;
  icon: React.ReactNode;
  subtitle?: string;
}

const MetricCard: React.FC<MetricCardProps> = ({
  label,
  value,
  unit = "",
  icon,
  subtitle,
}) => {
  return (
    <div className="border border-white/15 bg-black p-8 transition-colors duration-200 hover:border-white/40">
      <div className="flex flex-col space-y-6">
        <div className="flex items-start justify-between">
          <span className="font-mono text-[10px] font-medium uppercase tracking-[0.2em] text-white/50">
            {label}
          </span>
          <div className="text-white/40">{icon}</div>
        </div>
        <div className="space-y-2">
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-5xl font-light tracking-tight text-white tabular-nums">
              {value}
            </span>
            {unit && (
              <span className="font-mono text-lg text-white/40">{unit}</span>
            )}
          </div>
          {subtitle && (
            <div className="font-mono text-[10px] uppercase tracking-[0.15em] text-white/30">
              {subtitle}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

interface MetricsPayload {
  active_connections: number;
  queue_depth: number;
  allocations_per_sec: number;
  timestamp: string;
}

type ConnState = "connecting" | "open" | "closed";

interface LogEntry {
  id: number;
  clock: string;
  message: string;
}

// Default chaos target: the seeded Systems Programming section (no prerequisites),
// so the burst genuinely contends for seats and visualizes overselling pressure.
const CHAOS_SECTION_ID = "0c000000-0000-0000-0000-000000000002";

const OperatorDashboard: React.FC = () => {
  const [activeConnections, setActiveConnections] = useState<number | null>(null);
  const [queueDepth, setQueueDepth] = useState<number | null>(null);
  const [allocationsPerSec, setAllocationsPerSec] = useState<number | null>(null);
  const [lastTimestamp, setLastTimestamp] = useState<string | null>(null);
  const [connState, setConnState] = useState<ConnState>("connecting");
  const [tickCount, setTickCount] = useState(0);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [chaosBusy, setChaosBusy] = useState(false);
  const [chaosMsg, setChaosMsg] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const logIdRef = useRef(0);

  useEffect(() => {
    let stopped = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      setConnState("connecting");
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        if (!stopped) setConnState("open");
      };

      ws.onmessage = (event) => {
        try {
          const data: MetricsPayload = JSON.parse(event.data);
          setActiveConnections(data.active_connections);
          setQueueDepth(data.queue_depth);
          setAllocationsPerSec(data.allocations_per_sec);
          setLastTimestamp(data.timestamp);
          setTickCount((c) => c + 1);

          const clock =
            new Date(data.timestamp).toLocaleTimeString(undefined, {
              hour12: false,
            }) +
            "." +
            String(new Date(data.timestamp).getMilliseconds()).padStart(3, "0");
          setLogs((prev) => {
            const entry: LogEntry = {
              id: logIdRef.current++,
              clock,
              message: `tick · conns=${data.active_connections} · queue=${data.queue_depth} · alloc/s=${data.allocations_per_sec}`,
            };
            return [entry, ...prev].slice(0, 8);
          });
        } catch {
          // ignore malformed frames
        }
      };

      ws.onclose = () => {
        if (stopped) return;
        setConnState("closed");
        reconnectTimer = setTimeout(connect, 1000);
      };

      ws.onerror = () => {
        ws.close();
      };
    };

    connect();

    return () => {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      wsRef.current?.close();
    };
  }, []);

  const triggerChaos = async () => {
    setChaosBusy(true);
    setChaosMsg(null);
    try {
      const res = await fetch(`${API_BASE}/chaos/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ volume: 500, section_id: CHAOS_SECTION_ID }),
      });
      if (res.status === 409) {
        setChaosMsg("simulation already active");
      } else if (!res.ok) {
        setChaosMsg(`error ${res.status}`);
      } else {
        const data = await res.json();
        setChaosMsg(`burst launched · volume ${data.volume}`);
      }
    } catch {
      setChaosMsg("request failed — backend offline?");
    } finally {
      setTimeout(() => setChaosBusy(false), 1200);
    }
  };

  const systemOnline = connState === "open";
  const statusValue = systemOnline
    ? "ONLINE"
    : connState === "connecting"
    ? "LINKING"
    : "OFFLINE";

  const tickClock = lastTimestamp
    ? new Date(lastTimestamp).toLocaleTimeString(undefined, { hour12: false }) +
      "." +
      String(new Date(lastTimestamp).getMilliseconds()).padStart(3, "0")
    : "--:--:--";

  return (
    <div className="min-h-screen bg-black p-8 md:p-12 lg:p-16">
      <div className="mx-auto max-w-7xl space-y-12">
        {/* Header */}
        <header className="flex items-center justify-between border-b border-white/15 pb-6">
          <div className="space-y-2">
            <h1 className="font-mono text-sm font-medium uppercase tracking-[0.3em] text-white">
              ClassQ · Operator Dashboard
            </h1>
            <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-white/40">
              Real-time system monitoring
            </p>
          </div>
          <div className="flex items-center gap-6">
            <div className="flex flex-col items-end gap-1">
              <button
                onClick={triggerChaos}
                disabled={chaosBusy}
                className="border border-red-500/70 bg-red-600/90 px-5 py-2.5 font-mono text-[10px] font-semibold uppercase tracking-[0.2em] text-white transition-colors duration-150 hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {chaosBusy ? "● Firing…" : "▲ Trigger Chaos Burst"}
              </button>
              {chaosMsg && (
                <span className="font-mono text-[9px] uppercase tracking-[0.15em] text-red-400/80">
                  {chaosMsg}
                </span>
              )}
            </div>
            <div className="flex items-center gap-3 font-mono text-[10px] uppercase tracking-[0.2em] text-white/50">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  systemOnline
                    ? "animate-pulse bg-white"
                    : connState === "connecting"
                    ? "animate-pulse bg-white/50"
                    : "bg-white/20"
                }`}
              />
              <span>
                {systemOnline
                  ? "stream connected"
                  : connState === "connecting"
                  ? "linking…"
                  : "offline · retrying"}
              </span>
            </div>
          </div>
        </header>

        {/* Metric grid */}
        <div className="grid grid-cols-1 gap-px bg-white/10 md:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label="Active Connections"
            value={activeConnections ?? "—"}
            icon={<Activity size={18} strokeWidth={1} />}
            subtitle={`${tickCount} ticks received`}
          />
          <MetricCard
            label="Queue Depth"
            value={queueDepth ?? "—"}
            icon={<Database size={18} strokeWidth={1} />}
            subtitle="redis registration queues"
          />
          <MetricCard
            label="Allocations"
            value={allocationsPerSec ?? "—"}
            unit="/sec"
            icon={<Zap size={18} strokeWidth={1} />}
            subtitle="confirmed · last 1s window"
          />
          <MetricCard
            label="System Status"
            value={statusValue}
            icon={<Server size={18} strokeWidth={1} />}
            subtitle={`last tick ${tickClock}`}
          />
        </div>

        {/* Metrics stream log */}
        <div className="border border-white/15 bg-black p-8">
          <div className="mb-6 flex items-center justify-between">
            <h2 className="font-mono text-[10px] font-medium uppercase tracking-[0.2em] text-white/50">
              Metrics Stream
            </h2>
            <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.2em] text-white/40">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  systemOnline ? "animate-pulse bg-white" : "bg-white/20"
                }`}
              />
              <span>{systemOnline ? "live · 500ms" : "disconnected"}</span>
            </div>
          </div>
          <div className="space-y-3 font-mono text-xs">
            {logs.length === 0 ? (
              <div className="py-8 text-center text-white/30">
                awaiting stream…
              </div>
            ) : (
              logs.map((log) => (
                <div
                  key={log.id}
                  className="flex items-start space-x-4 border-b border-white/10 pb-3 last:border-0"
                >
                  <span className="whitespace-nowrap text-white/40">
                    {log.clock}
                  </span>
                  <span className="flex-1 text-white/80">{log.message}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default OperatorDashboard;
