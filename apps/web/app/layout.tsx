import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: "YureAtlas · Research workbench",
  description:
    "Local earthquake occurrence forecasting research for the Japan region.",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
