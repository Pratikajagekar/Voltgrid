# VoltGrid — EV Charging Discovery, Route Planning, Reservation & Verification Platform

A complete Flask + SQLite web app for discovering EV charging stations on a live map,
planning range-aware routes, reserving time slots with guaranteed no double-booking,
verifying payments, and — new in this version — **email verification, KYC-verified
station owners, and a Super Admin panel**, fronted by a proper marketing landing page.

---

## 1. Quick Setup

```bash
# 1. (Recommended) create a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
python app.py
```

Open **http://127.0.0.1:5000**. A SQLite database (`evcharge.db`), an uploads folder
(`static/uploads/`), and a KYC-documents folder (`static/kyc_uploads/`) are created
automatically on first run.

If you already have an `evcharge.db` from an earlier version, it's **automatically
upgraded in place** the next time you run `python app.py` — new columns/tables are
added without touching your existing data. Pre-existing accounts are grandfathered in
as email-verified so nobody gets locked out.

---

## 2. What's New: Landing Page, Verification & Admin

### Landing page
`/` is now a public marketing page (hero, feature grid, CTAs) — it no longer *is* the
login form. Login/register moved to **`/auth`**.

### Email verification
Every new **driver** and **station owner** account must verify their email before they
can log in.

- On registration, a verification link is generated and "sent."
- **No SMTP configured?** The app runs in **console mode** — the link is printed to
  your terminal instead of emailed, so you can fully test and demo the app without any
  real email account. Look for a block like this in the terminal:
  ```
  ======================================================================
  [DEV EMAIL — no SMTP configured] To: jane@example.com
  Subject: Verify your VoltGrid email
  ...
  http://127.0.0.1:5000/verify_email/<token>
  ======================================================================
  ```
  Copy that link into your browser to verify.
- Trying to log in before verifying redirects to a **Resend Verification Email** page.

### KYC verification for Station Owners
Station owners must upload a KYC document (business proof or government ID — PNG,
JPG, WEBP, or PDF) at registration. Their account starts as **KYC: Pending** and they
**cannot add stations** until a Super Admin approves it. Drivers don't need KYC.

### Super Admin panel
A new `admin` role (never selectable at public registration — see below) gets:
- **`/admin`** — platform-wide stats: drivers, station owners, stations, bookings,
  pending KYC count, pending payment count, unverified-email count.
- **`/admin/users`** — every user, filterable by role, with **Approve/Reject KYC**
  buttons and a link to view the uploaded document.
- **`/admin/stations`** — every station on the platform, across every owner.
- **`/admin/bookings`** — the most recent 200 bookings platform-wide.

---

## 3. Email Verification Setup (real SMTP)

To send real emails instead of console-mode links, set these environment variables
before running `python app.py`:

```bash
# Windows PowerShell
$env:SMTP_HOST="smtp.gmail.com"
$env:SMTP_PORT="587"
$env:SMTP_USERNAME="yourname@gmail.com"
$env:SMTP_PASSWORD="your-16-char-app-password"
$env:SMTP_FROM="yourname@gmail.com"

# macOS/Linux
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USERNAME="yourname@gmail.com"
export SMTP_PASSWORD="your-16-char-app-password"
export SMTP_FROM="yourname@gmail.com"
```

### Getting a Gmail App Password (free, no paid API)
1. Enable 2-Step Verification on your Google account: https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password (name it "VoltGrid"), copy the 16-character code.
4. Use that as `SMTP_PASSWORD` above — **not** your normal Gmail password.

