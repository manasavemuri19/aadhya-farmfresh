/**
 * Design tokens.
 *
 * The palette is taken from the shop's own shelf rather than from a generic
 * "fresh food app" template: the deep green of gongura leaves, the cool white
 * of milk (not cream — cream reads as bakery), and the red of Andhra chilli
 * masala reserved strictly for price and urgency.
 *
 * Type does the heavy lifting. Prices and quantities are set in DM Mono so
 * they line up in a column like a weighing-scale readout — the one place this
 * interface allows itself a flourish, and an honest one for a shop that sells
 * by the litre and the kilo.
 */

export const color = {
  ink: '#16261D',        // headings, primary surfaces
  inkSoft: '#2E4136',
  body: '#3D4A42',
  muted: '#6C7A70',
  line: '#E2E6DE',
  surface: '#F7F8F4',    // app background — milk white, cool cast
  card: '#FFFFFF',
  chilli: '#C2402F',     // price, discount, destructive
  chilliSoft: '#FBEDEA',
  turmeric: '#D99B2E',   // low stock, warnings
  turmericSoft: '#FDF4E4',
  leaf: '#2F7A52',       // success, in-stock, delivered
  leafSoft: '#EAF4EE',
  white: '#FFFFFF',
} as const;

export const font = {
  display: 'Fraunces_600SemiBold',
  displayBold: 'Fraunces_700Bold',
  body: 'DMSans_400Regular',
  bodyMedium: 'DMSans_500Medium',
  bodyBold: 'DMSans_700Bold',
  // DM Mono ships only 300/400/500 — there is no 700 weight. Medium is the
  // heaviest available, so it plays the 'bold' role for numerals.
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
  xxl: 32,
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
  sm: 6,
  md: 10,
  lg: 14,
  pill: 999,
} as const;

/** One elevation, used sparingly. Stacked shadows read as generic. */
export const shadow = {
  card: {
    shadowColor: '#16261D',
    shadowOpacity: 0.06,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 2 },
    elevation: 2,
  },
} as const;
