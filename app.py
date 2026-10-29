import os
import math
import uuid
from io import BytesIO
from xml.sax.saxutils import escape as xml_escape
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, date

import requests
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, abort, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_wtf import CSRFProtect
from sqlalchemy import func, inspect, text
from sqlalchemy.exc import IntegrityError
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable

# ----------------------------------------------------------------------------
# Absolute path configuration (Windows-safe)
# ----------------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "evcharge.db")

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB

# Public routing/geocoding services (no API key required)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving"
NOMINATIM_HEADERS = {"User-Agent": "VoltGrid-EV-Charging-App/1.0 (educational project)"}
ROUTE_BUFFER_KM = 5.0  # how far off the route a station can be and still count as "along the route"

# Feature flag: email verification is temporarily disabled by default so the
# app is easier to demo/test without SMTP setup. All the verification
# infrastructure (tokens, routes, emails) stays intact — set this env var to
# "true" to re-enable the requirement with zero code changes.
REQUIRE_EMAIL_VERIFICATION = os.environ.get("REQUIRE_EMAIL_VERIFICATION", "false").lower() == "true"

# Real-time soft-lock: how long a station owner has to verify a payment
# before the reservation auto-expires and the slot is released to others.
PENDING_VERIFICATION_TIMEOUT_HOURS = float(os.environ.get("PENDING_VERIFICATION_TIMEOUT_HOURS", "24"))

# Multi-currency support — lets station owners anywhere in the world list
# prices in their own currency instead of being locked to INR.
CURRENCY_SYMBOLS = {
    "INR": "\u20b9", "USD": "$", "EUR": "\u20ac", "GBP": "\u00a3",
    "JPY": "\u00a5", "AUD": "A$", "CAD": "C$", "CNY": "\u00a5",
    "AED": "AED", "SGD": "S$",
}
CURRENCIES = list(CURRENCY_SYMBOLS.keys())
DEFAULT_CURRENCY = "INR"

# Email (SMTP). If SMTP_HOST is not set, the app falls back to "console mode":
# verification links are printed to the terminal, so the app is fully runnable
# end-to-end without real email credentials. See README "Email Verification
# Setup" for how to enable real sending.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME or "no-reply@voltgrid.local")
EMAIL_TOKEN_MAX_AGE = 60 * 60 * 24  # 24 hours
RESEND_COOLDOWN_SECONDS = 60

KYC_UPLOAD_DIR = None  # set after app/config init below

# ----------------------------------------------------------------------------
# App initialization
# ----------------------------------------------------------------------------
app = Flask(
    __name__,
    template_folder=TEMPLATE_DIR,
    static_folder=STATIC_DIR,
)

app.config["SECRET_KEY"] = os.environ.get("EVCHARGE_SECRET_KEY", "dev-secret-key-change-in-production-9f8a7d6c")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

KYC_UPLOAD_DIR = os.path.join(STATIC_DIR, "kyc_uploads")

# Ensure required directories exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(KYC_UPLOAD_DIR, exist_ok=True)
os.makedirs(TEMPLATE_DIR, exist_ok=True)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
token_serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])

login_manager = LoginManager(app)
login_manager.login_view = "auth_page"
login_manager.login_message = "Please log in to access that page."
login_manager.login_message_category = "warning"

PLUG_TYPES = ["CCS2 Fast Charger", "Type 2 AC", "CHAdeMO"]
STATION_STATUSES = ["Online", "Offline"]
BOOKING_STATUSES = ["Requested", "Accepted - Awaiting Payment", "Pending Verification", "Approved", "Rejected", "Cancelled", "Expired"]
ROLES = ["vehicle_owner", "station_owner"]  # selectable at public registration
KYC_STATUSES = ["Not Required", "Pending", "Approved", "Rejected"]


# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------
class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="vehicle_owner")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Email verification
    email_verified = db.Column(db.Boolean, nullable=False, default=False)
    email_verification_sent_at = db.Column(db.DateTime, nullable=True)

    # KYC (station owners only — drivers are "Not Required")
    kyc_status = db.Column(db.String(20), nullable=False, default="Not Required")
    kyc_document = db.Column(db.String(255), nullable=True)
    kyc_rejection_reason = db.Column(db.String(255), nullable=True)

    stations = db.relationship(
        "ChargingStation", backref="owner", lazy=True, cascade="all, delete-orphan"
    )
    bookings = db.relationship(
        "Booking", backref="driver", lazy=True, cascade="all, delete-orphan"
    )

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_station_owner(self):
        return self.role == "station_owner"

    @property
    def is_vehicle_owner(self):
        return self.role == "vehicle_owner"

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def kyc_approved(self):
        return self.kyc_status in ("Approved", "Not Required")


class ChargingStation(db.Model):
    __tablename__ = "charging_stations"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    plug_type = db.Column(db.String(50), nullable=False)
    price_per_kwh = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default=DEFAULT_CURRENCY)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Online")
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    bookings = db.relationship(
        "Booking", backref="station", lazy=True, cascade="all, delete-orphan"
    )
    favorites = db.relationship(
        "Favorite", backref="station", lazy=True, cascade="all, delete-orphan"
    )
    reviews = db.relationship(
        "Review", backref="station", lazy=True, cascade="all, delete-orphan"
    )

    @property
    def currency_symbol(self):
        return CURRENCY_SYMBOLS.get(self.currency, self.currency)

    def rating_summary(self):
        row = (
            db.session.query(func.avg(Review.rating), func.count(Review.id))
            .filter(Review.station_id == self.id)
            .first()
        )
        avg_rating, review_count = row if row else (None, 0)
        return round(avg_rating, 1) if avg_rating else None, review_count or 0

    def to_dict(self, user_id=None):
        avg_rating, review_count = self.rating_summary()
        is_favorited = False
        if user_id:
            is_favorited = (
                db.session.query(Favorite.id)
                .filter_by(user_id=user_id, station_id=self.id)
                .first()
                is not None
            )
        return {
            "id": self.id,
            "name": self.name,
            "plug_type": self.plug_type,
            "price_per_kwh": self.price_per_kwh,
            "currency": self.currency,
            "currency_symbol": self.currency_symbol,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "status": self.status,
            "owner_id": self.owner_id,
            "avg_rating": avg_rating,
            "review_count": review_count,
            "is_favorited": is_favorited,
        }


class Booking(db.Model):
    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    station_id = db.Column(db.Integer, db.ForeignKey("charging_stations.id"), nullable=False)
    booking_date = db.Column(db.Date, nullable=False, default=date.today)
    time_slot = db.Column(db.String(50), nullable=False)
    amount_paid = db.Column(db.Float, nullable=False)
    transaction_id = db.Column(db.String(64), nullable=True, default="")
    energy_kwh = db.Column(db.Float, nullable=True)
    payment_proof = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(30), nullable=False, default="Pending Verification")
    rejection_reason = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Real-time auto-expiring soft lock: a Pending Verification booking that
    # the station owner doesn't act on before this deadline is automatically
    # expired and its SlotLock released — so an unresponsive owner can never
    # permanently block a slot for every other driver.
    verification_deadline = db.Column(db.DateTime, nullable=True)

    def seconds_until_expiry(self):
        if self.status != "Pending Verification" or not self.verification_deadline:
            return None
        remaining = (self.verification_deadline - datetime.utcnow()).total_seconds()
        return max(0, int(remaining))


