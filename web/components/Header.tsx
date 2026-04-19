"use client";
import { Brain } from "lucide-react";
import clsx from "clsx";
import type { ModelProfile } from "@/lib/api";

interface Props {
  profiles: ModelProfile[];
  active: ModelProfile | null;
  onChange: (profile: string) => void;
  thinking: boolean;
  onToggleThinking: (on: boolean) => void;
}

export function Header({ profiles, active, onChange, thinking, onToggleThinking }: Props) {
  return (
    <header className="h-12 border-b border-border bg-bg/80 backdrop-blur flex items-center px-3 gap-3">
      <select
        value={active?.profile ?? ""}
        onChange={(e) => onChange(e.target.value)}
        className="bg-panel border border-border rounded-md text-sm px-2 py-1 outline-none"
      >
        {profiles.map((p) => (
          <option key={p.profile} value={p.profile}>{p.name}</option>
        ))}
      </select>

      {active?.supports_thinking && (
        <button
          onClick={() => onToggleThinking(!thinking)}
          className={clsx(
            "flex items-center gap-1.5 text-xs px-2 py-1 rounded-md border transition-colors",
            thinking
              ? "border-accent bg-accent/20 text-white"
              : "border-border text-muted hover:text-white"
          )}
        >
          <Brain size={12} />
          Thinking {thinking ? "on" : "off"}
        </button>
      )}

      <div className="ml-auto text-xs text-muted truncate max-w-[40%]">
        {active?.description}
      </div>
    </header>
  );
}
