"""Synthetic demo data for the Acme Field Services spendcoder template.

Everything written here is invented: the company, its departments, cardholder
memos, amounts and dates. Merchant names are generic public brands or obviously
fake local businesses. Nothing is copied from a real card program.

Regenerate (deterministic: random.Random(42), standard library only) from the
public/ folder:

    .venv\\Scripts\\python.exe scripts\\make_synthetic_data.py

Writes src/spendcoder/templates/acme/ (policy/, data/, spendcoder.toml, README.md).
Re-running overwrites those files with identical content.
"""
from __future__ import annotations

import csv
import random
import re
from dataclasses import dataclass
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "src/spendcoder/templates/acme"

SEED = 42
N_TRANSACTIONS = 240
N_LABELED = 120
EMPTY_MEMO_RATE = 0.35
TEAM_KW = "team lunch, all hands, happy hour, HH"
DISPUTE_MEMO = "please dispute - not my charge"
PERSONAL_MEMO = "NOT for team lunch - personal, will repay"
DEPARTMENTS = ("Field Ops", "Sales", "Engineering", "Admin", "People")
ISSUER_LABELS = ("Dining", "Travel", "Merchandise", "Groceries", "Utilities", "Software",
                 "Automotive", "Office", "Services", "Gifts", "Entertainment", "Misc")

# Issuer's own (often wrong) label, used roughly 40% of the time. Never read by the tool.
ISSUER_HINT = {
    "Restaurant": "Dining", "Coffee shop": "Dining", "Catering": "Dining",
    "Grocery": "Groceries", "Airline": "Travel", "Hotel": "Travel", "Rideshare": "Travel",
    "Rental car": "Travel", "Parking": "Automotive", "Fuel": "Automotive",
    "Office supplies": "Office", "Electronics": "Merchandise", "Computer software": "Software",
    "Subscriptions": "Software", "Hardware store": "Merchandise", "Plumbing supplies": "Merchandise",
    "HVAC parts": "Merchandise", "Uniforms": "Merchandise", "Shipping": "Services",
    "Training and certification": "Services", "Team events": "Entertainment",
    "Gifts and recognition": "Gifts", "Utilities": "Utilities", "Donations": "Misc",
}

CATEGORIES = [
    ("Restaurant", "Sit-down and takeout restaurant meals"),
    ("Coffee shop", "Coffee and tea shops"),
    ("Grocery", "Supermarkets and grocery stores"),
    ("Catering", "Catered food for events"),
    ("Airline", "Airfare for business travel"),
    ("Hotel", "Lodging while traveling"),
    ("Rideshare", "Uber, Lyft and similar rides"),
    ("Rental car", "Vehicle rentals"),
    ("Parking", "Parking garages and meters"),
    ("Fuel", "Gas and fuel stations"),
    ("Office supplies", "Paper, pens and desk items"),
    ("Computer software", "Software licenses and apps"),
    ("Electronics", "Electronic devices and accessories"),
    ("Hardware store", "Tools and general hardware"),
    ("Plumbing supplies", "Pipe, fittings and valves"),
    ("HVAC parts", "Furnace, AC and duct parts"),
    ("Shipping", "Parcels and freight"),
    ("Training and certification", "Courses, exams and certifications"),
    ("Team events", "Crew and company activities"),
    ("Gifts and recognition", "Gifts and awards for staff"),
    ("Subscriptions", "Recurring service fees"),
    ("Utilities", "Power, water and phone bills"),
    ("Uniforms", "Work apparel and boots"),
    ("Donations", "Charitable contributions"),
]

GL_NAMES = {
    "610100": "Meals",
    "610200": "Client Meals",
    "610300": "Team Meals",
    "610400": "Catering",
    "620100": "Airfare",
    "620200": "Lodging",
    "620300": "Ground Transportation",
    "620400": "Parking",
    "630100": "Vehicle Fuel",
    "630200": "Vehicle Rental",
    "640100": "Office Supplies",
    "640200": "Software",
    "640210": "Software - Business",
    "640300": "Subscriptions and Memberships",
    "640400": "Equipment",
    "640500": "Employee Recognition",
    "640600": "Gifts",
    "640700": "Client Gifts",
    "650100": "Break Room Supplies",
    "650200": "Job Materials - General",
    "650300": "Job Materials - Plumbing",
    "650400": "Job Materials - HVAC",
    "660100": "Shipping and Postage",
    "660150": "Freight and Logistics",
    "660200": "Training and Certification",
    "660300": "Team Events",
    "660320": "Employee Engagement",
    "660400": "Utilities",
    "660500": "Uniforms and PPE",
    "660600": "Donations",
}