class SlotLock(db.Model):
    """
    Guarantees exactly one active reservation per (station, date, time slot).
    Created atomically alongside a Booking; the unique constraint means the
    database itself rejects a second driver trying to grab the same slot at
    the same moment, closing the race condition a plain status check can't.
    Deleted when a station owner rejects the booking, freeing the slot again.
    """
    __tablename__ = "slot_locks"
    __table_args__ = (
        db.UniqueConstraint("station_id", "booking_date", "time_slot", name="uq_station_date_slot"),
    )

    id = db.Column(db.Integer, primary_key=True)
    station_id = db.Column(db.Integer, db.ForeignKey("charging_stations.id"), nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=False)
    booking_date = db.Column(db.Date, nullable=False)
    time_slot = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Favorite(db.Model):
    """A driver's saved/starred station — Google-Maps-style bookmarking."""
    __tablename__ = "favorites"
    __table_args__ = (
        db.UniqueConstraint("user_id", "station_id", name="uq_user_station_favorite"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    station_id = db.Column(db.Integer, db.ForeignKey("charging_stations.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Review(db.Model):
    """A driver's star rating + written review for a station (one per driver/station)."""
    __tablename__ = "reviews"
    __table_args__ = (
        db.UniqueConstraint("user_id", "station_id", name="uq_user_station_review"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    station_id = db.Column(db.Integer, db.ForeignKey("charging_stations.id"), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Vehicle(db.Model):
    """A driver's saved vehicle profile — auto-fills range & plug type in Route Planner."""
    __tablename__ = "vehicles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    plug_type = db.Column(db.String(50), nullable=False)
    full_range_km = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))



# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def save_payment_proof(file_storage):
    """Validate and persist an uploaded payment proof. Returns stored filename or None."""
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        raise ValueError("Invalid file type. Only PNG, JPG, and WEBP are allowed.")

    original_name = secure_filename(file_storage.filename)
    ext = original_name.rsplit(".", 1)[1].lower()
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    destination = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
    file_storage.save(destination)
    return unique_name


KYC_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "pdf"}


def save_kyc_document(file_storage):
    """Validate and persist an uploaded KYC document. Returns stored filename or None."""
    if not file_storage or file_storage.filename == "":
        return None
    if "." not in file_storage.filename:
        raise ValueError("Invalid file.")
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    if ext not in KYC_ALLOWED_EXTENSIONS:
        raise ValueError("Invalid file type. Only PNG, JPG, WEBP, or PDF are allowed.")

    unique_name = f"{uuid.uuid4().hex}.{ext}"
    destination = os.path.join(KYC_UPLOAD_DIR, unique_name)
    file_storage.save(destination)
    return unique_name


# ----------------------------------------------------------------------------
# Email (verification links) — real SMTP if configured, console fallback otherwise
# ----------------------------------------------------------------------------
def send_email(to_address, subject, body):
    """
    Send a plain-text email. If SMTP_HOST is not configured, prints the email
    to the console instead so the app remains fully usable for local/offline
    demos without real email credentials.
    """
    if not SMTP_HOST:
        print("=" * 70)
        print(f"[DEV EMAIL — no SMTP configured] To: {to_address}")
        print(f"Subject: {subject}")
        print(body)
        print("=" * 70)
        return True

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_address
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            if SMTP_USERNAME:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_address], msg.as_string())
        return True
    except Exception as exc:  # noqa: BLE001 — logged for the developer, not shown raw to users
        print(f"[EMAIL SEND FAILED] {exc}")
        return False


def generate_email_token(user):
    return token_serializer.dumps(user.email, salt="email-verify")


def verify_email_token(token, max_age=EMAIL_TOKEN_MAX_AGE):
    try:
        return token_serializer.loads(token, salt="email-verify", max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def send_verification_email(user):
    token = generate_email_token(user)
    link = url_for("verify_email", token=token, _external=True)
    send_email(
        user.email,
        "Verify your VoltGrid email",
        f"Hi {user.username},\n\n"
        f"Please verify your email address to activate your VoltGrid account:\n{link}\n\n"
        f"This link expires in 24 hours. If you didn't create this account, ignore this email.",
    )
    user.email_verification_sent_at = datetime.utcnow()
    db.session.commit()
    return link


def role_required(role_name):
    def decorator(f):
        from functools import wraps

        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth_page"))
            if current_user.role != role_name:
                abort(403)
            return f(*args, **kwargs)

        return wrapped
    return decorator


# ----------------------------------------------------------------------------
# Real-time auto-expiring soft lock
# ----------------------------------------------------------------------------
def expire_stale_bookings():
    """
    Self-healing sweep: unprocessed requests, unpaid accepted slots, and
    unverified payments expire after their deadline and release the slot. Runs
    lazily at the top of the routes that read booking/slot state — no
    background worker or cron job required, which keeps the app a single
    process that's trivial to deploy anywhere.
    """
    now = datetime.utcnow()
    stale = Booking.query.filter(
        Booking.status.in_(["Requested", "Accepted - Awaiting Payment", "Pending Verification"]),
        Booking.verification_deadline.isnot(None),
        Booking.verification_deadline < now,
    ).all()
    if not stale:
        return
    for booking in stale:
        booking.status = "Expired"
        lock = SlotLock.query.filter_by(booking_id=booking.id).first()
        if lock:
            db.session.delete(lock)
    db.session.commit()


# ----------------------------------------------------------------------------
# Route-planning helpers (great-circle distance, route sampling, station matching)
# ----------------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two lat/lng points, in kilometers."""
    r = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def decimate_route(coords, max_points=250):
    """Reduce a long route polyline to at most max_points, keeping start/end."""
    if len(coords) <= max_points:
        return coords
    step = len(coords) / max_points
    sampled = [coords[int(i * step)] for i in range(max_points)]
    if sampled[-1] != coords[-1]:
        sampled.append(coords[-1])
    return sampled


def compute_route_recommendations(coords, range_km, buffer_km=ROUTE_BUFFER_KM, plug_type=None, user_id=None):
    """
    Given a route polyline (list of (lat, lng)) and the driver's remaining
    range in km, find Online charging stations near the route, tag each with
    how far along the route it sits and whether it's reachable on the
    current charge, and pick a single best recommended stop. Optionally
    restrict to a single plug_type (e.g. matching the driver's saved vehicle).
    """
    coords = decimate_route(coords)

    cumulative = [0.0]
    for i in range(1, len(coords)):
        lat1, lon1 = coords[i - 1]
        lat2, lon2 = coords[i]
        cumulative.append(cumulative[-1] + haversine_km(lat1, lon1, lat2, lon2))
    total_distance_km = cumulative[-1] if cumulative else 0.0

    station_query = ChargingStation.query.filter_by(status="Online")
    if plug_type:
        station_query = station_query.filter_by(plug_type=plug_type)
    stations = station_query.all()

    candidates = []
    for s in stations:
        best_dist, best_cum = None, None
        for idx, (lat, lng) in enumerate(coords):
            d = haversine_km(s.latitude, s.longitude, lat, lng)
            if best_dist is None or d < best_dist:
                best_dist, best_cum = d, cumulative[idx]
        if best_dist is not None and best_dist <= buffer_km:
            avg_rating, review_count = s.rating_summary()
            is_favorited = False
            if user_id:
                is_favorited = (
                    db.session.query(Favorite.id)
                    .filter_by(user_id=user_id, station_id=s.id)
                    .first()
                    is not None
                )
            candidates.append({
                "id": s.id,
                "name": s.name,
                "plug_type": s.plug_type,
                "price_per_kwh": s.price_per_kwh,
                "latitude": s.latitude,
                "longitude": s.longitude,
                "distance_along_route_km": round(best_cum, 2),
                "distance_from_route_km": round(best_dist, 2),
                "reachable": best_cum <= range_km,
                "avg_rating": avg_rating,
                "review_count": review_count,
                "is_favorited": is_favorited,
            })

    candidates.sort(key=lambda r: r["distance_along_route_km"])

    reachable_without_charging = total_distance_km <= range_km
    recommended_station_id = None
    if not reachable_without_charging:
        reachable_candidates = [c for c in candidates if c["reachable"]]
        if reachable_candidates:
            # Best pick = the farthest reachable station, i.e. the last
            # sensible place to top up before the range runs out.
            recommended_station_id = max(
                reachable_candidates, key=lambda r: r["distance_along_route_km"]
            )["id"]

    return {
        "total_distance_km": round(total_distance_km, 2),
        "reachable_without_charging": reachable_without_charging,
        "recommended_station_id": recommended_station_id,
        "stations": candidates,
    }


# ----------------------------------------------------------------------------
# Routes: Landing, Auth
# ----------------------------------------------------------------------------
def redirect_to_dashboard(user):
    if user.is_admin:
        return redirect(url_for("admin_dashboard"))
    if user.is_station_owner:
        return redirect(url_for("owner_dashboard"))
    return redirect(url_for("user_dashboard"))


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect_to_dashboard(current_user)
    return render_template("landing.html")


@app.route("/auth")
def auth_page():
    if current_user.is_authenticated:
        return redirect_to_dashboard(current_user)
    return render_template("auth.html")


@app.route("/register", methods=["POST"])
def register():
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "vehicle_owner")
    kyc_file = request.files.get("kyc_document")

    if role not in ROLES:
        flash("Invalid role selected.", "error")
        return redirect(url_for("auth_page"))

    if not username or not email or not password:
        flash("All fields are required to register.", "error")
        return redirect(url_for("auth_page"))

    if len(password) < 6:
        flash("Password must be at least 6 characters long.", "error")
        return redirect(url_for("auth_page"))

    if role == "station_owner" and (not kyc_file or kyc_file.filename == ""):
        flash("Station owners must upload a KYC document (business proof or ID) to register.", "error")
        return redirect(url_for("auth_page"))

    existing = User.query.filter(
        (User.username == username) | (User.email == email)
    ).first()
    if existing:
        flash("Username or email already registered.", "error")
        return redirect(url_for("auth_page"))

    user = User(username=username, email=email, role=role)
    user.set_password(password)

    if role == "station_owner":
        try:
            stored_name = save_kyc_document(kyc_file)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("auth_page"))
        user.kyc_document = stored_name
        user.kyc_status = "Pending"
    else:
        user.kyc_status = "Not Required"

    if not REQUIRE_EMAIL_VERIFICATION:
        user.email_verified = True

    db.session.add(user)
    db.session.commit()

    if REQUIRE_EMAIL_VERIFICATION:
        send_verification_email(user)
        flash(
            "Account created! We've sent a verification link to your email — "
            "please verify before logging in.",
            "success",
        )
        return redirect(url_for("auth_page"))

    login_user(user)
    flash(f"Welcome aboard, {username}!", "success")
    return redirect_to_dashboard(user)


@app.route("/login", methods=["POST"])
def login():
    identifier = request.form.get("identifier", "").strip()
    password = request.form.get("password", "")

    user = User.query.filter(
        (User.username == identifier) | (User.email == identifier.lower())
    ).first()

    if not user or not user.check_password(password):
        flash("Invalid credentials. Please try again.", "error")
        return redirect(url_for("auth_page"))

    if REQUIRE_EMAIL_VERIFICATION and not user.email_verified:
        flash("Please verify your email before logging in. Check your inbox, or resend below.", "warning")
        return redirect(url_for("resend_verification_page", email=user.email))

    login_user(user)
    flash(f"Welcome back, {user.username}.", "success")
    return redirect_to_dashboard(user)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


# ----------------------------------------------------------------------------
# Email verification
# ----------------------------------------------------------------------------
@app.route("/verify_email/<token>")
def verify_email(token):
    email = verify_email_token(token)
    if not email:
        flash("That verification link is invalid or has expired. Please request a new one.", "error")
        return redirect(url_for("resend_verification_page"))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Account not found.", "error")
        return redirect(url_for("auth_page"))

    if not user.email_verified:
        user.email_verified = True
        db.session.commit()

    flash("Email verified! You can now log in.", "success")
    return redirect(url_for("auth_page"))


@app.route("/resend_verification", methods=["GET", "POST"])
def resend_verification_page():
    if request.method == "GET":
        prefill_email = request.args.get("email", "")
        return render_template("resend_verification.html", prefill_email=prefill_email)

    email = request.form.get("email", "").strip().lower()
    user = User.query.filter_by(email=email).first()

    # Same message whether or not the account exists, to avoid leaking which
    # emails are registered.
    generic_msg = "If that email is registered and unverified, a new link has been sent."

    if user and not user.email_verified:
        cooldown_ok = (
            not user.email_verification_sent_at
            or (datetime.utcnow() - user.email_verification_sent_at).total_seconds() > RESEND_COOLDOWN_SECONDS
        )
        if cooldown_ok:
            send_verification_email(user)
        else:
            flash("Please wait a bit before requesting another link.", "warning")
            return redirect(url_for("resend_verification_page"))

    flash(generic_msg, "success")
    return redirect(url_for("auth_page"))


# ----------------------------------------------------------------------------
# Routes: Vehicle Owner (Driver) Portal
# ----------------------------------------------------------------------------
@app.route("/user_dashboard")
@login_required
@role_required("vehicle_owner")
def user_dashboard():
    return render_template("user_dashboard.html", plug_types=PLUG_TYPES)


@app.route("/api/stations")
@login_required
def api_stations():
    plug_type = request.args.get("plug_type", "").strip()
    favorites_only = request.args.get("favorites_only") == "1"

    query = ChargingStation.query.filter_by(status="Online")
    if plug_type in PLUG_TYPES:
        query = query.filter_by(plug_type=plug_type)
    stations = query.all()

    if favorites_only:
        favorite_ids = {
            row.station_id
            for row in Favorite.query.filter_by(user_id=current_user.id).all()
        }
        stations = [s for s in stations if s.id in favorite_ids]

    return jsonify([s.to_dict(user_id=current_user.id) for s in stations])


@app.route("/api/station_slots")
@login_required
def api_station_slots():
    """Which time slots are already locked for a station on a given date."""
    expire_stale_bookings()

    station_id = request.args.get("station_id", type=int)
    booking_date_raw = request.args.get("booking_date", "")

    if not station_id:
        return jsonify({"taken_slots": []})

    try:
        booking_date_value = datetime.strptime(booking_date_raw, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"taken_slots": []})

    taken = [
        row.time_slot
        for row in SlotLock.query.filter_by(station_id=station_id, booking_date=booking_date_value).all()
    ]
    return jsonify({"taken_slots": taken})


