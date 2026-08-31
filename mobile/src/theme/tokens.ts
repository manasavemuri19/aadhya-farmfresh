/**
 * Design tokens — matched to the Aadya prototype.
 *
 * Palette: a warm beige field (the colour of set curd and khoya), a terracotta
 * orange as the primary action colour (Andhra chilli, the pickle that gives the
 * shop half its shelf), and a deep leaf green as the accent for freshness cues —
 * delivery times, in-stock, savings. Orange leads, green supports, beige holds
 * it all. Prices stay in mono so they line up like a scale readout.
 */

export const color = {
  // Surfaces
  surface: '#F4EDE0',    // app background — warm beige
  surfaceAlt: '#EFE6D6', // image placeholder wells, pressed states
  surfaceDeep: '#4A7A43', // header band 
  card: '#FFFDF9',       // cards — warm white, not clinical white

  // Ink
  ink: '#FBEDEA' , //'2C2013',        // headings — dark roasted brown, not black
  body: '#5A4C3C',       // body text
  muted: '#9A8B78',      // captions, secondary
  line: '#E6DAC7',       // hairlines, borders

  // Primary — terracotta orange
  primary: '#D9642C',       // buttons, active states, price
  primaryPressed: '#BF521F',
  primarySoft: '#F7E4D6',   // tinted backgrounds
  onPrimary: '#FFFFFF',

  // Accent — leaf green
  leaf: '#4A7A43',       // delivery time, in-stock, success
  leafSoft: '#E6EFE0',

  // Signal
  discount: '#C2402F',   // discount badge (deep chilli red)
  discountSoft: '#FBEDEA',
  lowStock: '#C67A1E',   // low-stock warning (turmeric)
  white: '#FFFFFF',
} as const;

export const font = {
  display: 'Fraunces_600SemiBold',
  displayBold: 'Fraunces_700Bold',
  body: 'DMSans_400Regular',
  bodyMedium: 'DMSans_500Medium',
  bodyBold: 'DMSans_700Bold',
  // DM Mono ships 300/400/500 only — 500 is its heaviest, used as the "bold".
  mono: 'DMMono_400Regular',
  monoBold: 'DMMono_500Medium',
} as const;

export const size = {
  xs: 11,
  sm: 13,
  base: 15,
  md: 17,
  lg: 20,
  xl: 26,
  xxl: 34,
} as const;

export const space = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
} as const;

export const radius = {
  sm: 8,
  md: 12,
  lg: 16,
  pill: 999,
} as const;

export const shadow = {
  card: {
    shadowColor: '#2C2013',
    shadowOpacity: 0.07,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 3 },
    elevation: 2,
  },
} as const;
