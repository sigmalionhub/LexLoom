import type { Config } from 'tailwindcss';

/**
 * Two materials, one page.
 *
 * The left half is a document: pale cool paper, statute serif, generous
 * measure. The right half is an instrument: dark slate, monospace readouts,
 * one brass accent for anything that is a *measurement*. The split is the
 * identity of the page — it says, before a word is read, that there are two
 * things here and only one of them is the product.
 *
 * Every face below is a system stack. A webfont would mean a download at build
 * time (breaking a clean clone with no network) and a download at page load
 * (delaying the one thing this page exists to show: the first token).
 */
const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        paper: '#E9ECF0',
        'paper-raised': '#F6F8FA',
        ink: '#131820',
        'ink-soft': '#4C5665',
        rule: '#C8D0DA',
        slate: '#171D24',
        'slate-raised': '#1E252E',
        'slate-rule': '#2B343F',
        chalk: '#DBE2EA',
        mist: '#8593A3',
        // Reserved for measurements and for nothing else. The moment brass is
        // used for decoration it stops meaning "this number was measured".
        brass: '#D59C3C',
        'brass-dim': '#8A6A2C',
        alarm: '#CE5B41',
      },
      fontFamily: {
        serif: [
          'Charter',
          'Bitstream Charter',
          'Iowan Old Style',
          'Palatino Linotype',
          'Palatino',
          'Book Antiqua',
          'Georgia',
          'serif',
        ],
        sans: [
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SF Mono',
          'SFMono-Regular',
          'Menlo',
          'Monaco',
          'Cascadia Mono',
          'Roboto Mono',
          'Consolas',
          'Liberation Mono',
          'monospace',
        ],
      },
      fontSize: {
        micro: ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.09em' }],
        data: ['0.75rem', { lineHeight: '1.15rem' }],
      },
      keyframes: {
        'rise-in': {
          from: { opacity: '0', transform: 'translateY(3px)' },
          to: { opacity: '1', transform: 'none' },
        },
        caret: {
          '0%, 45%': { opacity: '1' },
          '55%, 100%': { opacity: '0' },
        },
      },
      animation: {
        'rise-in': 'rise-in 180ms ease-out both',
        caret: 'caret 1.05s steps(1) infinite',
      },
    },
  },
  plugins: [],
};

export default config;