@app.route("/route_planner")
@login_required
@role_required("vehicle_owner")
def route_planner():
    my_vehicles = Vehicle.query.filter_by(user_id=current_user.id).order_by(Vehicle.created_at.desc()).all()
    vehicles_data = [
        {"id": v.id, "name": v.name, "plug_type": v.plug_type, "full_range_km": v.full_range_km}
        for v in my_vehicles
    ]
    return render_template("route_planner.html", vehicles=vehicles_data, plug_types=PLUG_TYPES)



@app.route("/api/geocode")
@login_required
def api_geocode():
    """Proxy to OpenStreetMap Nominatim so the browser never needs a key."""
    query = request.args.get("q", "").strip()
    if len(query) < 3:
        return jsonify([])

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"format": "json", "q": query, "limit": 5},
            headers=NOMINATIM_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return jsonify({"error": "Geocoding service is unavailable right now. Please try again."}), 503

    results = [
        {"display_name": item.get("display_name", ""), "lat": float(item["lat"]), "lon": float(item["lon"])}
        for item in data
        if "lat" in item and "lon" in item
    ]
    return jsonify(results)


@app.route("/api/route")
@login_required
def api_route():
    """Proxy to the public OSRM driving-routing engine."""
    from_lat = request.args.get("from_lat", type=float)
    from_lng = request.args.get("from_lng", type=float)
    to_lat = request.args.get("to_lat", type=float)
    to_lng = request.args.get("to_lng", type=float)

    if None in (from_lat, from_lng, to_lat, to_lng):
        return jsonify({"error": "Missing or invalid coordinates."}), 400

    url = f"{OSRM_ROUTE_URL}/{from_lng},{from_lat};{to_lng},{to_lat}"
    try:
        resp = requests.get(url, params={"overview": "full", "geometries": "geojson"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return jsonify({"error": "Routing service is unavailable right now. Please try again."}), 503

    if data.get("code") != "Ok" or not data.get("routes"):
        return jsonify({"error": "No driving route could be found between these points."}), 404

    route = data["routes"][0]
    coordinates = [[lat, lng] for lng, lat in route["geometry"]["coordinates"]]
    return jsonify({
        "distance_km": round(route["distance"] / 1000.0, 2),
        "duration_min": round(route["duration"] / 60.0, 1),
        "coordinates": coordinates,
    })


@app.route("/api/stations_along_route", methods=["POST"])
@login_required
@role_required("vehicle_owner")
def api_stations_along_route():
    payload = request.get_json(silent=True) or {}
    coordinates = payload.get("coordinates")
    range_km = payload.get("range_km")
    plug_type = (payload.get("plug_type") or "").strip() or None

    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return jsonify({"error": "A valid route is required."}), 400

    try:
        range_km = float(range_km)
    except (TypeError, ValueError):
        return jsonify({"error": "Please enter a valid range in km."}), 400
    if range_km <= 0:
        return jsonify({"error": "Range must be greater than zero."}), 400

    try:
        coords = [(float(pt[0]), float(pt[1])) for pt in coordinates]
    except (TypeError, ValueError, IndexError):
        return jsonify({"error": "Malformed route coordinates."}), 400

    return jsonify(compute_route_recommendations(
        coords, range_km, plug_type=plug_type, user_id=current_user.id
    ))


# The route-recommendation endpoint only reads data (no DB writes) and is
# called from JS via fetch with a JSON body, so it's exempted from the
# form-based CSRF check like the other read-only /api/ endpoints.
csrf.exempt(api_stations_along_route)


# ----------------------------------------------------------------------------
# Favorites (Google-Maps-style saved stations)
# ----------------------------------------------------------------------------
@app.route("/api/favorites")
@login_required
def api_favorites():
    favorite_ids = [
        row.station_id for row in Favorite.query.filter_by(user_id=current_user.id).all()
    ]
    return jsonify({"favorite_ids": favorite_ids})


@app.route("/toggle_favorite/<int:station_id>", methods=["POST"])
@login_required
@role_required("vehicle_owner")
def toggle_favorite(station_id):
    station = db.session.get(ChargingStation, station_id)
    if not station:
        return jsonify({"error": "Station not found."}), 404

    existing = Favorite.query.filter_by(user_id=current_user.id, station_id=station_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({"favorited": False})

    try:
        db.session.add(Favorite(user_id=current_user.id, station_id=station_id))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # already favorited in a concurrent request — fine either way
    return jsonify({"favorited": True})


csrf.exempt(toggle_favorite)


# ----------------------------------------------------------------------------
# Ratings & Reviews
# ----------------------------------------------------------------------------
@app.route("/api/station_reviews")
@login_required
def api_station_reviews():
    station_id = request.args.get("station_id", type=int)
    if not station_id:
        return jsonify({"reviews": [], "avg_rating": None, "review_count": 0})

    station = db.session.get(ChargingStation, station_id)
    if not station:
        return jsonify({"reviews": [], "avg_rating": None, "review_count": 0})

    avg_rating, review_count = station.rating_summary()
    reviews = (
        Review.query.filter_by(station_id=station_id)
        .order_by(Review.created_at.desc())
        .limit(20)
        .all()
    )
    return jsonify({
        "avg_rating": avg_rating,
        "review_count": review_count,
        "reviews": [
            {
                "username": db.session.get(User, r.user_id).username,
                "rating": r.rating,
                "comment": r.comment,
                "created_at": r.created_at.strftime("%d %b %Y"),
            }
            for r in reviews
        ],
    })


@app.route("/submit_review", methods=["POST"])
@login_required
@role_required("vehicle_owner")
def submit_review():
    station_id = request.form.get("station_id", type=int)
    rating = request.form.get("rating", type=int)
    comment = request.form.get("comment", "").strip()

    station = db.session.get(ChargingStation, station_id) if station_id else None
    if not station:
        flash("Selected station could not be found.", "error")
        return redirect(url_for("history"))

    if rating is None or not (1 <= rating <= 5):
        flash("Please choose a rating between 1 and 5 stars.", "error")
        return redirect(url_for("history"))

    # Only allow reviewing a station the driver has actually had an approved
    # booking at, so reviews reflect genuine usage.
    has_approved_booking = (
        Booking.query.filter_by(user_id=current_user.id, station_id=station_id, status="Approved").first()
        is not None
    )
    if not has_approved_booking:
        flash("You can review a station only after a charging payment there has been approved.", "error")
        return redirect(url_for("history"))

    existing = Review.query.filter_by(user_id=current_user.id, station_id=station_id).first()
    if existing:
        existing.rating = rating
        existing.comment = comment or None
    else:
        db.session.add(Review(
            user_id=current_user.id, station_id=station_id, rating=rating, comment=comment or None,
        ))
    db.session.commit()

    flash("Thanks — your review has been saved.", "success")
    return redirect(url_for("history"))


# ----------------------------------------------------------------------------
# My Reservations — driver self-service cancellation
# ----------------------------------------------------------------------------
@app.route("/cancel_booking/<int:booking_id>", methods=["POST"])
@login_required
@role_required("vehicle_owner")
def cancel_booking(booking_id):
    booking = db.session.get(Booking, booking_id)
    if not booking or booking.user_id != current_user.id:
        abort(404)

    if booking.status not in ("Requested", "Accepted - Awaiting Payment", "Pending Verification"):
        flash("This reservation can no longer be cancelled from the driver portal.", "error")
        return redirect(url_for("history"))

    booking.status = "Cancelled"
    lock = SlotLock.query.filter_by(booking_id=booking.id).first()
    if lock:
        db.session.delete(lock)
    db.session.commit()

    flash("Reservation cancelled — the slot is now free for other drivers.", "success")
    return redirect(url_for("history"))


# ----------------------------------------------------------------------------
# Vehicle Profiles
# ----------------------------------------------------------------------------
@app.route("/vehicles")
@login_required
@role_required("vehicle_owner")
def vehicles():
    my_vehicles = Vehicle.query.filter_by(user_id=current_user.id).order_by(Vehicle.created_at.desc()).all()
    return render_template("vehicles.html", vehicles=my_vehicles, plug_types=PLUG_TYPES)


@app.route("/vehicles/add", methods=["POST"])
@login_required
@role_required("vehicle_owner")
def add_vehicle():
    name = request.form.get("name", "").strip()
    plug_type = request.form.get("plug_type", "")
    full_range_km = request.form.get("full_range_km", type=float)

    if not name:
        flash("Please give the vehicle a name.", "error")
        return redirect(url_for("vehicles"))
    if plug_type not in PLUG_TYPES:
        flash("Please select a valid plug type.", "error")
        return redirect(url_for("vehicles"))
    if full_range_km is None or full_range_km <= 0:
        flash("Please enter a valid full-charge range in km.", "error")
        return redirect(url_for("vehicles"))

    db.session.add(Vehicle(
        user_id=current_user.id, name=name, plug_type=plug_type, full_range_km=full_range_km,
    ))
    db.session.commit()
    flash(f"Vehicle '{name}' saved.", "success")
    return redirect(url_for("vehicles"))


@app.route("/vehicles/<int:vehicle_id>/delete", methods=["POST"])
@login_required
@role_required("vehicle_owner")
def delete_vehicle(vehicle_id):
    vehicle = db.session.get(Vehicle, vehicle_id)
    if not vehicle or vehicle.user_id != current_user.id:
        abort(404)
    db.session.delete(vehicle)
    db.session.commit()
    flash("Vehicle removed.", "success")
    return redirect(url_for("vehicles"))


@app.route("/request_booking", methods=["POST"])
@login_required
@role_required("vehicle_owner")
def request_booking():
    """Create a slot request. Payment is only submitted after the owner accepts."""
    expire_stale_bookings()
    station_id = request.form.get("station_id", type=int)
    booking_date_raw = request.form.get("booking_date", "").strip()
    time_slot = request.form.get("time_slot", "").strip()
    amount_paid = request.form.get("amount_paid", type=float)
    energy_kwh = request.form.get("energy_kwh", type=float)
    station = db.session.get(ChargingStation, station_id) if station_id else None
    if not station or station.status != "Online":
        flash("Selected charging station is unavailable.", "error")
        return redirect(url_for("user_dashboard"))
    try:
        booking_date_value = datetime.strptime(booking_date_raw, "%Y-%m-%d").date()
    except ValueError:
        flash("Please select a valid reservation date.", "error")
        return redirect(url_for("user_dashboard"))
    if booking_date_value < date.today():
        flash("Reservation date can't be in the past.", "error")
        return redirect(url_for("user_dashboard"))
    if not time_slot or amount_paid is None or amount_paid <= 0 or energy_kwh is None or energy_kwh <= 0:
        flash("Please provide a valid time slot and energy amount.", "error")
        return redirect(url_for("user_dashboard"))
    # Recalculate amount on the server so a modified browser value cannot set the price.
    expected_amount = round(station.price_per_kwh * energy_kwh, 2)
    if abs(expected_amount - amount_paid) > 0.05:
        amount_paid = expected_amount
    if db.session.query(SlotLock.id).filter_by(
        station_id=station.id, booking_date=booking_date_value, time_slot=time_slot
    ).first():
        flash("Sorry, that slot was just requested by another driver. Please pick another time.", "error")
        return redirect(url_for("user_dashboard"))
    booking = Booking(
        user_id=current_user.id, station_id=station.id,
        booking_date=booking_date_value, time_slot=time_slot,
        amount_paid=amount_paid, energy_kwh=energy_kwh,
        transaction_id="", payment_proof=None, status="Requested",
        verification_deadline=datetime.utcnow() + timedelta(hours=PENDING_VERIFICATION_TIMEOUT_HOURS),
    )
    db.session.add(booking)
    try:
        db.session.flush()
        db.session.add(SlotLock(station_id=station.id, booking_id=booking.id,
            booking_date=booking_date_value, time_slot=time_slot))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("That slot was just requested by another driver. Please choose another time.", "error")
        return redirect(url_for("user_dashboard"))
    flash("Booking request sent to the station owner. You can pay after it is accepted.", "success")
    return redirect(url_for("history"))


@app.route("/pay_booking/<int:booking_id>", methods=["POST"])
@login_required
@role_required("vehicle_owner")
def pay_booking(booking_id):
    """Submit payment reference and proof only for an owner-accepted booking."""
    booking = db.session.get(Booking, booking_id)
    if not booking or booking.user_id != current_user.id:
        abort(404)
    if booking.status != "Accepted - Awaiting Payment":
        flash("This booking is not awaiting payment.", "error")
        return redirect(url_for("history"))
    transaction_id = request.form.get("transaction_id", "").strip()
    proof_file = request.files.get("payment_proof")
    if not transaction_id or len(transaction_id) < 6 or len(transaction_id) > 64:
        flash("Please enter a valid UPI / UTR / transaction ID.", "error")
        return redirect(url_for("history"))
    try:
        stored_filename = save_payment_proof(proof_file)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("history"))
    if not stored_filename:
        flash("Please upload a payment proof screenshot.", "error")
        return redirect(url_for("history"))
    booking.transaction_id = transaction_id
    booking.payment_proof = stored_filename
    booking.status = "Pending Verification"
    booking.verification_deadline = datetime.utcnow() + timedelta(hours=PENDING_VERIFICATION_TIMEOUT_HOURS)
    db.session.commit()
    flash("Payment details submitted. Your station owner will verify them; the invoice is generated after approval.", "success")
    return redirect(url_for("history"))


@app.route("/history")
@login_required
@role_required("vehicle_owner")
def history():
    expire_stale_bookings()

    bookings = (
        Booking.query.filter_by(user_id=current_user.id)
        .order_by(Booking.created_at.desc())
        .all()
    )
    total_spend = sum(b.amount_paid for b in bookings if b.status == "Approved")

    my_reviews = Review.query.filter_by(user_id=current_user.id).all()
    reviewed_stations = {r.station_id: r for r in my_reviews}

    return render_template(
        "history.html",
        bookings=bookings,
        total_spend=total_spend,
        reviewed_stations=reviewed_stations,
    )


# ----------------------------------------------------------------------------
# Routes: Station Owner Portal
# ----------------------------------------------------------------------------
@app.route("/owner_dashboard")
@login_required
@role_required("station_owner")
def owner_dashboard():
    expire_stale_bookings()

    stations = ChargingStation.query.filter_by(owner_id=current_user.id).order_by(
        ChargingStation.created_at.desc()
    ).all()

    station_ids = [s.id for s in stations]
    pending_bookings = (
        Booking.query.filter(
            Booking.station_id.in_(station_ids), Booking.status == "Pending Verification"
        ).order_by(Booking.created_at.asc()).all()
        if station_ids else []
    )
    booking_requests = (
        Booking.query.filter(
            Booking.station_id.in_(station_ids), Booking.status == "Requested"
        ).order_by(Booking.created_at.asc()).all()
        if station_ids else []
    )
    confirmed_slot_bookings = (
        Booking.query.filter(
            Booking.station_id.in_(station_ids),
            Booking.status.in_(["Accepted - Awaiting Payment", "Pending Verification", "Approved"]),
        ).order_by(Booking.booking_date.asc(), Booking.time_slot.asc()).all()
        if station_ids else []
    )

    total_earnings = (
        db.session.query(func.coalesce(func.sum(Booking.amount_paid), 0.0))
        .filter(Booking.station_id.in_(station_ids), Booking.status == "Approved")
        .scalar()
        if station_ids else 0.0
    )
    total_bookings = (
        db.session.query(func.count(Booking.id))
        .filter(Booking.station_id.in_(station_ids))
        .scalar()
        if station_ids else 0
    )

    return render_template(
        "owner_dashboard.html",
        stations=stations,
        pending_bookings=pending_bookings,
        booking_requests=booking_requests,
        confirmed_slot_bookings=confirmed_slot_bookings,
        plug_types=PLUG_TYPES,
        total_earnings=total_earnings,
        total_bookings=total_bookings,
        active_station_count=sum(1 for s in stations if s.status == "Online"),
    )


@app.route("/add_station", methods=["POST"])
@login_required
@role_required("station_owner")
def add_station():
    if not current_user.kyc_approved:
        flash("Your KYC verification must be approved by an admin before you can add stations.", "error")
        return redirect(url_for("owner_dashboard"))

    name = request.form.get("name", "").strip()
    plug_type = request.form.get("plug_type", "")
    price_per_kwh = request.form.get("price_per_kwh", type=float)
    currency = request.form.get("currency", DEFAULT_CURRENCY)
    latitude = request.form.get("latitude", type=float)
    longitude = request.form.get("longitude", type=float)
    status = request.form.get("status", "Online")

    if not name or plug_type not in PLUG_TYPES:
        flash("Please provide a valid station name and plug type.", "error")
        return redirect(url_for("owner_dashboard"))

    if price_per_kwh is None or price_per_kwh <= 0:
        flash("Price per kWh must be a positive number.", "error")
        return redirect(url_for("owner_dashboard"))

    if currency not in CURRENCIES:
        currency = DEFAULT_CURRENCY

    if latitude is None or longitude is None or not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        flash("Please provide valid GPS coordinates.", "error")
        return redirect(url_for("owner_dashboard"))

    if status not in STATION_STATUSES:
        status = "Online"

    station = ChargingStation(
        name=name,
        plug_type=plug_type,
        price_per_kwh=price_per_kwh,
        currency=currency,
        latitude=latitude,
        longitude=longitude,
        status=status,
        owner_id=current_user.id,
    )
    db.session.add(station)
    db.session.commit()

    flash(f"Station '{name}' registered successfully.", "success")
    return redirect(url_for("owner_dashboard"))


@app.route("/toggle_station/<int:station_id>", methods=["POST"])
@login_required
@role_required("station_owner")
def toggle_station(station_id):
    station = db.session.get(ChargingStation, station_id)
    if not station or station.owner_id != current_user.id:
        abort(404)
    station.status = "Offline" if station.status == "Online" else "Online"
    db.session.commit()
    flash(f"Station '{station.name}' is now {station.status}.", "success")
    return redirect(url_for("owner_dashboard"))


@app.route("/respond_booking/<int:booking_id>", methods=["POST"])
@login_required
@role_required("station_owner")
def respond_booking(booking_id):
    booking = db.session.get(Booking, booking_id)
    if not booking:
        abort(404)
    station = db.session.get(ChargingStation, booking.station_id)
    if not station or station.owner_id != current_user.id:
        abort(403)
    if booking.status != "Requested":
        flash("This booking request has already been processed.", "warning")
        return redirect(url_for("owner_dashboard"))
    decision = request.form.get("decision")
    if decision == "accept":
        booking.status = "Accepted - Awaiting Payment"
        booking.verification_deadline = datetime.utcnow() + timedelta(hours=PENDING_VERIFICATION_TIMEOUT_HOURS)
        flash("Booking accepted. The EV driver can now submit payment.", "success")
    elif decision == "reject":
        booking.status = "Rejected"
        booking.rejection_reason = request.form.get("reason", "").strip() or "Station owner declined the request."
        lock = SlotLock.query.filter_by(booking_id=booking.id).first()
        if lock:
            db.session.delete(lock)
        flash("Booking request rejected and slot released.", "warning")
    else:
        flash("Invalid booking decision.", "error")
        return redirect(url_for("owner_dashboard"))
    db.session.commit()
    return redirect(url_for("owner_dashboard"))


@app.route("/invoice/<int:booking_id>")
@login_required
@role_required("vehicle_owner")
def booking_invoice(booking_id):
    booking = db.session.get(Booking, booking_id)
    if not booking or booking.user_id != current_user.id:
        abort(404)
    if booking.status != "Approved":
        flash("Your invoice will be available after the station owner verifies your payment.", "warning")
        return redirect(url_for("history"))
    invoice_number = f"VG-{booking.booking_date.strftime('%Y%m%d')}-{booking.id:06d}"
    return render_template("invoice.html", booking=booking, invoice_number=invoice_number,
                           generated_at=datetime.now())


@app.route("/invoice/<int:booking_id>/pdf")
@login_required
@role_required("vehicle_owner")
def booking_invoice_pdf(booking_id):
    """Download a formatted A4 PDF invoice after payment approval."""
    booking = db.session.get(Booking, booking_id)
    if not booking or booking.user_id != current_user.id:
        abort(404)
    if booking.status != "Approved":
        flash("Your PDF invoice is available after the station owner verifies your payment.", "warning")
        return redirect(url_for("history"))

    invoice_number = f"VG-{booking.booking_date.strftime('%Y%m%d')}-{booking.id:06d}"
    generated_at = datetime.now()
    currency = booking.station.currency or DEFAULT_CURRENCY
    # Use the currency code in the PDF so it renders consistently on Windows and Linux.
    money = lambda value: f"{currency} {value:,.2f}"
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=20*mm, leftMargin=20*mm,
        topMargin=18*mm, bottomMargin=18*mm,
        title=f"VoltGrid Invoice {invoice_number}", author="VoltGrid"
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="VGBrand", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=23, leading=27, textColor=colors.HexColor("#173b29"), alignment=TA_LEFT, spaceAfter=3))
    styles.add(ParagraphStyle(name="VGSub", parent=styles["Normal"], fontSize=9, leading=13, textColor=colors.HexColor("#607365")))
    styles.add(ParagraphStyle(name="VGSection", parent=styles["Heading2"], fontSize=10, leading=13, textColor=colors.HexColor("#234d35"), spaceBefore=8, spaceAfter=6))
    styles.add(ParagraphStyle(name="VGSmall", parent=styles["Normal"], fontSize=8, leading=11, textColor=colors.HexColor("#536357")))
    styles.add(ParagraphStyle(name="VGRight", parent=styles["Normal"], fontSize=9, leading=13, alignment=TA_RIGHT, textColor=colors.HexColor("#536357")))
    story = []
    brand = [[Paragraph("VOLTGRID", styles["VGBrand"]), Paragraph("<b>INVOICE / PAYMENT RECEIPT</b><br/>Invoice No: %s<br/>Issued: %s" % (invoice_number, generated_at.strftime("%d %b %Y, %I:%M %p")), styles["VGRight"])],
             [Paragraph("EV CHARGING NETWORK<br/>Digital booking and payment record", styles["VGSub"]), Paragraph("<font color='#187343'><b>PAYMENT VERIFIED</b></font>", styles["VGRight"])]]
    header = Table(brand, colWidths=[90*mm, 75*mm])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#f1f7ef")),
        ("BOX", (0,0), (-1,-1), 0.7, colors.HexColor("#d8e6d6")),
        ("LEFTPADDING", (0,0), (-1,-1), 12), ("RIGHTPADDING", (0,0), (-1,-1), 12),
        ("TOPPADDING", (0,0), (-1,-1), 11), ("BOTTOMPADDING", (0,0), (-1,-1), 11),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    story += [header, Spacer(1, 10*mm)]
    story.append(Paragraph("CUSTOMER & CHARGING DETAILS", styles["VGSection"]))
    details = [
        [Paragraph("<b>BILLED TO</b>", styles["VGSmall"]), Paragraph("<b>CHARGING STATION</b>", styles["VGSmall"])],
        [Paragraph("%s<br/>%s" % (xml_escape(booking.driver.username or ""), xml_escape(booking.driver.email or "")), styles["Normal"]), Paragraph("%s<br/>Connector: %s" % (xml_escape(booking.station.name or ""), xml_escape(booking.station.plug_type or "")), styles["Normal"])],
        [Paragraph("<b>BOOKING REFERENCE</b>", styles["VGSmall"]), Paragraph("<b>PAYMENT REFERENCE</b>", styles["VGSmall"])],
        [Paragraph("VG-BKG-%06d<br/>Booking date: %s<br/>Time slot: %s" % (booking.id, booking.booking_date.strftime("%d %b %Y"), xml_escape(booking.time_slot or "")), styles["Normal"]), Paragraph("Transaction ID: %s<br/>Status: Payment verified" % xml_escape(booking.transaction_id or "Not provided"), styles["Normal"])],
    ]
    detail_table = Table(details, colWidths=[82.5*mm, 82.5*mm], hAlign="LEFT")
    detail_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), colors.white), ("BOX", (0,0), (-1,-1), .5, colors.HexColor("#dce5dc")),
        ("INNERGRID", (0,0), (-1,-1), .35, colors.HexColor("#e4ebe3")),
        ("LEFTPADDING", (0,0), (-1,-1), 9), ("RIGHTPADDING", (0,0), (-1,-1), 9),
        ("TOPPADDING", (0,0), (-1,-1), 8), ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
    ]))
    story += [detail_table, Spacer(1, 8*mm), Paragraph("CHARGING CHARGES", styles["VGSection"])]
    charge_data = [
        ["Description", "Date / time slot", "Energy (kWh)", "Rate", "Amount"],
        ["EV charging slot reservation", booking.booking_date.strftime("%d %b %Y") + " / " + xml_escape(booking.time_slot or ""),
         f"{(booking.energy_kwh or 0):.2f}", money(booking.station.price_per_kwh), money(booking.amount_paid)],
    ]
    charges = Table(charge_data, colWidths=[43*mm, 38*mm, 25*mm, 28*mm, 31*mm], repeatRows=1)
    charges.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#214a32")), ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTSIZE", (0,0), (-1,-1), 8),
        ("LEADING", (0,0), (-1,-1), 11), ("GRID", (0,0), (-1,-1), .45, colors.HexColor("#dce5dc")),
        ("BACKGROUND", (0,1), (-1,1), colors.HexColor("#fbfcfa")),
        ("ALIGN", (2,1), (-1,-1), "RIGHT"), ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING", (0,0), (-1,-1), 7), ("RIGHTPADDING", (0,0), (-1,-1), 7),
        ("TOPPADDING", (0,0), (-1,-1), 10), ("BOTTOMPADDING", (0,0), (-1,-1), 10),
    ]))
    story += [charges, Spacer(1, 7*mm)]
    total_table = Table([["Subtotal", money(booking.amount_paid)], ["Tax", "Not separately itemized"], ["TOTAL PAID", money(booking.amount_paid)]], colWidths=[48*mm, 45*mm], hAlign="RIGHT")
    total_table.setStyle(TableStyle([
        ("ALIGN", (1,0), (1,-1), "RIGHT"), ("FONTNAME", (0,0), (-1,-1), "Helvetica"),
        ("FONTSIZE", (0,0), (-1,-1), 9), ("TEXTCOLOR", (0,0), (-1,-2), colors.HexColor("#536357")),
        ("BACKGROUND", (0,2), (-1,2), colors.HexColor("#eaf5e7")), ("TEXTCOLOR", (0,2), (-1,2), colors.HexColor("#173b29")),
        ("FONTNAME", (0,2), (-1,2), "Helvetica-Bold"), ("FONTSIZE", (0,2), (-1,2), 12),
        ("TOPPADDING", (0,0), (-1,-1), 8), ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("LINEABOVE", (0,2), (-1,2), .8, colors.HexColor("#b6d3b2")),
    ]))
    story += [total_table, Spacer(1, 12*mm), HRFlowable(width="100%", thickness=.6, color=colors.HexColor("#dce5dc")), Spacer(1, 4*mm),
              Paragraph("Thank you for charging with VoltGrid. This invoice was generated after the station owner verified the payment proof submitted through the platform.", styles["VGSmall"]),
              Paragraph("This is a platform-generated payment receipt. Taxes are not separately itemized, and this document should not be treated as a GST tax invoice unless supported by the relevant registered supplier details.", styles["VGSmall"])]
    def draw_page(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#c9d9c7"))
        canvas.line(20*mm, 13*mm, A4[0]-20*mm, 13*mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#718075"))
        canvas.drawString(20*mm, 8*mm, "VOLTGRID • EV CHARGING NETWORK")
        canvas.drawRightString(A4[0]-20*mm, 8*mm, f"Invoice {invoice_number}  |  Page {document.page}")
        canvas.restoreState()
    doc.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True,
                     download_name=f"VoltGrid-Invoice-{invoice_number}.pdf")


@app.route("/verify_payment/<int:booking_id>", methods=["POST"])
@login_required
@role_required("station_owner")
def verify_payment(booking_id):
    booking = db.session.get(Booking, booking_id)
    if not booking:
        abort(404)

    station = db.session.get(ChargingStation, booking.station_id)
    if not station or station.owner_id != current_user.id:
        abort(403)

    if booking.status != "Pending Verification":
        flash("Only submitted payment proofs can be verified.", "warning")
        return redirect(url_for("owner_dashboard"))

    decision = request.form.get("decision")
    reason = request.form.get("reason", "").strip()

    if decision == "approve":
        booking.status = "Approved"
        booking.rejection_reason = None
        flash("Payment approved.", "success")
    elif decision == "reject":
        booking.status = "Rejected"
        booking.rejection_reason = reason or "No reason provided."
        # Free the slot so another driver can book it.
        lock = SlotLock.query.filter_by(booking_id=booking.id).first()
        if lock:
            db.session.delete(lock)
        flash("Payment rejected. The slot has been released for other drivers.", "warning")
    else:
        flash("Invalid decision.", "error")
        return redirect(url_for("owner_dashboard"))

    db.session.commit()
    return redirect(url_for("owner_dashboard"))


# ----------------------------------------------------------------------------
# Static file serving for uploaded proofs (explicit, secure)
# ----------------------------------------------------------------------------
@app.route("/static/uploads/<path:filename>")
def uploaded_file(filename):
    safe_name = secure_filename(filename)
    return send_from_directory(app.config["UPLOAD_FOLDER"], safe_name)


@app.route("/static/kyc_uploads/<path:filename>")
@login_required
@role_required("admin")
def kyc_file(filename):
    """KYC documents are sensitive — only admins can view them."""
    safe_name = secure_filename(filename)
    return send_from_directory(KYC_UPLOAD_DIR, safe_name)


# ----------------------------------------------------------------------------
# Routes: Super Admin Panel
# ----------------------------------------------------------------------------
@app.route("/admin")
@login_required
@role_required("admin")
def admin_dashboard():
    expire_stale_bookings()

    total_drivers = User.query.filter_by(role="vehicle_owner").count()
    total_owners = User.query.filter_by(role="station_owner").count()
    total_stations = ChargingStation.query.count()
    total_bookings = Booking.query.count()
    pending_kyc_count = User.query.filter_by(kyc_status="Pending").count()
    pending_payments_count = Booking.query.filter_by(status="Pending Verification").count()
    unverified_email_count = User.query.filter_by(email_verified=False).count()

    return render_template(
        "admin_dashboard.html",
        total_drivers=total_drivers,
        total_owners=total_owners,
        total_stations=total_stations,
        total_bookings=total_bookings,
        pending_kyc_count=pending_kyc_count,
        pending_payments_count=pending_payments_count,
        unverified_email_count=unverified_email_count,
    )


@app.route("/admin/users")
@login_required
@role_required("admin")
def admin_users():
    role_filter = request.args.get("role", "")
    query = User.query.filter(User.role != "admin")
    if role_filter in ROLES:
        query = query.filter_by(role=role_filter)
    users = query.order_by(User.created_at.desc()).all()
    return render_template("admin_users.html", users=users, role_filter=role_filter)


@app.route("/admin/kyc/<int:user_id>/approve", methods=["POST"])
@login_required
@role_required("admin")
def admin_approve_kyc(user_id):
    user = db.session.get(User, user_id)
    if not user or user.role != "station_owner":
        abort(404)
    user.kyc_status = "Approved"
    user.kyc_rejection_reason = None
    db.session.commit()
    send_email(
        user.email, "VoltGrid: KYC Approved",
        f"Hi {user.username},\n\nYour station-owner KYC verification has been approved. "
        f"You can now register charging stations on VoltGrid.",
    )
    flash(f"KYC approved for {user.username}.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/kyc/<int:user_id>/reject", methods=["POST"])
@login_required
@role_required("admin")
def admin_reject_kyc(user_id):
    user = db.session.get(User, user_id)
    if not user or user.role != "station_owner":
        abort(404)
    reason = request.form.get("reason", "").strip()
    user.kyc_status = "Rejected"
    user.kyc_rejection_reason = reason or "No reason provided."
    db.session.commit()
    send_email(
        user.email, "VoltGrid: KYC Rejected",
        f"Hi {user.username},\n\nYour station-owner KYC verification was rejected.\n"
        f"Reason: {user.kyc_rejection_reason}\n\nYou can contact support or resubmit.",
    )
    flash(f"KYC rejected for {user.username}.", "warning")
    return redirect(url_for("admin_users"))


@app.route("/admin/stations")
@login_required
@role_required("admin")
def admin_stations():
    stations = ChargingStation.query.order_by(ChargingStation.created_at.desc()).all()
    return render_template("admin_stations.html", stations=stations)


@app.route("/admin/bookings")
@login_required
@role_required("admin")
def admin_bookings():
    expire_stale_bookings()
    bookings = Booking.query.order_by(Booking.created_at.desc()).limit(200).all()
    return render_template("admin_bookings.html", bookings=bookings)


# ----------------------------------------------------------------------------
# Error handlers
# ----------------------------------------------------------------------------
@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="You don't have permission to access this page."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="The page you're looking for doesn't exist."), 404


