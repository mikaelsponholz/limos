import type { Metadata } from "next";
import { Outfit } from "next/font/google";
import Header from "@/components/Header";
import { CustomerProvider } from "@/lib/customer";
import "./globals.css";

const outfit = Outfit({ variable: "--font-outfit", subsets: ["latin"], display: "swap" });

export const metadata: Metadata = {
  title: "Chef Aimo",
  description: "Order from Valio Aimo by writing what you need.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className={`${outfit.variable} font-sans antialiased min-h-screen flex flex-col`}>
        <CustomerProvider>
          <Header />
          <div className="flex-1 flex flex-col">{children}</div>
          <footer className="site-footer border-t border-hair">
            <p className="max-w-[1080px] mx-auto px-4 sm:px-6 py-4 m-0 text-[15px] text-soft text-center">
              <strong className="font-semibold text-ink">Demo</strong> | Prices and stock from the Valio Aimo test API. Orders are simulated.
            </p>
          </footer>
        </CustomerProvider>
      </body>
    </html>
  );
}
