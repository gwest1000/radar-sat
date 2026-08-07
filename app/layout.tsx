import type { Metadata } from "next";
import "./globals.css";

const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

export const metadata: Metadata = {
  metadataBase: new URL("https://gwest1000.github.io/radar-sat/"),
  title: "Real-Time WX Display",
  description:
    "Operational satellite, radar, lightning, smoke, and fire loops for British Columbia, North America, and the Pacific.",
  icons: {
    icon: `${basePath}/favicon.svg`,
    shortcut: `${basePath}/favicon.svg`,
  },
  openGraph: {
    title: "Real-Time WX Display",
    description:
      "Operational satellite, radar, lightning, smoke, and fire loops for British Columbia, North America, and the Pacific.",
    type: "website",
    url: "https://gwest1000.github.io/radar-sat/",
    siteName: "Real-Time WX Display",
    images: [
      {
        url: "https://gwest1000.github.io/radar-sat/og-radar-sat.png",
        width: 1200,
        height: 630,
        alt: "Real-time satellite, radar, lightning, smoke and fire loops",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Real-Time WX Display",
    description:
      "Operational satellite, radar, lightning, smoke, and fire loops for British Columbia, North America, and the Pacific.",
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
