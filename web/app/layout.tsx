import type { Metadata } from "next";
import { Figtree } from "next/font/google";
import Header from "@/components/Header";
import { CustomerProvider } from "@/lib/customer";
import "./globals.css";

const figtree = Figtree({ variable: "--font-figtree", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Limos",
  description: "Order from Valio Aimo by writing what you need.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className={`${figtree.variable} font-sans antialiased min-h-screen flex flex-col`}>
        <CustomerProvider>
          <Header />
          <div className="flex-1">{children}</div>
          <footer className="max-w-[1080px] w-full mx-auto px-6 py-6 text-sm text-muted">
            Prices and availability from the Valio Aimo test API. Orders are simulated. Ordering rules are fixed code, not AI.
          </footer>
        </CustomerProvider>
      </body>
    </html>
  );
}
