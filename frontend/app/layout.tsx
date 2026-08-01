import type { Metadata, Viewport } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'LexLoom — sourced answers, with the backend in view',
  description:
    'A legal-research assistant over a multi-tenant FastAPI service: sources arrive before the first word, and a live panel shows the stream, the timings and the tenant boundary as they happen.',
  robots: { index: true, follow: true },
};

export const viewport: Viewport = {
  themeColor: '#171D24',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
