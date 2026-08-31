import type { HelpTree } from './helpTree.types';

/**
 * Content for the Help & Support chat tree — see HelpChatTree for the UI
 * that walks this. Kept honest about what the app actually does rather
 * than reassuring: e.g. refunds on a cancelled paid order are NOT
 * automatic yet (order_service.cancel only releases stock, it never calls
 * the payment provider's refund()), and there's no in-app way to edit an
 * order's address after it's placed. Both of those route straight to
 * "message us" rather than pretending otherwise.
 */
export const HELP_TREE: HelpTree = {
  root: {
    id: 'root',
    text: "Hi! What do you need help with?",
    options: [
      { label: 'Orders & delivery', next: 'orders' },
      { label: 'Payments', next: 'payments' },
      { label: 'Products & stock', next: 'products' },
      { label: 'My account', next: 'account' },
      { label: 'Something else', next: 'other' },
    ],
  },

  orders: {
    id: 'orders',
    text: "What's up with your order?",
    options: [
      { label: 'What do the order statuses mean?', next: 'orders_status' },
      { label: 'I want to cancel my order', next: 'orders_cancel' },
      { label: "Something's wrong with what I received", next: 'orders_wrong' },
      { label: 'Where do you deliver?', next: 'orders_area' },
    ],
  },
  orders_status: {
    id: 'orders_status',
    text:
      'An order moves through: Awaiting payment → Preparing → Packed → On the way → Delivered. ' +
      "If a payment doesn't go through, it just stays at Awaiting payment — that never blocks " +
      'your account, and you can try paying again any time from the order screen.',
  },
  orders_cancel: {
    id: 'orders_cancel',
    text: 'Has your order already gone out for delivery?',
    options: [
      { label: "No, it hasn't left yet", next: 'orders_cancel_early' },
      { label: "Yes, it's on the way", next: 'orders_cancel_late' },
    ],
  },
  orders_cancel_early: {
    id: 'orders_cancel_early',
    text:
      'You can cancel it yourself — open it from My Orders and tap Cancel order. ' +
      "That works any time up until it's marked On the way.",
  },
  orders_cancel_late: {
    id: 'orders_cancel_late',
    text:
      "Once it's on the way, cancelling isn't something the app can do on its own — message us " +
      "below with the order number and we'll sort it out, including a refund if you already paid online.",
  },
  orders_wrong: {
    id: 'orders_wrong',
    text:
      "Sorry about that. Tell us the order number and what's off — missing, wrong, or damaged " +
      "item — below, and we'll fix it directly. That's not something the canned answers here can resolve.",
  },
  orders_area: {
    id: 'orders_area',
    text:
      "We currently deliver out from our farm in Amberpet, Hyderabad. If you're placing your " +
      "first order, checkout will tell you if we can't reach your address yet.",
  },

  payments: {
    id: 'payments',
    text: "What's going on with payment?",
    options: [
      { label: "My payment didn't go through", next: 'payments_failed' },
      { label: 'I paid but the order still shows Awaiting payment', next: 'payments_stuck' },
      { label: 'Do you accept cash on delivery?', next: 'payments_cod' },
      { label: 'I cancelled a paid order — where\u2019s my refund?', next: 'payments_refund' },
    ],
  },
  payments_failed: {
    id: 'payments_failed',
    text:
      "That's fine — nothing was charged if it failed. Go back to the order and tap Pay again; " +
      "it'll open a fresh payment page.",
  },
  payments_stuck: {
    id: 'payments_stuck',
    text:
      'Give it a minute — confirmation can lag slightly behind the actual payment. If it\u2019s been ' +
      'more than 10\u201315 minutes and the money left your account, message us below with the order ' +
      "number so we can check it manually, rather than retrying and risking a double charge.",
  },
  payments_cod: {
    id: 'payments_cod',
    text: 'Yes — cash (or UPI) on delivery is offered as a payment option right at checkout, alongside paying online.',
  },
  payments_refund: {
    id: 'payments_refund',
    text:
      "Being upfront about this: refunds on a cancelled paid order aren't automatic yet — that's a " +
      "genuine gap, not a bug. Message us below with the order number and we'll process it directly.",
  },

  products: {
    id: 'products',
    text: "What's up with a product?",
    options: [
      { label: 'An item shows out of stock', next: 'products_stock' },
      { label: 'The price looks different than I expected', next: 'products_price' },
    ],
  },
  products_stock: {
    id: 'products_stock',
    text:
      "Stock updates through the day as the farm's actual supply changes — if something's out, " +
      "it'll come back once more is available. There's no notify-me yet, so it's worth checking back.",
  },
  products_price: {
    id: 'products_price',
    text:
      'Prices can move day to day with supply, but whatever is showing at checkout is exactly what ' +
      "you're charged — nothing changes after you've paid. If a completed order shows something " +
      'different from what you actually agreed to pay, message us below with the order number.',
  },

  account: {
    id: 'account',
    text: 'What do you need to update?',
    options: [
      { label: 'Change my name, phone or address', next: 'account_edit' },
      { label: 'Change the address on an order I already placed', next: 'account_edit_order' },
    ],
  },
  account_edit: {
    id: 'account_edit',
    text: 'Profile tab → Edit details lets you update your name, phone, and delivery address.',
  },
  account_edit_order: {
    id: 'account_edit_order',
    text:
      "Editing the address on an order that's already placed isn't something the app supports yet — " +
      "message us below with the order number and the correct address, and we'll pass it on before " +
      "it goes out if there's still time.",
  },

  other: {
    id: 'other',
    text: "Tell us what's going on below and we'll take a look.",
  },
};
