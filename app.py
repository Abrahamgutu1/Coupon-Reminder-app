from flask import (
    Flask, render_template, request, redirect,
    url_for, send_file, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
import os
import io
import secrets
import qrcode

# ---------------------------------------------------------------------
# App + DB setup
# ---------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "coupons_real.db")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------
class Offer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    restaurant = db.Column(db.String(150), nullable=False)
    description = db.Column(db.String(300), nullable=False)
    expires = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CouponCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    offer_id = db.Column(db.Integer, db.ForeignKey("offer.id"), nullable=True)
    restaurant = db.Column(db.String(150), nullable=False)
    description = db.Column(db.String(300), nullable=False)
    code = db.Column(db.String(64), unique=True, nullable=False)
    expires = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    redeemed_at = db.Column(db.DateTime, nullable=True)
    redeemed_by = db.Column(db.String(150), nullable=True)

    def is_expired(self):
        return date.today() > self.expires

    def is_redeemed(self):
        return self.redeemed_at is not None

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def generate_code(prefix="COUP", length=8):
    token = secrets.token_hex(length // 2).upper()[:length]
    return f"{prefix}-{token}"

def qr_bytes_for_text(text):
    img = qrcode.make(data=text)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# ---------------------------------------------------------------------
# Create DB and seed example (Flask 3.1 safe)
# ---------------------------------------------------------------------
with app.app_context():
    db.create_all()
    if Offer.query.count() == 0:
        sample = Offer(
            restaurant="Chipotle",
            description="Free chips",
            expires=datetime.strptime("2025-11-05", "%Y-%m-%d").date()
        )
        db.session.add(sample)
        db.session.commit()

# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.route("/")
def home():
    q = request.args.get("restaurants", "").strip()
    if q:
        # if user searched from home, filter
        offers = Offer.query.filter(Offer.restaurant.ilike(f"%{q}%")).order_by(Offer.created_at.desc()).all()
    else:
        offers = Offer.query.order_by(Offer.created_at.desc()).all()

    recent = CouponCode.query.order_by(CouponCode.created_at.desc()).limit(25).all()
    return render_template("home.html", offers=offers, recent=recent, search_term=q)

@app.route("/search")
def search():
    """
    /search?restaurant=Chipotle
    If found → show matching offers
    If not found → show link to create offer with that name prefilled
    """
    name = (request.args.get("restaurant") or "").strip()
    offers = []
    if name:
        offers = Offer.query.filter(Offer.restaurant.ilike(f"%{name}%")).order_by(Offer.created_at.desc()).all()
    return render_template("search.html", offers=offers, search_term=name)

@app.route("/create_offer", methods=["GET", "POST"])
def create_offer():
    if request.method == "POST":
        restaurant = request.form["restaurant"].strip()
        description = request.form["description"].strip()
        expires_str = request.form["expires"].strip()

        if not (restaurant and description and expires_str):
            return "All fields required", 400

        try:
            expires = datetime.strptime(expires_str, "%Y-%m-%d").date()
        except ValueError:
            return "Date must be YYYY-MM-DD", 400

        offer = Offer(
            restaurant=restaurant,
            description=description,
            expires=expires,
        )
        db.session.add(offer)
        db.session.commit()
        return redirect(url_for("home"))
    # GET → maybe prefill from search
    prefill_restaurant = request.args.get("restaurant", "")
    return render_template("create_offer.html", restaurant=prefill_restaurant)

@app.route("/claim/<int:offer_id>", methods=["POST"])
def claim_offer(offer_id):
    offer = Offer.query.get_or_404(offer_id)
    prefix = offer.restaurant[:4].upper()
    code = None
    for _ in range(10):
        candidate = generate_code(prefix=prefix, length=10)
        if not CouponCode.query.filter_by(code=candidate).first():
            code = candidate
            break
    coupon = CouponCode(
        offer_id=offer.id,
        restaurant=offer.restaurant,
        description=offer.description,
        code=code,
        expires=offer.expires
    )
    db.session.add(coupon)
    db.session.commit()
    return jsonify({
        "ok": True,
        "code": coupon.code,
        "view_url": url_for("view_coupon", code=coupon.code, _external=True),
        "qr_url": url_for("coupon_qr", code=coupon.code, _external=True),
        "expires": coupon.expires.isoformat()
    })

@app.route("/coupon/<code>")
def view_coupon(code):
    c = CouponCode.query.filter_by(code=code).first_or_404()
    return render_template("view_coupon.html", coupon=c)

@app.route("/coupon/<code>/qr.png")
def coupon_qr(code):
    c = CouponCode.query.filter_by(code=code).first_or_404()
    bio = qr_bytes_for_text(c.code)
    return send_file(bio, mimetype="image/png")

@app.route("/redeem", methods=["POST"])
def redeem():
    code = (request.form.get("code") or "").strip().upper()
    who = (request.form.get("redeemed_by") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "missing code"}), 400
    c = CouponCode.query.filter_by(code=code).first()
    if not c:
        return jsonify({"ok": False, "error": "code not found"}), 404
    if c.is_expired():
        return jsonify({"ok": False, "error": "expired"}), 410
    if c.is_redeemed():
        return jsonify({"ok": False, "error": "already redeemed"}), 409
    c.redeemed_at = datetime.utcnow()
    if who:
        c.redeemed_by = who
    db.session.commit()
    return jsonify({"ok": True, "code": c.code})

# ---------------------------------------------------------------------
# Templates auto-create if missing
# ---------------------------------------------------------------------
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
os.makedirs(TEMPLATE_DIR, exist_ok=True)

# home.html
open(os.path.join(TEMPLATE_DIR, "home.html"), "w").write("""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Coupons</title>
  <style>
    body{font-family:sans-serif;max-width:800px;margin:2rem auto}
    .offer{border:1px solid #ccc;border-radius:8px;padding:10px;margin-bottom:10px}
    button{padding:.4rem .6rem}
    .searchbox{margin-bottom:1rem}
    .modal{position:fixed;inset:0;background:rgba(0,0,0,0.5);display:none;align-items:center;justify-content:center}
    .card{background:#fff;padding:20px;border-radius:10px;max-width:420px}
  </style>
</head>
<body>
  <h1>Available Offers</h1>

  <form class="searchbox" method="get" action="/">
    <input name="restaurant" placeholder="Search restaurant..." value="{{search_term or ''}}">
    <button type="submit">Search</button>
    <a href="/">Clear</a>
  </form>

  <p><a href="/create_offer">+ Create new offer</a></p>

  {% if offers %}
    {% for o in offers %}
      <div class="offer">
        <strong>{{o.restaurant}}</strong> — {{o.description}}<br>
        Expires: {{o.expires}}<br><br>
        <button onclick="claim({{o.id}},this)">Claim coupon</button>
      </div>
    {% endfor %}
  {% else %}
    <p>No offers match that restaurant. <a href="/create_offer?restaurant={{search_term}}">Create one?</a></p>
  {% endif %}

  <h2>Recently generated coupons</h2>
  <ul>
    {% for c in recent %}
      <li><a href="/coupon/{{c.code}}">{{c.code}}</a> — {{c.restaurant}} — {{c.description}}</li>
    {% endfor %}
  </ul>

  <div id="modal" class="modal">
    <div class="card" id="modalCard"></div>
  </div>

<script>
async function claim(id,btn){
  btn.disabled=true;btn.textContent='Claiming...';
  const resp = await fetch('/claim/'+id, {method:'POST'});
  const j = await resp.json();
  if(!j.ok){
    alert('Error: ' + (j.error || 'could not create'));
    btn.disabled=false;btn.textContent='Claim coupon';
    return;
  }
  document.getElementById('modalCard').innerHTML = `
    <h3>Coupon created ✅</h3>
    <p><strong>Code:</strong> ${j.code}</p>
    <p><img src="${j.qr_url}" width="200"></p>
    <p>Expires: ${j.expires}</p>
    <p><a href="${j.view_url}" target="_blank">Open coupon page</a></p>
    <p><button onclick="closeModal()">Close</button></p>
  `;
  document.getElementById('modal').style.display='flex';
  btn.disabled=false;btn.textContent='Claim coupon';
}
function closeModal(){
  document.getElementById('modal').style.display='none';
}
document.getElementById('modal').addEventListener('click', function(e){
  if(e.target.id === 'modal'){ closeModal(); }
});
</script>
</body>
</html>
""")

# search.html (optional separate page)
open(os.path.join(TEMPLATE_DIR, "search.html"), "w").write("""<!doctype html>
<html><head><meta charset="utf-8"><title>Search</title></head>
<body>
<h1>Search results for "{{search_term}}"</h1>
<p><a href="/">Back to home</a></p>
{% if offers %}
  {% for o in offers %}
    <div>
      <strong>{{o.restaurant}}</strong> — {{o.description}} — {{o.expires}}
      <form method="post" action="/claim/{{o.id}}" style="display:inline">
        <button type="submit">Claim</button>
      </form>
    </div>
  {% endfor %}
{% else %}
  <p>No offers found. <a href="/create_offer?restaurant={{search_term}}">Create one for "{{search_term}}"</a></p>
{% endif %}
</body></html>
""")

# create_offer.html
open(os.path.join(TEMPLATE_DIR, "create_offer.html"), "w").write("""<!doctype html>
<html><head><meta charset="utf-8"><title>Create Offer</title></head>
<body>
<h1>Create Offer</h1>
<form method="POST">
  Restaurant: <input name="restaurant" value="{{restaurant or ''}}" required><br>
  Description: <input name="description" placeholder="e.g. Free chips" required><br>
  Expires (YYYY-MM-DD): <input name="expires" required><br>
  <button type="submit">Save</button>
</form>
<p><a href="/">Back</a></p>
</body></html>
""")

# view_coupon.html
open(os.path.join(TEMPLATE_DIR, "view_coupon.html"), "w").write("""<!doctype html>
<html><head><meta charset="utf-8"><title>Coupon {{coupon.code}}</title></head>
<body>
<h1>{{coupon.restaurant}} — {{coupon.description}}</h1>
<p>Code: <strong>{{coupon.code}}</strong></p>
<p>Expires: {{coupon.expires}}</p>
<img src="/coupon/{{coupon.code}}/qr.png" width="200">
<form method="POST" action="/redeem">
  <input name="code" value="{{coupon.code}}" readonly>
  Redeemed by: <input name="redeemed_by">
  <button type="submit">Redeem</button>
</form>
<p><a href="/">Back</a></p>
</body></html>
""")

# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------
if __name__ == "__main__":
    # you already had 5000 in use so we stay on 5001
    app.run(debug=True, port=5001)
