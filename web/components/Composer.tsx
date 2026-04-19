"use client";
import { useRef, useState } from "react";
import { Paperclip, ArrowUp, X } from "lucide-react";
import clsx from "clsx";
import { estimateTokens } from "@/lib/tokens";

interface Props {
  contextWindow: number;
  onSend: (text: string, attachments: UploadRef[]) => void;
  disabled?: boolean;
}

export interface UploadRef {
  id: string;
  filename: string;
  mime_type: string;
  size: number;
}

export function Composer({ contextWindow, onSend, disabled }: Props) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<UploadRef[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);

  const tokens = estimateTokens(text);
  const pct = Math.min(100, (tokens / contextWindow) * 100);
  const barColor = pct > 90 ? "bg-red-500" : pct > 70 ? "bg-amber-500" : "bg-accent";

  async function handleFiles(files: FileList | null) {
    if (!files) return;
    for (const file of Array.from(files)) {
      const fd = new FormData();
      fd.append("file", file);
      try {
        const r = await fetch("/api/uploads", { method: "POST", body: fd });
        if (r.ok) setAttachments((a) => [...a, await r.json()]);
      } catch {}
    }
  }

  function submit() {
    const body = text.trim();
    if (!body || disabled) return;
    onSend(body, attachments);
    setText("");
    setAttachments([]);
  }

  return (
    <div className="border-t border-border p-3 bg-bg">
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-2">
          {attachments.map((a) => (
            <span key={a.id} className="text-xs px-2 py-1 rounded-md bg-[#1a1a1a] flex items-center gap-2">
              {a.filename}
              <button
                onClick={() => setAttachments((prev) => prev.filter((x) => x.id !== a.id))}
                className="text-muted hover:text-white"
              >
                <X size={12} />
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="relative flex items-end gap-2 rounded-xl border border-border bg-panel p-2">
        <button
          onClick={() => fileInput.current?.click()}
          className="p-2 text-muted hover:text-white"
          aria-label="Attach file"
        >
          <Paperclip size={16} />
        </button>
        <input
          ref={fileInput}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => handleFiles(e.target.files)}
        />

        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
          }}
          placeholder="Type a message..."
          rows={1}
          className="flex-1 bg-transparent outline-none resize-none py-2 text-sm placeholder:text-muted max-h-48"
        />

        <button
          onClick={submit}
          disabled={!text.trim() || disabled}
          className="p-2 rounded-md bg-accent text-white disabled:opacity-30"
          aria-label="Send"
        >
          <ArrowUp size={16} />
        </button>
      </div>

      <div className="mt-1.5 h-[2px] rounded-full bg-neutral-800 overflow-hidden">
        <div className={clsx("h-full transition-all", barColor)} style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 text-[10px] text-muted tabular-nums">
        ~{tokens.toLocaleString()} / {contextWindow.toLocaleString()} tokens
      </div>
    </div>
  );
}
