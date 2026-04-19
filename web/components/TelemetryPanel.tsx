"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";

interface GpuMetrics {
  gpu: string;
  vram_used_mb?: number;
  vram_total_mb?: number;
  vram_pct?: number;
  overspill: "green" | "amber" | "red" | "unknown" | "unavailable";
}

interface Metrics {
  gpu: GpuMetrics;
  ping_ms: number | null;
  ts: number;
}

const overspillColor = {
  green:       "bg-emerald-500/80",
  amber:       "bg-amber-500/80",
  red:         "bg-red-500/80",
  unknown:     "bg-neutral-600/80",
  unavailable: "bg-neutral-700/80",
};

export function TelemetryPanel({ tokensPerSec }: { tokensPerSec: number | null }) {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [open, setOpen] = useState(true);

  useEffect(() => {
    const es = new EventSource("/api/telemetry");
    es.addEventListener("metrics", (e) => {
      try { setMetrics(JSON.parse((e as MessageEvent).data)); } catch {}
    });
    return () => es.close();
  }, []);

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="absolute right-3 top-3 text-xs text-muted hover:text-white"
      >
        metrics
      </button>
    );
  }

  return (
    <aside className="w-[200px] shrink-0 bg-panel/70 border-l border-border p-3 flex flex-col gap-3 text-xs">
      <div className="flex justify-between items-center">
        <span className="uppercase tracking-wider text-muted text-[10px]">Metrics</span>
        <button onClick={() => setOpen(false)} className="text-muted hover:text-white">×</button>
      </div>

      <Row label="tok/s" value={tokensPerSec != null ? tokensPerSec.toFixed(1) : "—"} />
      <Row label="ping" value={metrics?.ping_ms != null ? `${metrics.ping_ms} ms` : "—"} />

      <div className="flex flex-col gap-1">
        <div className="flex justify-between">
          <span className="text-muted">GPU</span>
          <span className="text-neutral-300 truncate ml-2 max-w-[100px]">
            {metrics?.gpu.gpu ?? "—"}
          </span>
        </div>
        {metrics?.gpu.vram_pct != null && (
          <div className="h-1 rounded-full bg-neutral-800 overflow-hidden">
            <div
              className={clsx("h-full transition-all", overspillColor[metrics.gpu.overspill])}
              style={{ width: `${Math.min(100, metrics.gpu.vram_pct)}%` }}
            />
          </div>
        )}
        {metrics?.gpu.vram_used_mb != null && metrics.gpu.vram_total_mb != null && (
          <div className="text-muted text-[10px]">
            {metrics.gpu.vram_used_mb} / {metrics.gpu.vram_total_mb} MB
          </div>
        )}
      </div>
    </aside>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-muted">{label}</span>
      <span className="text-neutral-200 tabular-nums">{value}</span>
    </div>
  );
}
