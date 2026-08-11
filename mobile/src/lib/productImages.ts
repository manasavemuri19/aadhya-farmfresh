/**
 * Local product photography, bundled into the app — never fetched from a
 * remote host. Resolution is by keyword match against the product name,
 * because that field is present everywhere a product shows up (catalog
 * cards, cart lines, order history) while slug/category are only available
 * on the catalog response.
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
const CHEESE = require('../../assets/products/cheese.jpg');
const GHEE = require('../../assets/products/ghee.jpg');
const BUTTER = require('../../assets/products/butter.jpg');
const HONEY = require('../../assets/products/honey.jpg');
const JAM = require('../../assets/products/jam.jpg');
const JUNNU = require('../../assets/products/junnu.jpg');
const DOODH_PEDA = require('../../assets/products/doodh-peda.jpg');
const MYSORE_PAK = require('../../assets/products/mysore-pak.jpg');
const EGGS = require('../../assets/products/eggs.jpg');
const BROWN_EGGS = require('../../assets/products/brown-eggs.jpg');
const TEA = require('../../assets/products/tea.jpg');
const BISCUITS = require('../../assets/products/osmania-biscuits.jpg');
const MIXTURE = require('../../assets/products/avd-mixture.jpg');
const COOL_DRINKS = require('../../assets/products/cool-drinks.jpg');
const PICKLE = require('../../assets/products/pickle.jpg');
const PLACEHOLDER = require('../../assets/products/placeholder.jpg');

// Order matters: more specific patterns are checked first, so a substring
// shared with a broader pattern below it doesn't win by accident — e.g.
// "Brown Eggs" must match before the bare "eggs" rule, and "buttermilk"
// must match before "butter".
const RULES: [RegExp, ReturnType<typeof require>][] = [
  [/buttermilk/i, BUTTERMILK],
  [/curd/i, CURD],
  [/khoya|mawa/i, PANEER],
  [/paneer/i, PANEER],
  [/cheese/i, CHEESE],
  [/ghee/i, GHEE],
  [/butter/i, BUTTER],
  [/honey/i, HONEY],
  [/\bjam\b/i, JAM],
  [/junnu/i, JUNNU],
  [/mysore ?pak/i, MYSORE_PAK],
  [/doodh ?peda|\bpeda\b/i, DOODH_PEDA],
  [/brown eggs/i, BROWN_EGGS],
  [/\beggs\b/i, EGGS],
  [/nilofer|chaipatha|\btea\b/i, TEA],
  [/osmania|biscuit/i, BISCUITS],
  [/avd|mixture/i, MIXTURE],
  [/cool drinks?|soft drinks?/i, COOL_DRINKS],
  [/pickle|avakaya|gongura/i, PICKLE],
  [/buffalo milk|cow milk|\bmilk\b/i, MILK],
];

function byName(name: string): ReturnType<typeof require> | null {
  for (const [pattern, image] of RULES) {
    if (pattern.test(name)) return image;
  }
  return null;
}

// Only used for a product whose name doesn't match anything above — every
// category here is one where the fallback photo still looks like a
// reasonable stand-in, not a misleading one.
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