# (category, department, excluded_departments, amount_range, keywords, gl_code)
RULES = [
    ("Restaurant", "Sales", "", "", "", "610200"),
    ("Restaurant", "", "Sales", "", "", "610100"),
    ("Restaurant", "", "", "", TEAM_KW, "610300"),
    ("Coffee shop", "", "", "", "", "610100"),
    ("Grocery", "", "", "", "", "650100"),
    ("Grocery", "", "", "", TEAM_KW, "610300"),
    ("Catering", "Sales", "", "", "", "610200"),
    ("Catering", "", "Sales", "", "", "610400"),
    ("Airline", "", "", "", "", "620100"),
    ("Hotel", "", "", "", "", "620200"),
    ("Rideshare", "", "", "", "", "620300"),
    ("Rental car", "", "", "", "", "630200"),
    ("Parking", "", "", "", "", "620400"),
    ("Fuel", "Field Ops", "", "", "", "630100"),
    ("Fuel", "", "Field Ops", "", "", "620300"),
    ("Office supplies", "", "", "", "", "640100"),
    ("Electronics", "", "", "0-74.99", "", "640100"),
    ("Electronics", "", "", "75-100000", "", "640400"),
    ("Computer software", "Engineering", "", "", "", "640200"),
    ("Computer software", "", "Engineering", "", "", "640210"),
    ("Hardware store", "", "", "", "", "650200"),
    ("Plumbing supplies", "Field Ops", "", "", "", "650300"),
    ("Plumbing supplies", "", "Field Ops", "", "", "650200"),
    ("HVAC parts", "Field Ops", "", "", "", "650400"),
    ("HVAC parts", "", "Field Ops", "", "", "650200"),
    ("Shipping", "", "", "0-99.99", "", "660100"),
    ("Shipping", "", "", "100-100000", "", "660150"),
    ("Training and certification", "", "", "", "", "660200"),
    ("Team events", "", "Admin,People", "", "", "660300"),
    ("Team events", "Admin", "", "", "", "660320"),
    ("Team events", "People", "", "", "", "660320"),
    ("Gifts and recognition", "", "", "", "recognition, award, employee of the month", "640500"),
    ("Gifts and recognition", "Sales", "", "", "", "640700"),
    ("Gifts and recognition", "", "Sales", "", "", "640600"),
    ("Subscriptions", "", "", "", "", "640300"),
    ("Utilities", "", "", "", "", "660400"),
    ("Uniforms", "", "", "", "", "660500"),
    ("Donations", "", "", "", "", "660600"),
]

VENDOR_OVERRIDES = [
    ("Zoom", "Computer software"),
    ("Adobe", "Computer software"),
    ("Costco", "Grocery"),
    ("Staples", "Office supplies"),
    ("Lowe's", "Hardware store"),
]

VENDORS = [
    "Starbucks", "Dunkin", "Chipotle", "Panera Bread", "Cedar Grill", "Taqueria Sol",
    "Bluebird Bakery", "Pinecrest Coffee Roasters", "Summit Catering Co", "Costco", "Walmart",
    "Walgreens", "Target", "Shell", "Chevron", "Delta Air Lines", "United Airlines", "Marriott",
    "Hilton", "Uber", "Lyft", "Hertz", "Avis", "Enterprise", "Metro Parking Garage", "Staples",
    "Home Depot", "Lowe's", "Best Buy", "Apple", "Dell", "Zoom", "Adobe", "Microsoft", "Google",
    "GitHub", "Northside Plumbing Supply", "Ridgeline HVAC Parts", "Grainger", "FedEx", "UPS",
    "Harbor Print & Ship", "Willow Creek Training Center", "Eastgate Uniform Co", "AT&T",
    "Amazon", "DoorDash", "Lakeshore Bowl Lanes", "Workwear Outfitters",
]


@dataclass(frozen=True)
class Template:
    category: str
    departments: tuple[str, ...]
    descriptors: tuple[str, ...]
    memos: tuple[str, ...]
    amount: tuple[float, float]
    sign: int = 1  # -1 for refunds


