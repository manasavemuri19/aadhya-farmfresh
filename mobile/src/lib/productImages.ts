/**
 * Local product photography, bundled into the app — never fetched from a
 * remote host. Resolution is by keyword match against the product name,
 * because that field is present everywhere a product shows up (catalog
 * cards, cart lines, order history) while slug/category are only available
 * on the catalog response. Keyword matching also means a renamed or
 * reworded product still resolves sensibly without a lookup table to keep
 * in sync.
 *
 * Products without real photography yet fall back to a plain "photo coming
 * soon" placeholder rather than silently borrowing an unrelated product's
 * photo — a wrong picture is worse than an honest blank.
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
const PLACEHOLDER = require('../../assets/products/placeholder.jpg');

// Ordered so more specific words are checked first — "buttermilk" must win
// over the "butter" substring it contains, "brown eggs" and "eggs" share a
// photo, and multi-word sweets are matched before any shorter overlap.
const RULES: [RegExp, ReturnType<typeof require>][] = [
  [/buttermilk/i, BUTTERMILK],
  [/curd/i, CURD],
  [/khoya|mawa/i, PANEER],
  [/paneer/i, PANEER],
  [/ghee/i, GHEE],
  [/butter/i, BUTTER],
  [/pickle|avakaya|gongura/i, PICKLE],
  [/buffalo milk|cow milk|\bmilk\b/i, MILK],
];

function byName(name: string): ReturnType<typeof require> | null {
  for (const [pattern, image] of RULES) {
    if (pattern.test(name)) return image;
  }
  return null;
}

// Category-level fallback is only used where every product in that category
// looks close enough to be a reasonable stand-in. "Ghee & Butter" is
// deliberately excluded here even though Ghee and Butter each have their own
// photo above — Honey and Jam live in that same category but look nothing
// like a jar of ghee, so they fall through to the placeholder instead of
// borrowing a photo that would actively mislead a customer.
const BY_CATEGORY: Record<string, ReturnType<typeof require>> = {
  milk: MILK,
  curd: CURD,
  'paneer-cheese': PANEER,
};

/**
 * Resolve the image for a product or cart/order line. A remote `image_url`
 * from the backend (if one is ever supplied later) wins when present;
 * otherwise falls back to a name-keyword match, then category, then an
 * honest placeholder — never a confidently wrong photo.
 */
export function productImageSource(
  name: string,
  category?: string,
  remoteUrl?: string,
): ReturnType<typeof require> | { uri: string } {
  if (remoteUrl && remoteUrl.trim().length > 0) return { uri: remoteUrl };
  return byName(name) ?? (category ? BY_CATEGORY[category] : null) ?? PLACEHOLDER;
}
