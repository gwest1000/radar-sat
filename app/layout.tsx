import type { Metadata } from "next";
import "./globals.css";

const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

export const metadata: Metadata = {
  metadataBase: new URL("https://gwest1000.github.io/radar-sat/"),
  title: "BC Satellite/Radar/Lightning",
  description:
    "Operational satellite, radar, precipitation-type, and lightning loops for British Columbia.",
  icons: {
    icon: `${basePath}/favicon.svg`,
    shortcut: `${basePath}/favicon.svg`,
  },
  openGraph: {
    title: "BC Satellite/Radar/Lightning",
    description:
      "Operational satellite, radar, precipitation-type, and lightning loops for British Columbia.",
    type: "website",
    url: "https://gwest1000.github.io/radar-sat/",
    siteName: "BC Satellite/Radar/Lightning",
    images: [
      {
        url: "https://gwest1000.github.io/radar-sat/og-radar-sat.png",
        width: 1200,
        height: 630,
        alt: "BC satellite, radar and lightning observational loops",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "BC Satellite/Radar/Lightning",
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