# Shared merchants: used for transactions.csv and labeled.csv.
TEMPLATES = [
    Template("Restaurant", ("Sales",),
             ("SQ *BLUEBIRD BAKERY AUSTIN TX", "TST* TAQUERIA SOL AUSTIN TX", "DOORDASH*CHIPOTLE", "CEDAR GRILL 0042 DALLAS TX"),
             ("client dinner w/ prospect", "lunch w/ facilities mgr", "client lunch - Riverside job bid"), (38, 185)),
    Template("Restaurant", ("Field Ops", "Engineering", "Admin", "People"),
             ("TST* TAQUERIA SOL AUSTIN TX", "DOORDASH*CHIPOTLE", "SQ *CEDAR GRILL DALLAS TX", "CEDAR GRILL 0042 DALLAS TX"),
             ("team lunch after install", "HH for ops crew", "all hands lunch", "team lunch - sprint demo", "lunch on the road"), (22, 160)),
    Template("Restaurant", ("Field Ops", "Admin", "Engineering"),
             ("UBER *EATS HELP.UBER.COM", "DOORDASH*CHIPOTLE"),
             ("working lunch", "lunch on the road", "dinner during Tucson job"), (14, 58)),
    Template("Coffee shop", ("Sales", "Field Ops", "Engineering", "Admin", "People"),
             ("STARBUCKS STORE 14221 HOUSTON TX", "SQ *PINECREST COFFEE ROASTERS", "STARBUCKS 08812 DENVER CO"),
             ("coffee for the crew", "client coffee", "early install coffee"), (4, 19)),
    Template("Grocery", ("Field Ops", "Admin", "People"),
             ("COSTCO WHSE #1183 PHOENIX AZ", "COSTCO WHSE #0412 MESA AZ"),
             ("water and snacks for install crew", "break room restock", "office snacks"), (38, 210)),
    Template("Grocery", ("Field Ops",),
             ("WALGREENS #09921 TUCSON AZ", "WAG #04417 MESA AZ"),
             ("bottled water for crew", "ice and cooler supplies"), (9, 64)),
    Template("Catering", ("Sales",),
             ("SUMMIT CATERING CO", "SQ *SUMMIT CATERING CO"),
             ("catered lunch for client demo", "client breakfast - site walk"), (180, 640)),
    Template("Catering", ("Field Ops", "Admin", "People", "Engineering"),
             ("SUMMIT CATERING CO", "HARBOR CATERING TUCSON"),
             ("all hands catering", "catering for onboarding day", "crew catering - Pine Ct"), (150, 520)),
    Template("Airline", ("Sales", "Engineering", "Field Ops", "Admin", "People"),
             ("DELTA AIR 0067112345678", "DELTA AIR LINES 0062389512647"),
             ("flight to Denver training", "site visit flight", "return flight - client site"), (190, 680)),
    Template("Hotel", ("Sales", "Engineering", "Field Ops", "Admin", "People"),
             ("MARRIOTT HOTELS 1234 DENVER CO", "MARRIOTT 0819 PHOENIX AZ"),
             ("training hotel - 2 nights", "crew lodging, Tucson job", "client site stay"), (140, 380)),
    Template("Rideshare", ("Sales", "Engineering", "Field Ops", "Admin", "People"),
             ("UBER *TRIP HELP.UBER.COM",),
             ("airport to client site", "to Sunrise Ave job", "ride from hotel to office"), (11, 58)),
    Template("Rental car", ("Sales", "Field Ops", "Engineering"),
             ("HERTZ 0132 DENVER CO", "AVIS BUDGET 0075 PHOENIX AZ"),
             ("rental for training trip", "van for Tucson job"), (62, 310)),
    Template("Parking", ("Sales", "Admin", "Engineering", "Field Ops"),
             ("SP * METRO PARKING 0219", "PARKING PAY-BY-PHONE", "METRO PARKING GARAGE DENVER CO"),
             ("airport long-term", "client visit parking", "job site meter"), (6, 42)),
    Template("Fuel", ("Field Ops",),
             ("SHELL OIL 57444 DALLAS TX", "CHEVRON 0045512 PHOENIX AZ", "SHELL OIL 60211 AUSTIN TX"),
             ("truck fuel - Tucson job", "van fuel for crew"), (38, 96)),
    Template("Fuel", ("Sales", "Admin"),
             ("SHELL OIL 57444 DALLAS TX", "CHEVRON 0045512 PHOENIX AZ"),
             ("gas for rental return", "fuel for loaner car"), (30, 72)),
    Template("Office supplies", ("Admin", "People", "Sales", "Engineering"),
             ("STAPLES 00129 DENVER CO", "STAPLES 00315 PHOENIX AZ", "STAPLES.COM 800-555-0182"),
             ("printer toner", "whiteboard markers", "dispatch binders"), (12, 240)),
    Template("Electronics", ("Admin", "Engineering", "Sales", "People"),
             ("BEST BUY 00412 DENVER CO", "AMZN MKTP US*2K4", "APPLE STORE R123 HOUSTON TX"),
             ("replacement mouse + cable", "usb hub for field laptop", "HDMI cable for demo"), (12, 74)),
    Template("Electronics", ("Admin", "Sales", "Engineering"),
             ("BEST BUY 00412 DENVER CO", "APPLE STORE R123 HOUSTON TX", "AMZN MKTP US*2K4"),
             ("replacement monitor for Ops desk", "tablet for dispatch", "headset for sales calls"), (89, 1290)),
    Template("Computer software", ("Engineering",),
             ("ADOBE *CREATIVE CLOUD", "ZOOM.US 888-799-9666", "ADOBE *ACROBAT PRO"),
             ("annual seat for Engineering", "dev tools renewal"), (14, 240)),
    Template("Computer software", ("Sales", "Admin", "People", "Field Ops"),
             ("ZOOM.US 888-799-9666", "ADOBE *ACROBAT PRO", "ADOBE *ACROBAT PRO SUB"),
             ("PDF tool for proposals", "webinar license", "meeting host add-on"), (14, 160)),
    Template("Hardware store", ("Field Ops", "Admin", "Sales"),
             ("HOME DEPOT 4417 DENVER CO", "HOME DEPOT #0822 AUSTIN TX", "LOWE'S #1948 PHOENIX AZ", "LOWES #2290 TUCSON AZ"),
             ("PVC fittings for Maple job", "dust masks and tool bits", "fasteners for install"), (9, 210)),
    Template("Plumbing supplies", ("Field Ops",),
             ("NORTHSIDE PLUMBING SUPPLY #211 MESA AZ", "NORTHSIDE PLUMBING SUPPLY #98 TUCSON AZ", "NPS *COUNTER SALE PHOENIX AZ"),
             ("PEX fittings for job 4411", "copper and solder", "P-trap and flange"), (34, 780)),
    Template("Plumbing supplies", ("Sales", "Admin", "Engineering"),
             ("NORTHSIDE PLUMBING SUPPLY #211 MESA AZ", "NPS *COUNTER SALE PHOENIX AZ"),
             ("sample kit for trade show", "demo fixtures"), (40, 300)),
    Template("HVAC parts", ("Field Ops",),
             ("RIDGELINE HVAC PARTS 0442 PHOENIX AZ", "RIDGELINE HVAC PARTS 0309 MESA AZ", "AMZN MKTP US*2K4"),
             ("replacement capacitor, job 4411", "replacement thermostat for job 4411", "blower motor - Pine Ct", "fan relay"), (24, 690)),
    Template("HVAC parts", ("Admin", "Engineering", "Sales"),
             ("RIDGELINE HVAC PARTS 0442 PHOENIX AZ",),
             ("spare filters for office unit", "return filter order"), (30, 260)),
    Template("Shipping", ("Admin", "Sales", "Engineering", "Field Ops", "People"),
             ("FEDEX 882110 DENVER CO", "FEDEX OFFICE 3412 AUSTIN TX", "HARBOR PRINT & SHIP"),
             ("return labels for warranty part", "ship sample to client", "certified mail"), (8, 92)),
    Template("Shipping", ("Field Ops", "Sales"),
             ("FEDEX 882110 DENVER CO", "FEDEX FREIGHT 4421 PHOENIX AZ"),
             ("return core - HVAC compressor", "freight for meter pallet"), (110, 640)),
    Template("Training and certification", ("Field Ops", "Engineering", "People"),
             ("WILLOW CREEK TRAINING CTR", "COURSEHUB ONLINE LEARNING", "TRADE CERT EXAM FEE 800-555-0148"),
             ("refrigerant handling course", "exam fee - journeyman", "onboarding training fee"), (49, 899)),
    Template("Team events", ("Field Ops", "Sales", "Engineering"),
             ("LAKESHORE BOWL LANES", "SQ *KIDS CLIMB ARENA"),
             ("team outing - bowling", "crew event", "summer picnic supplies"), (120, 600)),
    Template("Team events", ("Admin", "People"),
             ("LAKESHORE BOWL LANES", "SQ *KIDS CLIMB ARENA"),
             ("employee engagement - bowling", "wellness day"), (100, 480)),
    Template("Gifts and recognition", ("People", "Admin", "Field Ops", "Engineering"),
             ("AMZN MKTP US*2K4", "TARGET 00412 DENVER CO"),
             ("employee of the month gift card", "recognition for 5-yr tech", "award plaque", "thank-you cards and gift"), (25, 210)),
    Template("Gifts and recognition", ("Sales",),
             ("AMZN MKTP US*2K4", "SQ *BLUEBIRD BAKERY AUSTIN TX"),
             ("client holiday gifts", "thank-you for referral partner"), (30, 150)),
    Template("Subscriptions", ("Sales", "Admin", "Engineering", "Field Ops", "People"),
             ("NATIONAL TRADE ASSN DUES 800-555-0199", "SKYLINE MEMBERSHIP PORTAL", "PP *LICENSE RENEWAL DIR"),
             ("annual dues", "monthly subscription", "renewal"), (49, 420)),
    Template("Utilities", ("Admin",),
             ("CITY OF MESA UTIL BILL", "RIVERBEND WATER UTILITY", "AT&T BILL PAY"),
             ("office internet", "shop water"), (48, 640)),
    Template("Uniforms", ("Field Ops",),
             ("EASTGATE UNIFORM CO", "WORKWEAR OUTFITTERS #33"),
             ("boots reimbursement", "new hire shirts", "jackets for winter crew"), (49, 310)),
    Template("Donations", ("People", "Admin"),
             ("GIVING PORTAL 800-555-0177", "COMMUNITY FOOD BANK DONATE", "LOCAL SHELTER FUNDRAISER"),
             ("toy drive", "food bank - Oct drive", "fundraiser sponsorship"), (25, 500)),
]

