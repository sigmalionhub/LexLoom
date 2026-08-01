import type { Metadata, Viewport } from 'next';
import './globals.css';

const TITLE = 'LexLoom — sourced answers, with the backend in view';
const DESCRIPTION =
  'A legal-research assistant over a multi-tenant FastAPI service: sources arrive before the first word, and a live panel shows the stream, the timings and the tenant boundary as they happen.';
const SITE = 'https://lexloom.sigmalion.io';
/** Built from `scripts/opengraph-image.tsx`; see that file for the regeneration steps. */
const CARD = {
  url: '/og.png',
  width: 1200,
  height: 630,
  alt: 'LexLoom — sources arrive before the first word',
};

export const metadata: Metadata = {
  // The page's whole job is to be opened from a link someone pasted. Without
  // these it unfurls in Slack and LinkedIn as a bare URL, which is a poor first
  // impression for the one impression it gets.
  metadataBase: new URL(SITE),
  title: TITLE,
  description: DESCRIPTION,
  applicationName: 'LexLoom',
  authors: [{ name: 'Sergey Korkishko' }],
  robots: { index: true, follow: true },
  openGraph: {
    type: 'website',
    url: SITE,
    siteName: 'LexLoom',
    title: TITLE,
    description: DESCRIPTION,
    locale: 'en',
    images: [CARD],
  },
  twitter: {
    card: 'summary_large_image',
    title: TITLE,
    description: DESCRIPTION,
    images: [CARD],
  },
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