@app.errorhandler(413)
def too_large(e):
    return render_template("error.html", code=413, message="Uploaded file is too large. Maximum size is 5 MB."), 413


@app.errorhandler(500)
def server_error(e):
    db.session.rollback()
    return render_template("error.html", code=500, message="Something went wrong on our end."), 500


# ----------------------------------------------------------------------------
# CLI-free bootstrap: create tables on first run, safely patch older DBs
# ----------------------------------------------------------------------------
def run_light_migrations():
    """
    Safely upgrade an existing VoltGrid SQLite database.
    Adds missing columns without dropping existing users, stations,
    bookings, password hashes, or payment information.
    """

    table_names = set(inspect(db.engine).get_table_names())

    # ---------------------------------------------------------
    # BOOKINGS TABLE
    # ---------------------------------------------------------
    if "bookings" in table_names:
        columns = {
            c["name"]
            for c in inspect(db.engine).get_columns("bookings")
        }

        with db.engine.begin() as conn:

            if "booking_date" not in columns:
                conn.execute(text(
                    "ALTER TABLE bookings ADD COLUMN booking_date DATE"
                ))
                conn.execute(text(
                    "UPDATE bookings SET booking_date = date('now') "
                    "WHERE booking_date IS NULL"
                ))

            if "energy_kwh" not in columns:
                conn.execute(text(
                    "ALTER TABLE bookings ADD COLUMN energy_kwh FLOAT"
                ))

            if "verification_deadline" not in columns:
                conn.execute(text(
                    "ALTER TABLE bookings ADD COLUMN "
                    "verification_deadline DATETIME"
                ))
                conn.execute(text(
                    "UPDATE bookings SET verification_deadline = "
                    "datetime('now', '+"
                    + str(PENDING_VERIFICATION_TIMEOUT_HOURS)
                    + " hours') "
                    "WHERE status IN "
                    "('Requested', 'Accepted - Awaiting Payment', "
                    "'Pending Verification') "
                    "AND verification_deadline IS NULL"
                ))

    # ---------------------------------------------------------
    # CHARGING STATIONS TABLE
    # ---------------------------------------------------------
    if "charging_stations" in table_names:
        columns = {
            c["name"]
            for c in inspect(db.engine).get_columns(
                "charging_stations"
            )
        }

        with db.engine.begin() as conn:
            if "currency" not in columns:
                conn.execute(text(
                    "ALTER TABLE charging_stations "
                    "ADD COLUMN currency VARCHAR(3) DEFAULT '"
                    + DEFAULT_CURRENCY + "'"
                ))

            conn.execute(text(
                "UPDATE charging_stations SET currency = '"
                + DEFAULT_CURRENCY
                + "' WHERE currency IS NULL OR currency = ''"
            ))

    # ---------------------------------------------------------
    # USERS TABLE
    # ---------------------------------------------------------
    if "users" in table_names:
        columns = {
            c["name"]
            for c in inspect(db.engine).get_columns("users")
        }

        # Existing users are grandfathered as verified if this
        # column did not exist before the migration.
        email_verification_column_was_missing = (
            "email_verified" not in columns
        )

        user_column_additions = {
            "email_verified": "BOOLEAN NOT NULL DEFAULT 1",
            "email_verification_sent_at": "DATETIME",
            "kyc_status": "VARCHAR(20) DEFAULT 'Not Required'",
            "kyc_document": "VARCHAR(255)",
            "kyc_rejection_reason": "VARCHAR(255)",
        }

        with db.engine.begin() as conn:
            for column_name, column_definition in (
                user_column_additions.items()
            ):
                if column_name not in columns:
                    conn.execute(text(
                        f"ALTER TABLE users ADD COLUMN "
                        f"{column_name} {column_definition}"
                    ))

            # Preserve login access for accounts that existed before
            # email verification was introduced.
            if email_verification_column_was_missing:
                conn.execute(text(
                    "UPDATE users SET email_verified = 1"
                ))

            # Initialize KYC state for older accounts only when
            # the KYC column did not previously exist.
            if "kyc_status" not in columns:
                conn.execute(text(
                    "UPDATE users SET kyc_status = 'Approved' "
                    "WHERE role = 'station_owner'"
                ))
                conn.execute(text(
                    "UPDATE users SET kyc_status = 'Not Required' "
                    "WHERE role != 'station_owner'"
                ))

    # ---------------------------------------------------------
    # OTHER TABLES
    # ---------------------------------------------------------
    # db.create_all() below creates missing tables such as
    # slot_locks, favorites, reviews, and vehicles.
    # It does not add missing columns to existing tables.


with app.app_context():
    run_light_migrations()
    db.create_all()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
