# Aadhya Pickles & Dairy

A quick-commerce app for a dairy farm: catalog, cart, checkout, payments, order
tracking, and a stock screen the farm actually uses each morning.

```
aadhya/
├── backend/     FastAPI + PostgreSQL API
└── mobile/      Expo React Native app (TypeScript)
```

---

## Setup

### 1. Backend

**Prerequisites:** Python 3.11+, and a PostgreSQL database (local via Docker, or a cloud one — see Deploy).

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

Generate a real JWT secret and paste it into `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

**Start PostgreSQL:**

```bash
docker compose up -d db
```

Create the schema, seed the catalog (13 products, 33 SKUs), and run the API:

```bash
alembic upgrade head
python -m scripts.seed
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/docs for the interactive API.

Run the tests:

```bash
createdb aadhya_test    # once
ENV=test TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/aadhya_test pytest -q
```

### 2. Mobile

**Prerequisites:** Node 18+, and the Expo Go app on your phone.

```bash
cd mobile
npm install
cp .env.example .env
```

**Set the API URL.** On a physical phone, `localhost` means the phone, not your
laptop — use your machine's LAN IP:

```bash
# macOS / Linux
ipconfig getifaddr en0 || hostname -I | awk '{print $1}'
# Windows
ipconfig    # look for IPv4 Address
```

Put that in `mobile/.env`:

```
EXPO_PUBLIC_API_BASE_URL=http://192.168.1.5:8000/v1
```

Then:

```bash
npx expo start
```

Scan the QR code with Expo Go. On the Android emulator use `10.0.2.2` instead
of the LAN IP.

### 3. Try the whole flow

1. Browse the catalog, pick a size, tap **Add**.
2. Open the cart — the total comes from the server, not the app.
3. Sign in. In local builds the OTP is returned in the response and fills in
   automatically (`OTP_DEBUG_ECHO=true`; the server refuses to start in
   production with this on).
4. Enter an address and place the order.
5. With `PAYMENT_PROVIDER=mock`, simulate the gateway confirming payment:

```bash
ORDER_ID="<provider_order_id from the order response>"
PAYLOAD="{\"event_id\":\"evt_1\",\"event\":\"payment.captured\",\"order_id\":\"$ORDER_ID\",\"payment_id\":\"pay_1\",\"amount\":13400}"
SIG=$(python -c "
import hmac,hashlib,sys
print(hmac.new(b'mock-provider-development-secret', sys.argv[1].encode(), hashlib.sha256).hexdigest())
" "$PAYLOAD")

curl -X POST http://localhost:8000/v1/payments/webhook \
  -H "Content-Type: application/json" -H "X-Mock-Signature: $SIG" -d "$PAYLOAD"
```

The order screen is polling, so it flips to **Preparing** on its own.

---

## Going live

### Razorpay

1. Get your key id, key secret, and create a webhook secret in the dashboard.
2. Set `PAYMENT_PROVIDER=razorpay` plus the three credentials.
3. Point the Razorpay webhook at `https://your-api/v1/payments/webhook` and
   subscribe to `payment.captured`, `payment.failed`, and `refund.processed`.
4. Add the Razorpay React Native SDK to the mobile app and open the checkout
   sheet with `order.payment.checkout_payload`. Everything else already works —
   the payload is built server-side and the webhook handling is done.

### SMS

`AuthService._deliver` is the seam. Plug in MSG91, Gupshup or Twilio there and
set `OTP_DEBUG_ECHO=false`. Nothing else changes.

### Deploy

The backend ships as a Docker image and `railway.json` is included, so Railway
picks it up directly.

1. Add a PostgreSQL database to your Railway project. Railway injects
   `DATABASE_URL` automatically — the app normalises whatever URL format the
   provider hands out, so paste it verbatim if you are using Supabase or Neon
   instead.
2. Set the rest of the variables from `.env.example` in the dashboard.
3. Migrations run on container start (`alembic upgrade head` in the Dockerfile
   `CMD`). Alembic is idempotent, so this is safe on every deploy and restart.

The app refuses to boot in production with a default secret, the mock payment
provider, or debug OTP echo still enabled.

### Changing the schema later

```bash
alembic revision --autogenerate -m "add subscriptions"
# review the generated file — autogenerate is a good first draft, not a final answer
alembic upgrade head
```

Commit the migration file. Railway applies it on the next deploy.

For the app, `eas.json` has `development`, `preview` (APK for internal testing)
and `production` (AAB for Play Store) profiles.

---

## How it works

### Products versus variants

Customers browse a **product** ("Full Cream Cow Milk") but buy a **variant**
("1 litre"). Price and stock live on the variant, keyed by SKU. Every cart
line, order line, and stock movement references a SKU, never a product.

This is the single most important modelling decision here. Putting price on
the product is the mistake that forces a rewrite the first time someone asks
for a 5 litre can at a different per-litre rate.

### Money

Money is an `int` of paise everywhere — database, API, business logic. A float
never touches a price. The client formats for display and computes nothing.

### The server prices everything

The app posts SKUs and quantities. It never sends a price. The server
re-prices the cart from the catalog at checkout. A tampered client cannot
change what it is charged, because there is no field to tamper with.

Checkout also sends `expected_total_paise` — the number the customer actually
saw. If the server computes something different, the order is rejected rather
than silently charging a new amount.

### Stock cannot be oversold

Reservation is a single conditional UPDATE:

```sql
UPDATE variants SET stock_qty = stock_qty - :qty
 WHERE sku = :sku AND stock_qty >= :qty
```

