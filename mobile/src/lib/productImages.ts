/**
 * Local product photography, bundled into the app — never fetched from a
 * remote host. Resolution is by keyword match against the product name,
 * because that field is present everywhere a product shows up (catalog
 * cards, cart lines, order history) while slug/category are only available
 * on the catalog response. Keyword matching also means a renamed or
 * reworded product still resolves sensibly without a lookup table to keep
 * in sync.
 *
 * `require()` for a static local asset must be a literal string Metro can
 * see at bundle time — that's why every entry is written out by hand.
 */

const MILK = require('../../assets/products/milk.jpg');
const CURD = require('../../assets/products/curd.jpg');
const BUTTERMILK = require('../../assets/products/buttermilk.jpg');
const PANEER = require('../../assets/products/paneer.jpg');
const GHEE = require('../../assets/products/ghee.jpg');
const BUTTER = require('../../assets/products/butter.jpg');
const PICKLE = require('../../assets/products/pickle.jpg');

// Ordered so more specific words are checked first — "buttermilk" must win
// over the "butter" substring it contains, and "khoya"/paneer share a photo.
const RULES: [RegExp, ReturnType<typeof require>][] = [
  [/buttermilk/i, BUTTERMILK],
  [/curd/i, CURD],
  [/khoya|mawa|paneer/i, PANEER],
  [/ghee/i, GHEE],
  [/butter/i, BUTTER],
  [/pickle|avakaya|gongura/i, PICKLE],
  [/milk/i, MILK],
];

function byName(name: string): ReturnType<typeof require> | null {
  for (const [pattern, image] of RULES) {
    if (pattern.test(name)) return image;
  }
  return null;
}

const BY_CATEGORY: Record<string, ReturnType<typeof require>> = {
  milk: MILK,
  'curd-buttermilk': CURD,
  'paneer-khoya': PANEER,
  'ghee-butter': GHEE,
  pickles: PICKLE,
};

/**
 * Resolve the image for a product or cart/order line. A remote `image_url`
 * from the backend (if one is ever supplied later) wins when present;
 * otherwise falls back to a name-keyword match, then category, then milk —
 * so a card is never blank.
 */
export function productImageSource(
  name: string,
  category?: string,
  remoteUrl?: string,
): ReturnType<typeof require> | { uri: string } {
  if (remoteUrl && remoteUrl.trim().length > 0) return { uri: remoteUrl };
  return byName(name) ?? (category ? BY_CATEGORY[category] : null) ?? MILK;
}
