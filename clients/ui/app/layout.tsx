import type { Metadata } from "next";
import { Fraunces, IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import "./globals.css";

/**
 * Fraunces for display: a variable old-style serif with real character (its optical-size
 * and "wonk" axes give headings the slightly irregular warmth of set type) — the
 * manuscript half of the identity. IBM Plex Sans carries the interface and Plex Mono the
 * data, because a tool about provenance should render ids and timestamps in something
 * that lines up.
 */
const display = Fraunces({
  subsets: ["latin"],
  variable: "--font-display",
  axes: ["SOFT", "WONK", "opsz"],
});
const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-sans",
});
const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "palimpsest",
  description: "A self-maintaining knowledge base on top of Notion.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${sans.variable} ${mono.variable}`}>
      <body className="grain underwriting font-sans antialiased">
        <div className="relative z-10">{children}</div>
      </body>
    </html>
  );
}