# Merchants that appear only in labeled.csv (so onboarding sees merchants the tool has not been shown).
NOVEL_TEMPLATES = [
    Template("Restaurant", ("Sales",), ("PANERA BREAD 4452 DENVER CO",),
             ("client lunch - pricing review", "client lunch - renewal check-in"), (30, 110)),
    Template("Restaurant", ("Field Ops", "Admin"), ("CHIPOTLE 1234 DALLAS TX",),
             ("lunch on the road", "working lunch"), (12, 38)),
    Template("Coffee shop", ("Admin", "People"), ("DUNKIN 332011 PHOENIX AZ",),
             ("interview coffee", "coffee for visitors"), (4, 14)),
    Template("Grocery", ("Admin",), ("WALMART 2200 MESA AZ",),
             ("snacks for all hands", "paper plates and cups"), (18, 120)),
    Template("Hotel", ("Sales", "Engineering"), ("HILTON GARDEN INN DENVER",),
             ("client site stay", "conference hotel"), (140, 300)),
    Template("Rideshare", ("Admin", "People"), ("LYFT *RIDE SAN FRANCISCO CA",),
             ("shuttle to offsite", "ride to vendor office"), (14, 48)),
    Template("Rental car", ("Sales",), ("ENTERPRISE RENT-A-CAR 0042",),
             ("client site van", "rental for demo trip"), (62, 260)),
    Template("Parking", ("Sales",), ("CITY PARKING METER DENVER",),
             ("client visit parking",), (4, 24)),
    Template("Fuel", ("Field Ops",), ("SHELL OIL 60211 AUSTIN TX",),
             ("fuel for the service van",), (40, 90)),
    Template("Airline", ("Sales", "Engineering"), ("UNITED AIRLINES 0161234567890",),
             ("flight to Denver training", "site visit flight"), (180, 620)),
    Template("Shipping", ("Field Ops",), ("UPS STORE 4411 MESA AZ",),
             ("return core", "ship parts to supplier"), (14, 85)),
    Template("Electronics", ("Engineering",), ("DELL MARKETING 800-999",),
             ("dev laptop dock", "usb-c docking station"), (120, 1180)),
    Template("Computer software", ("Engineering",), ("GITHUB *TEAM PLAN", "GOOGLE *WORKSPACE"),
             ("seat renewal", "shared drive storage"), (18, 210)),
    Template("Hardware store", ("Admin", "Sales"), ("ACE HARDWARE 0031 TUCSON AZ",),
             ("cable ties and tape", "fuses for break room fridge"), (6, 80)),
    Template("Gifts and recognition", ("People",), ("TARGET 00412 DENVER CO",),
             ("recognition gift cards", "holiday card supplies"), (40, 200)),
    Template("HVAC parts", ("Field Ops",), ("GRAINGER INDUSTRIAL 0813",),
             ("capacitor for job 4411", "contactor kit"), (20, 96)),
    Template("Uniforms", ("Field Ops",), ("WORKWEAR OUTFITTERS #33",),
             ("boots", "rain gear"), (60, 240)),
    Template("Subscriptions", ("Admin",), ("NEWSLETTER ANNUAL - TRADE JOURNAL",),
             ("annual subscription", "magazine renewal"), (49, 180)),
    Template("Training and certification", ("Engineering",), ("ONLINE CERT PROCTORING 800-555-0190",),
             ("certification exam voucher",), (99, 320)),
    Template("Donations", ("People",), ("COMMUNITY GIVING PORTAL 800-555-0166",),
             ("match drive", "charity walk sponsorship"), (25, 250)),
    Template("Team events", ("Sales",), ("PINEWOOD LANES BOWL",),
             ("client appreciation bowling",), (150, 480)),
    Template("Utilities", ("Admin",), ("XFINITY 800-555-0144",),
             ("office internet",), (60, 180)),
]

