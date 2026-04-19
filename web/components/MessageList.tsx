"use client";
import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import clsx from "clsx";
import type { Message } from "@/lib/api";

export function MessageList({ messages, streaming }: { messages: Message[]; streaming: string }) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, streaming]);

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-3xl mx-auto px-4 py-6 space-y-6">
        {messages.map((m) => <Bubble key={m.id} role={m.role} content={m.content} />)}
        {streaming && <Bubble role="assistant" content={streaming} />}
        <div ref={endRef} />
      </div>
    </div>
  );
}

function Bubble({ role, content }: { role: "user" | "assistant"; content: string }) {
  return (
    <div className={clsx("flex", role === "user" ? "justify-end" : "justify-start")}>
      <div
        className={clsx(
          "rounded-2xl px-4 py-2.5 max-w-[85%] text-[14px] leading-relaxed prose prose-invert prose-sm",
          role === "user" ? "bg-accent text-white" : "bg-panel border border-border"
        )}
      >
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
      </div>
    </div>
  );
}
