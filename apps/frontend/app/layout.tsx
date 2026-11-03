import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "CloudMorph 이전 콘솔",
  description: "AWS → GCP 무중단 이전 실행·승인 화면",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ko" className="h-full antialiased">
      <body className="flex min-h-full flex-col">
        <header className="border-b border-ink/10 bg-paper/90 backdrop-blur">
          <div className="mx-auto flex max-w-5xl flex-wrap items-baseline justify-between gap-x-4 gap-y-1 px-4 py-3 sm:px-6">
            <Link href="/" className="font-semibold tracking-tight">
              CloudMorph <span className="text-ink/50">이전 콘솔</span>
            </Link>
            <span className="font-mono text-xs text-ink/50">AWS ECS·RDS → GCP Cloud Run·Cloud SQL</span>
          </div>
        </header>
        <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 sm:px-6">{children}</main>
      </body>
    </html>
  );
}