# Hard labeled rows (~15%): cryptic descriptors, or memos that point the other way.
HARD_LABELED = [
    (Template("Grocery", ("Field Ops",), ("COSTCO WHSE #1183 PHOENIX AZ",), (), (60, 210)), "team lunch after install"),
    (Template("HVAC parts", ("Field Ops",), ("AMZN MKTP US*2K4",), (), (38, 240)), "replacement thermostat for job 4411"),
    (Template("Grocery", ("Field Ops",), ("WAG #04417 MESA AZ",), (), (9, 40)), "cups and napkins for install crew"),
    (Template("Plumbing supplies", ("Field Ops",), ("PP*7731 SVCS TUCSON AZ",), (), (34, 300)), ""),
    (Template("Restaurant", ("Field Ops",), ("SP * 88122 AZ",), (), (40, 160)), "HH for ops crew"),
    (Template("Gifts and recognition", ("People",), ("POS 4421 MISC",), (), (60, 300)), "employee of the month gift cards"),
    (Template("Restaurant", ("Sales",), ("TST* 0219 AUSTIN",), (), (32, 120)), "lunch w/ facilities mgr"),
    (Template("Hotel", ("Engineering",), ("HILTON GARDEN INN DENVER",), (), (150, 320)), "training hotel - 2 nights"),
    (Template("Rideshare", ("Sales",), ("LYFT *RIDE SAN FRANCISCO CA",), (), (22, 64)), "airport to client site"),
    (Template("Restaurant", ("Engineering",), ("DOORDASH*PANERA 4452",), (), (28, 90)), "team lunch - sprint demo"),
    (Template("Shipping", ("Field Ops",), ("UPS STORE 4411 MESA AZ",), (), (19, 58)), "ship warranty core back"),
    (Template("Electronics", ("Admin",), ("DELL MARKETING 800-999",), (), (240, 412)), "monitor for dispatch desk"),
    (Template("Computer software", ("Engineering",), ("GOOGLE *WORKSPACE",), (), (12, 144)), ""),
    (Template("Electronics", ("Admin",), ("BEST BUY 00412 DENVER CO",), (), (19, 49)), "power strip for front desk"),
    (Template("Gifts and recognition", ("People",), ("TARGET 00412 DENVER CO",), (), (50, 150)), "recognition gift cards"),
    (Template("HVAC parts", ("Field Ops",), ("GRAINGER INDUSTRIAL 0813",), (), (20, 96)), "capacitor for job 4411"),
    (Template("Computer software", ("Sales",), ("ZOOM.US 888-799-9666",), (), (14, 160)), ""),
    (Template("Airline", ("Sales",), ("DELTA AIR LINES 0062389512647",), (), (189, 412), -1), "refund - cancelled flight"),
]

