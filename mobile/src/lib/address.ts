/**
 * `city` staying hardcoded is a deliberate, disclosed scope limit, not an
 * oversight: this app serves one city today, and there's no UI anywhere for
 * a customer to pick a different one. `SERVICE_CITY` exists so that changes
 * when it does, not before.
 *
 * AAD-MOB-022 (multi-address support): `label` used to be a second kind of
 * hardcode alongside `city` — every screen wrote the same literal
 * `'Home'`, which is *why* a customer could only ever have one saved
 * address despite the schema, the backend, and the profile UI all having
 * room for several. That's fixed now: `app/addresses.tsx` (manage saved
 * addresses) and `app/address-edit.tsx` (add/rename/edit one) let the
 * customer pick a real label per address, and checkout.tsx lets them
 * choose among their saved ones rather than always reading `addresses[0]`.
 *
 * `DEFAULT_ADDRESS_LABEL` still has two legitimate jobs, both about a
 * *first* address rather than one of several: the very first address a new
 * customer ever saves, during onboarding (`CompleteProfileScreen.tsx` —
 * "Home" is a reasonable default before they've thought about naming
 * anything), and checkout's fallback when an order is placed with a
 * hand-typed address that was never saved to the profile at all (nothing
 * to look up a real label from, in that case).
 */
export const SERVICE_CITY = 'Hyderabad';
export const DEFAULT_ADDRESS_LABEL = 'Home';
