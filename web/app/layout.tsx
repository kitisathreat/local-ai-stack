import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LocalAIStack",
  description: "Local AI chat",
  viewport: "width=device-width, initial-scale=1, viewport-fit=cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