The availability check lives *inside* the WHERE clause, so row-level locking
decides the winner and `rowcount` reports it. No read-then-write, no
application lock.

Underneath that, a database CHECK constraint (`stock_qty >= 0`) makes negative
stock impossible regardless of what any future query does. There is a test that
tries to violate it directly and asserts the database refuses.

Ten customers racing for five litres, on ten separate connections: exactly five
succeed. That test is in `tests/test_concurrency_real.py`.

Stock is reserved **before** payment. For fresh dairy that is the right trade —
overselling the last two litres costs a phone call and a refund, while briefly
holding stock that then expires costs nothing. Abandoned checkouts are swept
back onto the shelf after 15 minutes.

### Orders cannot go backwards

Every status change goes through one state machine. `cancelled → delivered` is
impossible by construction, not by convention. Transitions are compare-and-swap
on the current status, so a webhook and a staff tap arriving together cannot
clobber each other.

### Payments

The webhook is the only thing that confirms money. It verifies an HMAC over the
**raw** request body before parsing anything, records the event id so the
gateway's routine replays become no-ops, and refuses to confirm an order whose
amount does not match. The client's post-checkout "verify" call is a UI hint
that lets the success screen appear sooner — it moves no money.

`PaymentProvider` is an interface. The `mock` implementation runs the entire
checkout offline, which is why the flow above works with no gateway account.

### One transaction, no compensation

Reserving stock for every SKU, inserting the order, its lines, its first event,
its payment row and its ledger entries all happen in **one transaction**. If
the third reservation fails, the first two vanish on rollback. There is no
compensating-action code to get wrong, which is the main practical reason this
system is on a relational database.

The one thing that cannot join that transaction is the payment gateway, since
it is a call to somebody else's system. So the gateway order is created
*before* the write opens — a slow gateway must never hold locks on inventory
rows. If the write then fails, the gateway order is orphaned and expires
unpaid, which is a strictly better failure than reserved stock for an order
that does not exist.

### Duplicate orders

Checkout sends an `Idempotency-Key`, generated once per attempt and stable
across retries. Mobile networks drop responses constantly; without this, a
customer who loses signal and taps again gets charged twice. Reusing a key with
a *different* cart is rejected rather than replayed, so a client bug surfaces
instead of hiding.

### Stock the farm can actually manage

`POST /v1/admin/stock` takes either `set_qty` (the morning routine: "we bottled
40 litres") or `delta_qty` (a correction: "two got broken"). Every change is
written to an append-only ledger with the staff member who made it, so an
end-of-day discrepancy can be reconstructed rather than argued about.

Khoya is seeded as `MADE_TO_ORDER` — it is reduced fresh per order and never
blocks on a stock count.

---

## Architecture

```
routes/        HTTP only: parse, authorise, delegate
services/      Business rules. No HTTP, no SQL.
repositories/  Persistence. No business rules.
db/models.py   SQLAlchemy tables and constraints
domain/        Enums and the order state machine
schemas/       Pydantic wire contracts (extra="forbid")
payments/      Provider interface + mock + Razorpay
alembic/       Schema migrations
```

`pricing.py` is pure — no I/O, no clock, no database — which is why it can be
tested exhaustively.

One transaction per request, opened by the `db_session` dependency. A handler
that raises rolls everything back.

## Tests

```bash
cd backend
createdb aadhya_test
ENV=test TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/aadhya_test pytest -q
```

68 tests. Covers pricing and delivery thresholds, every legal and illegal order
transition, phone normalisation, stock reservation, idempotent checkout,
webhook replay, amount-mismatch rejection, and cross-user access.

These run against a **real PostgreSQL**, not a stand-in — which matters,
because the guarantees this system depends on are database behaviours: the
CHECK constraint on stock, row locking during reservation, unique constraints
behind idempotency and webhook replay, and savepoint behaviour on conflict. A
mock would happily pass tests the real thing fails.

`tests/test_concurrency_real.py` opens independent connections and runs them
simultaneously, because a shared session only ever proves sequential
behaviour.

## Why PostgreSQL

Orders are relational: an order has lines, lines reference products, payments
belong to orders. Three things follow from using a relational database here,
and each removes a category of bug rather than merely making one less likely:

- **Real transactions.** The entire checkout write is atomic, so there is no
  compensation logic to maintain or get wrong.
- **Constraints the application cannot bypass.** `stock_qty >= 0` and the
  foreign keys are enforced by the database. A future bug in a query cannot
  oversell stock or orphan an order line.
- **Reporting.** "Revenue by category last month" is one SQL query.

It is also the right foundation if daily-delivery subscriptions get built:
recurring billing and payment ledgers are meaningfully easier relationally.

Order lines deliberately snapshot the product name, variant label and unit
price at purchase time, and are *not* a foreign key to `variants`. A six-month
old receipt must show what the customer actually paid, not today's price.

## Known gaps

- **Rate limiting** is per-phone for OTP only. Put a real limiter in front of
  the API before launch.
- **Full-text search** is `ILIKE`, which is fine for a catalog this size. Past
  a few hundred products, move to a `tsvector` column with a GIN index.
- **Images** point at a placeholder CDN. Wire up S3/R2 or Cloudinary.
- **Push notifications** are not built. FCM would go in `OrderService` at each
  transition.
- **The admin screens** exist as API endpoints, not UI. Decide whether that is
  a staff-role tab in this app or a small separate web page.
- **Refunds** are recorded when the gateway reports them, but nothing initiates
  one yet.
