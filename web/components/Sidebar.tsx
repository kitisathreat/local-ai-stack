"use client";
import { Plus, Trash2, MessageSquare } from "lucide-react";
import clsx from "clsx";
import type { Chat } from "@/lib/api";

interface Props {
  chats: Chat[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}

export function Sidebar({ chats, activeId, onSelect, onNew, onDelete }: Props) {
  return (
    <aside className="w-64 shrink-0 bg-panel border-r border-border flex flex-col h-full">
      <button
        onClick={onNew}
        className="m-3 px-3 py-2 rounded-md border border-border hover:bg-[#1a1a1a] text-sm flex items-center gap-2 transition-colors"
      >
        <Plus size={14} /> New chat
      </button>
      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {chats.map((c) => (
          <div
            key={c.id}
            onClick={() => onSelect(c.id)}
            className={clsx(
              "group px-3 py-2 rounded-md text-sm cursor-pointer flex items-center gap-2 transition-colors",
              activeId === c.id ? "bg-[#1f1f1f]" : "hover:bg-[#151515]"
            )}
          >
            <MessageSquare size={12} className="text-muted shrink-0" />
            <span className="truncate flex-1">{c.title}</span>
            <button
              onClick={(e) => { e.stopPropagation(); onDelete(c.id); }}
              className="opacity-0 group-hover:opacity-100 text-muted hover:text-red-400"
              aria-label="Delete chat"
            >
              <Trash2 size={12} />
            </button>
          </div>
        ))}
        {!chats.length && (
          <div className="px-3 py-2 text-xs text-muted">No chats yet.</div>
        )}
      </div>
    </aside>
  );
}
