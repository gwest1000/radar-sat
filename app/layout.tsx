import type { Metadata } from "next";
import "./globals.css";

const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

export const metadata: Metadata = {
  metadataBase: new URL("https://gwest1000.github.io/radar-sat/"),
  title: "Radar-Sat | BC Observational Loops",
  description:
    "Operational satellite, radar, precipitation-type, and lightning loops for British Columbia.",
  icons: {
    icon: `${basePath}/favicon.svg`,
    shortcut: `${basePath}/favicon.svg`,
  },
  openGraph: {
    title: "Radar-Sat | BC Observational Loops",
    description:
      "Operational satellite, radar, precipitation-type, and lightning loops for British Columbia.",
    type: "website",
    url: "https://gwest1000.github.io/radar-sat/",
    siteName: "Radar-Sat",
    images: [
      {
        url: "https://gwest1000.github.io/radar-sat/og-radar-sat.png",
        width: 1200,
        height: 630,
        alt: "Radar-Sat: BC observational loops",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Radar-Sat | BC Observational Loops",
    description:
      "Operational satellite, radar, precipitation-type, and lightning loops for British Columbia.",
    images: ["https://gwest1000.github.io/radar-sat/og-radar-sat.png"],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
