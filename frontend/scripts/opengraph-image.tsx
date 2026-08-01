import { ImageResponse } from 'next/og';

/**
 * The source of `public/og.png`, the link preview card.
 *
 * To regenerate: copy this file to `app/opengraph-image.tsx`, run
 * `npm run build`, copy `out/opengraph-image` to `public/og.png`, and remove it
 * from `app/` again.
 *
 * That detour exists for one reason. As a live route, Next emits the card at
 * `out/opengraph-image` — with no file extension — and nginx serves an unknown
 * extension as `application/octet-stream`, which some crawlers refuse. Shipping
 * the same bytes as an ordinary `public/og.png` means the correct content type
 * everywhere with no server configuration to remember, and the card costs one
 * committed 32 KB file instead of a deployment footnote.
 */
export const alt = 'LexLoom — sourced answers over a multi-tenant API';
export const size = { width: 1200, height: 630 };
export const contentType = 'image/png';

export default function OpengraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: '100%',
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'space-between',
          background: '#171D24',
          padding: '72px 80px',
          fontFamily: 'sans-serif',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <div style={{ display: 'flex', fontSize: 62, color: '#DBE2EA', letterSpacing: '-0.02em' }}>
            LexLoom
          </div>
          <div
            style={{
              display: 'flex',
              marginTop: 22,
              fontSize: 30,
              lineHeight: 1.4,
              color: '#8593A3',
              maxWidth: 880,
            }}
          >
            Sources arrive before the first word — and a live panel measures by how much.
          </div>
        </div>

        {/* The timeline motif from the panel, to scale with a real reading. */}
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <div style={{ display: 'flex', alignItems: 'center', height: 26 }}>
            <div style={{ display: 'flex', width: 3, height: 22, background: '#D59C3C' }} />
            <div style={{ display: 'flex', width: 44, height: 1, background: '#2B343F' }} />
            <div style={{ display: 'flex', width: 2, height: 16, background: '#DBE2EA' }} />
            <div
              style={{ display: 'flex', width: 760, height: 7, background: '#8A6A2C', borderRadius: 4 }}
            />
            <div style={{ display: 'flex', width: 2, height: 16, background: '#DBE2EA' }} />
          </div>
          <div style={{ display: 'flex', marginTop: 26, fontSize: 25, color: '#8593A3' }}>
            <span style={{ color: '#D59C3C' }}>citations</span>
            <span style={{ marginLeft: 14 }}>
              → first token · multi-tenant FastAPI, RLS in Postgres, SSE
            </span>
          </div>
        </div>
      </div>
    ),
    size,
  );
}
