"""Campfire CEO Dashboard: a read-only Campfire OAuth app.

Routes
  /connect                    send the user to Campfire to approve read-only access
  /callback                   receive the OAuth code and store the token
  /healthz                    health check for Render
  /manage                     connection status (the manifest's manage_url)
  /frame/overview             KPI summary
  /frame/revenue              Revenue & Profitability
  /frame/cash                 Cash & Runway
  /frame/working-capital      Working Capital

Configuration comes only from environment variables: CLIENT_ID, CLIENT_SECRET,
SIGNING_SECRET, PUBLIC_BASE_URL, FLASK_SECRET, DB_PATH (plus optional
DEMO_MODE, CAMPFIRE_API_BASE, FRAME_ANCESTORS).
"""

from __future__ import annotations

import logging
import os
import secrets

import jwt
import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import campfire
import db
from demo_data import build_demo_dataset
from metrics import compute

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dashboard")

PAGES = [
    ("overview", "Overview"),
    ("revenue", "Revenue & Profitability"),
    ("cash", "Cash & Runway"),
    ("working-capital", "Working Capital"),
]
PAGE_TEMPLATES = {
    "overview": "overview.html",
    "revenue": "revenue.html",
    "cash": "cash.html",
    "working-capital": "working_capital.html",
}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def create_app() -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    flask_secret = os.environ.get("FLASK_SECRET")
    if not flask_secret:
        log.warning("FLASK_SECRET is not set; using a random per-process key.")
        flask_secret = secrets.token_hex(32)
    app.secret_key = flask_secret
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    db.init_db()

    frame_ancestors = os.environ.get("FRAME_ANCESTORS", "https://app.meetcampfire.com").split()

    @app.after_request
    def security_headers(resp):
        # Let Campfire show our pages in its frame; refuse everyone else.
        resp.headers.pop("X-Frame-Options", None)
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'self' " + " ".join(frame_ancestors)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith(("/frame/", "/callback", "/manage")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # --- template helpers ---------------------------------------------------

    @app.template_filter("money")
    def money(value, compact=True):
        if value is None:
            return "–"
        sign = "-" if value < 0 else ""
        v = abs(value)
        if compact and v >= 1_000_000:
            return f"{sign}${v / 1_000_000:.1f}M"
        if compact and v >= 10_000:
            return f"{sign}${v / 1_000:.0f}K"
        return f"{sign}${v:,.0f}"

    @app.template_filter("pct")
    def pct(value, digits=1, signed=False):
        if value is None:
            return "–"
        return f"{value * 100:+.{digits}f}%" if signed else f"{value * 100:.{digits}f}%"

    @app.template_filter("num")
    def num(value, digits=1):
        return "–" if value is None else f"{value:,.{digits}f}"

    @app.template_filter("month")
    def month(value):
        import calendar
        y, m = value.split("-")[:2]
        return f"{calendar.month_abbr[int(m)]} {y}"

    @app.context_processor
    def inject_globals():
        return {"PAGES": PAGES}

    def message(title: str, body: str, status: int = 200, **extra):
        return render_template("message.html", title=title, body=body, **extra), status

    def demo_enabled() -> bool:
        return _truthy(os.environ.get("DEMO_MODE")) or _truthy(request.args.get("demo"))

    # --- routes -------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        try:
            with db.connect() as conn:
                conn.execute("SELECT 1")
        except Exception:  # noqa: BLE001 - report unhealthy, never crash the probe
            log.exception("Health check failed")
            return jsonify(status="error"), 503
        return jsonify(status="ok")

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            configured=all(os.environ.get(k) for k in ("CLIENT_ID", "CLIENT_SECRET", "PUBLIC_BASE_URL")),
            demo_mode=_truthy(os.environ.get("DEMO_MODE")),
            scopes=campfire.SCOPES,
        )

    @app.get("/connect")
    def connect():
        missing = [k for k in ("CLIENT_ID", "CLIENT_SECRET", "PUBLIC_BASE_URL") if not os.environ.get(k)]
        if missing:
            return message("App not configured",
                           "Set these on the Render service's Environment page: " + ", ".join(missing),
                           status=500)
        customer_id = None
        if request.args.get("cf_ctx"):
            try:
                customer_id = campfire.verify_cf_ctx(request.args["cf_ctx"])["customer_id"]
            except (jwt.InvalidTokenError, campfire.CampfireError):
                customer_id = None  # still allow connecting; claimed on first page view
        state = secrets.token_urlsafe(32)
        db.save_state(state, customer_id)
        return redirect(campfire.authorize_url(state))

    @app.get("/callback")
    def callback():
        if request.args.get("error"):
            return message("Connection not completed",
                           f"Campfire returned: {request.args.get('error')}. "
                           "Check the app's scopes and redirect URL, then try Connect again.",
                           status=400)
        state = request.args.get("state", "")
        code = request.args.get("code", "")
        row = db.pop_state(state) if state else None
        if row is None or not code:
            return message("Connection link expired",
                           "This sign-in link is invalid or older than 10 minutes. Start again from Connect.",
                           status=400)
        try:
            token = campfire.exchange_code(code)
        except (campfire.CampfireError, requests.RequestException) as exc:
            log.warning("Code exchange failed: %s", exc)
            return message("Couldn't finish connecting",
                           "Campfire didn't accept the sign-in. Check CLIENT_ID, CLIENT_SECRET and "
                           "that PUBLIC_BASE_URL/callback is in the app's redirect URIs.",
                           status=502)
        customer_id = campfire.customer_id_from_token(token) or row["customer_id"] or db.DEFAULT_CUSTOMER
        db.save_connection(customer_id, token)
        campfire.clear_cache(customer_id)
        log.info("Connected workspace %s", customer_id)
        return message("Connected to Campfire",
                       "The dashboard can now read this workspace's reports, accounts, invoices and "
                       "bills (read-only). Open it from 3rd Party Apps in Campfire's left navigation.")

    @app.get("/manage")
    def manage():
        claims = None
        conn = None
        if request.args.get("cf_ctx"):
            try:
                claims = campfire.verify_cf_ctx(request.args["cf_ctx"])
                conn = db.get_connection(claims["customer_id"])
            except (jwt.InvalidTokenError, campfire.CampfireError):
                claims = None
        return render_template("manage.html", claims=claims, conn=conn, scopes=campfire.SCOPES)

    @app.post("/disconnect")
    def disconnect():
        try:
            claims = campfire.verify_cf_ctx(request.form.get("cf_ctx", ""))
        except (jwt.InvalidTokenError, campfire.CampfireError):
            abort(403)
        db.delete_connection(claims["customer_id"])
        campfire.clear_cache(claims["customer_id"])
        return message("Disconnected",
                       "The stored Campfire token for this workspace was deleted. "
                       "To revoke the grant itself, disconnect the app in Campfire as well.")

    @app.get("/frame/<page>")
    def frame(page: str):
        if page not in PAGE_TEMPLATES:
            abort(404)
        cf_ctx = request.args.get("cf_ctx")
        nav_args = {"cf_ctx": cf_ctx} if cf_ctx else {}

        # Demo mode (DEMO_MODE env or ?demo=1): sample data, no Campfire calls.
        if demo_enabled():
            return render_template(
                PAGE_TEMPLATES[page],
                page=page,
                m=compute(build_demo_dataset()),
                nav_args={**nav_args, "demo": "1"},
            )

        claims = None
        if cf_ctx:
            try:
                claims = campfire.verify_cf_ctx(cf_ctx)
            except campfire.CampfireError as exc:
                return message("App not configured", f"{exc}. Add it on the Render Environment page.", status=500)
            except jwt.ExpiredSignatureError:
                return message("Session expired", "Reload this page from Campfire's left navigation.", status=401)
            except jwt.InvalidTokenError:
                return message("Couldn't verify Campfire",
                               "The page's sign-in token didn't verify. If this keeps happening, rotate the "
                               "app's signing secret in Campfire and paste the new value into SIGNING_SECRET.",
                               status=401)

        if claims is None:
            return message("Open this page from Campfire",
                           "This page shows live data only inside Campfire (3rd Party Apps in the left "
                           "navigation).",
                           demo_link=url_for("frame", page=page, demo=1))
        else:
            customer_id = claims["customer_id"]
            if db.get_connection(customer_id) is None and db.claim_default_connection(customer_id) is None:
                return message("Connect to Campfire",
                               "Approve read-only access so the dashboard can read your reports.",
                               connect_link=url_for("connect", cf_ctx=cf_ctx))
            try:
                data = campfire.live_dataset(customer_id)
            except campfire.NotConnected:
                return message("Reconnect to Campfire",
                               "The saved access has expired or was revoked. Approve access again.",
                               connect_link=url_for("connect", cf_ctx=cf_ctx))
            except (campfire.CampfireError, requests.RequestException, ValueError) as exc:
                log.warning("Live data failed for %s: %s", customer_id, exc)
                return message("Couldn't load data from Campfire", str(exc), status=502)

        return render_template(
            PAGE_TEMPLATES[page],
            page=page,
            m=compute(data),
            nav_args=nav_args,
        )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