# Transactions with deliberate edge cases: 3 disputes, 2 personal, 4 refunds.
SPECIAL_TRANSACTIONS = [
    (Template("Electronics", ("Admin",), ("AMZN MKTP US*2K4",), (), (89, 1290)), DISPUTE_MEMO),
    (Template("Restaurant", ("Sales",), ("DOORDASH*CHIPOTLE",), (), (24, 96)), DISPUTE_MEMO),
    (Template("Fuel", ("Field Ops",), ("CHEVRON 0045512 PHOENIX AZ",), (), (40, 88)), DISPUTE_MEMO),
    (Template("Restaurant", ("Field Ops",), ("TST* TAQUERIA SOL AUSTIN TX",), (), (30, 85)), PERSONAL_MEMO),
    (Template("Coffee shop", ("Engineering",), ("STARBUCKS STORE 14221 HOUSTON TX",), (), (5, 12)), PERSONAL_MEMO),
    (Template("Airline", ("Sales",), ("DELTA AIR LINES 0062389512647",), (), (189, 412), -1), "refund - cancelled flight"),
    (Template("Hotel", ("Engineering",), ("MARRIOTT 0819 PHOENIX AZ",), (), (120, 260), -1), "refund - cancelled room"),
    (Template("Office supplies", ("Admin",), ("STAPLES 00315 PHOENIX AZ",), (), (22, 74), -1), "return - wrong toner"),
    (Template("Hardware store", ("Field Ops",), ("LOWE'S #1948 PHOENIX AZ",), (), (18, 96), -1), "return - PVC fittings"),
]


# ---------------------------------------------------------------- rules