Any standard SMTP provider works the same way (Outlook, SendGrid SMTP relay, Mailgun,
your college's SMTP server, etc.) — just change `SMTP_HOST`/`SMTP_PORT` accordingly.

**Without these variables set, the app still runs completely end-to-end** via
console-mode links — this is intentional so the project is fully demoable offline.

---

## 4. Admin Setup

Admin accounts are **never** created through the public `/register` form — only via a
one-time script, so a stranger can't sign up as an admin:

```bash
python create_admin.py
```

Follow the prompts (username, email, password). Then log in normally at `/auth` — the
app detects the `admin` role and routes you straight to `/admin`.

You can create more than one admin by running the script again with different details.

---

## 5. Roles

- **EV Driver (`vehicle_owner`)** — discover stations, plan routes, reserve slots,
  favourite stations, leave ratings/reviews, save vehicle profiles, view history.
- **Station Owner (`station_owner`)** — KYC-gated; once approved, register/manage
  stations and verify driver payments.
- **Super Admin (`admin`)** — platform oversight: approve/reject KYC, view all users,
  stations, and bookings. Created only via `create_admin.py`.

---

## 6. API / Route Guide

All routes are served by the single Flask app (`app.py`). `POST` routes require a
`csrf_token` form field (Flask-WTF); `/api/*` `GET` routes don't need one.

### Public
| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Landing page |
| `/auth` | GET | Login / Register page |
| `/register` | POST | Create a driver or station-owner account (KYC file required for station owners) |
| `/login` | POST | Log in (blocked until email is verified) |
| `/logout` | GET | Log out |
| `/verify_email/<token>` | GET | Verify an email from the link sent at registration |
| `/resend_verification` | GET/POST | Request a new verification link |

### Driver (`vehicle_owner`, requires login)
| Route | Method | Purpose |
|---|---|---|
| `/user_dashboard` | GET | Discover page (map + list) |
| `/api/stations` | GET | JSON list of stations (`?plug_type=`, `?favorites_only=1`) |
| `/api/station_slots` | GET | Taken time-slots for a station/date (`?station_id=&booking_date=`) |
| `/api/favorites` | GET | Your favorited station IDs |
| `/toggle_favorite/<id>` | POST | Add/remove a station from favorites |
| `/api/station_reviews` | GET | Reviews + average rating for a station (`?station_id=`) |
| `/submit_review` | POST | Submit/update a rating+review (requires an Approved booking there) |
| `/route_planner` | GET | Route planner page |
| `/api/geocode` | GET | Destination search proxy (Nominatim) |
| `/api/route` | GET | Driving route proxy (OSRM) |
| `/api/stations_along_route` | POST | Range-aware station recommendations along a route |
| `/request_booking` | POST | Request a charging slot; owner must accept before payment |
| `/pay_booking/<booking_id>` | POST | Submit transaction reference and payment proof after acceptance |
| `/invoice/<booking_id>` | GET | View/print invoice after owner verifies payment |
| `/cancel_booking/<id>` | POST | Cancel your own pending reservation |
| `/history` | GET | Your reservation ledger + PDF receipt |
| `/vehicles` | GET | Manage saved vehicle profiles |
| `/vehicles/add` | POST | Save a vehicle (name, plug type, range) |
| `/vehicles/<id>/delete` | POST | Remove a saved vehicle |

### Station Owner (`station_owner`, requires login + KYC approved for station actions)
| Route | Method | Purpose |
|---|---|---|
| `/owner_dashboard` | GET | Owner console (stations, verification desk, revenue) |
| `/add_station` | POST | Register a station (blocked until KYC approved) |
| `/toggle_station/<id>` | POST | Toggle a station Online/Offline |
| `/verify_payment/<booking_id>` | POST | Approve/reject a driver's payment proof |

### Super Admin (`admin`, requires login)
| Route | Method | Purpose |
|---|---|---|
| `/admin` | GET | Platform stats dashboard |
| `/admin/users` | GET | All users (`?role=vehicle_owner\|station_owner`) |
| `/admin/kyc/<user_id>/approve` | POST | Approve a station owner's KYC |
| `/admin/kyc/<user_id>/reject` | POST | Reject KYC with a reason |
| `/admin/stations` | GET | All stations platform-wide |
| `/admin/bookings` | GET | Most recent 200 bookings platform-wide |
| `/static/kyc_uploads/<filename>` | GET | View a KYC document (admin-only) |

---

## 7. Roadmap (not yet built — flagged honestly)

These appeared in early planning/reference material but are **not implemented** yet.
Treat anything below as future work, not a current feature:

- Elevation-aware routing (would require a paid API like Google Elevation — out of
  scope while keeping the project 100% free/open-source)
- Automatic backup-station rebooking if a reserved slot falls through
- Automated GST/tax billing or a live payment gateway (current flow is proof-upload +
  manual owner approval)
- Full analytics dashboard with charts (trip/revenue trend lines) — the current Admin
  panel gives count-based stats only
- SMS/OTP-based phone verification (only email verification is implemented)

---

## 8. Slot Reservation & Duplicate Prevention

Every reservation is for a specific **station + date + time slot**. The moment a
driver submits, a `SlotLock` row is created with a `UNIQUE(station_id, booking_date,
time_slot)` database constraint — so even simultaneous submissions for the same slot
can't both succeed. Rejecting a booking (or the driver cancelling it) deletes the lock
and frees the slot immediately.

## 9. Notes

- Payment proofs and KYC documents are stored under randomized filenames
  (`static/uploads/`, `static/kyc_uploads/`) to avoid collisions and path traversal.
- KYC documents are only viewable by admins (`/static/kyc_uploads/<file>` is
  role-gated, unlike payment proofs which any logged-in user can view via their own
  booking history).
- CSRF protection (Flask-WTF) is enabled on every form.
- Passwords are hashed with Werkzeug's `generate_password_hash`.
- To reset all data, stop the server and delete `evcharge.db` plus the contents of
  `static/uploads/` and `static/kyc_uploads/`.
