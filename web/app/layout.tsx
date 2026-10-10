import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Course Watch · UMass Amherst",
  description: "Watch Spring 2027 Computer Science classes and get an email when seats open.",
  robots: { index: false, follow: false },
};
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body>{children}</body></html>;
}