def build_rules() -> list[dict[str, str]]:
    rules = []
    for i, (cat, dept, excluded, amount_range, keywords, gl) in enumerate(RULES, start=1):
        rules.append({
            "rule_id": f"R{i:03d}", "category": cat, "department": dept,
            "excluded_departments": excluded, "amount_range": amount_range,
            "keywords": keywords, "gl_code": gl, "gl_name": GL_NAMES[gl],
        })
    cats = {c for c, _ in CATEGORIES}
    assert all(r["category"] in cats for r in rules), "rule references unknown category"
    assert all(c in {r["category"] for r in rules} for c in cats), "category without a rule"
    return rules


def _split(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def _in_range(text: str, amount: float) -> bool:
    if not text:
        return True
    lo, hi = (float(x) for x in text.split("-"))
    return lo <= abs(amount) <= hi


def _keyword_in(keyword: str, memo: str) -> bool:
    return re.search(r"(?<![A-Za-z])" + re.escape(keyword) + r"(?![A-Za-z])", memo, re.IGNORECASE) is not None


def resolve(rules, category: str, dept: str, amount: float, memo: str) -> dict[str, str]:
    """Same contract as the tool: eligible rules, then keyword hit in memo, else the blank-keyword rule."""
    eligible = []
    for r in rules:
        if r["category"] != category:
            continue
        if r["department"] and r["department"] != dept:
            continue
        if dept in _split(r["excluded_departments"]):
            continue
        if not _in_range(r["amount_range"], amount):
            continue
        eligible.append(r)
    hits = [r for r in eligible if any(_keyword_in(k, memo) for k in _split(r["keywords"]))]
    pool = hits or [r for r in eligible if not r["keywords"]]
    if len({r["gl_code"] for r in pool}) != 1:
        raise ValueError(f"no single rule for {category}/{dept}/{amount}/{memo!r}: {[r['rule_id'] for r in pool]}")
    return pool[0]


# ---------------------------------------------------------------- rows

def make_row(rng: random.Random, t: Template, memo: str | None = None) -> dict[str, str]:
    dept = rng.choice(t.departments)
    descriptor = rng.choice(t.descriptors)
    if memo is None:
        memo = "" if rng.random() < EMPTY_MEMO_RATE else rng.choice(t.memos)
    cents = rng.randint(round(t.amount[0] * 100), round(t.amount[1] * 100))
    day = rng.randint(1, 31)
    issuer = ISSUER_HINT[t.category] if rng.random() < 0.4 else rng.choice(ISSUER_LABELS)
    return {
        "date": f"10/{day:02d}/2026",
        "descriptor": descriptor,
        "memo": memo,
        "amount": f"{t.sign * cents / 100:.2f}",
        "department": dept,
        "category": t.category,
        "issuer": issuer,
    }


def build_transactions(rng: random.Random) -> list[dict[str, str]]:
    rows = [make_row(rng, rng.choice(TEMPLATES)) for _ in range(N_TRANSACTIONS - len(SPECIAL_TRANSACTIONS))]
    rows += [make_row(rng, t, memo) for t, memo in SPECIAL_TRANSACTIONS]
    rng.shuffle(rows)
    for i, row in enumerate(rows, start=1):
        row["id"] = f"TX-{i:06d}"
    return rows


def build_labeled(rng: random.Random, rules) -> list[dict[str, str]]:
    pool = TEMPLATES + NOVEL_TEMPLATES
    rows = [make_row(rng, rng.choice(pool)) for _ in range(N_LABELED - len(HARD_LABELED))]
    rows += [make_row(rng, t, memo) for t, memo in HARD_LABELED]
    rng.shuffle(rows)
    for i, row in enumerate(rows, start=1):
        row["id"] = f"LB-{i:06d}"
        rule = resolve(rules, row["category"], row["department"], float(row["amount"]), row["memo"])
        row["expected_category"] = row["category"]
        row["expected_gl_code"] = rule["gl_code"]
    return rows


# ---------------------------------------------------------------- output

TOML_TEXT = '''# spendcoder project: Acme Field Services (100% synthetic demo data)
[project]
name = "Acme Field Services (synthetic demo)"

[dataset]
path = "data/transactions.csv"
labeled_path = "data/labeled.csv"   # rows with expected_category / expected_gl_code for onboarding
amount_sign = 1                      # set -1 if your export shows charges as negatives

[dataset.columns]                    # map YOUR export's headers to spendcoder fields
id = "Transaction ID"
date = "Posted Date"
description = "Merchant Descriptor"
note = "Memo"
amount = "Amount"
department = "Department"

[policy]
categories = "policy/categories.csv"
rules = "policy/rules.csv"
vendor_overrides = "policy/vendor_overrides.csv"
vendors = "policy/vendors.txt"

[policy.note_glossary]               # org shorthand cardholders type in memos
HH = "Happy hour (team food and drinks)"
"all hands" = "Company-wide meeting"

[policy.keyword_aliases]             # literal expansions used when matching rule keywords
"happy hour" = ["HH"]
meeting = ["mtg"]

[llm]
backend = "openrouter"               # openrouter | agent-files | mock
chat_model = "openai/gpt-5.6-terra"  # OpenRouter chat model for normalize + review
decision_model = "typesafe/jev-1.13" # OpenRouter Decisions model (see `spendcoder models`)
reflection_model = "openai/gpt-5.6-terra"  # GEPA reflection during onboarding
max_concurrency = 8

[search]
provider = "parallel"                # parallel | none  (needs PARALLEL_API_KEY)
mode = "fast"
max_results = 3
only_unknown_merchants = true

[gates]
category_min_confidence = 0.55       # below this, category stays Unknown and goes to review
select_min_confidence = 0.50
max_error_rate = 0.05

[review]
low_confidence = 0.5

[[review.flags]]
name = "dispute"
question = "Does the cardholder's note say this charge is disputed, unrecognized, fraudulent, or should be flagged for someone to look at?"
action = "hold"

[[review.flags]]
name = "recognition_intent"
question = "Was this purchase made to recognize, reward, or celebrate an employee or team achievement?"
action = "review"

[[review.flags]]
name = "personal_spend"
question = "Does the note suggest this was a personal purchase or will be repaid by the cardholder?"
action = "hold"

[tune]
components = ["categorize_instructions", "category_guide", "select_instructions"]
max_metric_calls = 300
split = [0.5, 0.25, 0.25]
'''

README_TEXT = """# Acme Field Services (synthetic demo)

Demo project for spendcoder. Acme Field Services is an invented HVAC and plumbing company.
All data here is synthetic: names, memos, amounts and dates were generated, not exported.
Merchants are generic public brands or obviously fake local businesses.

Regenerate (seeded, deterministic): public\\.venv\\Scripts\\python.exe public\\scripts\\make_synthetic_data.py

- spendcoder.toml: settings (columns, policy paths, LLM backend, gates, review flags, tuning)
- policy/categories.csv: categories the model may assign
- policy/rules.csv: category x department x amount to GL code; keywords break ties
- policy/vendor_overrides.csv: vendors that always map to one category
- policy/vendors.txt: reference vendor names for matching
- data/transactions.csv: 240 synthetic card transactions to code
- data/labeled.csv: 120 transactions with expected category and GL code, for onboarding
"""


def write_csv(path: Path, header: list[str], rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    rng = random.Random(SEED)
    rules = build_rules()
    transactions = build_transactions(rng)
    labeled = build_labeled(rng, rules)

    write_csv(TEMPLATE_DIR / "policy/categories.csv", ["category", "description"], CATEGORIES)
    write_csv(TEMPLATE_DIR / "policy/rules.csv",
              ["rule_id", "category", "department", "excluded_departments", "amount_range",
               "keywords", "gl_code", "gl_name"],
              [[r["rule_id"], r["category"], r["department"], r["excluded_departments"],
                r["amount_range"], r["keywords"], r["gl_code"], r["gl_name"]] for r in rules])
    write_csv(TEMPLATE_DIR / "policy/vendor_overrides.csv", ["vendor", "category"], VENDOR_OVERRIDES)
    write_text(TEMPLATE_DIR / "policy/vendors.txt", "\n".join(VENDORS) + "\n")

    tx_header = ["Transaction ID", "Posted Date", "Merchant Descriptor", "Memo", "Amount",
                 "Department", "Cardholder Category"]
    write_csv(TEMPLATE_DIR / "data/transactions.csv", tx_header,
              [[t["id"], t["date"], t["descriptor"], t["memo"], t["amount"], t["department"], t["issuer"]]
               for t in transactions])
    write_csv(TEMPLATE_DIR / "data/labeled.csv", tx_header + ["expected_category", "expected_gl_code"],
              [[t["id"], t["date"], t["descriptor"], t["memo"], t["amount"], t["department"], t["issuer"],
                t["expected_category"], t["expected_gl_code"]] for t in labeled])

    write_text(TEMPLATE_DIR / "spendcoder.toml", TOML_TEXT)
    write_text(TEMPLATE_DIR / "README.md", README_TEXT)

    print(f"wrote {TEMPLATE_DIR}")
    print(f"  categories={len(CATEGORIES)} rules={len(rules)} overrides={len(VENDOR_OVERRIDES)} "
          f"vendors={len(VENDORS)} transactions={len(transactions)} labeled={len(labeled)}")


if __name__ == "__main__":
    main()
