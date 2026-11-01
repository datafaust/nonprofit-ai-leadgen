"""Map untrusted source rows into bounded, nullable organization facts."""
import hashlib
import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .sources import Source

BOROUGHS = {"bronx": "Bronx", "brooklyn": "Brooklyn", "manhattan": "Manhattan", "queens": "Queens", "staten island": "Staten Island"}

class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)

def clean(value, limit=2000):
    if value is None or not isinstance(value, str):
        return None
    parser = _Text()
    parser.feed(value[:limit * 4])
    result = re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()
    return result[:limit] or None

def website(value):
    value = clean(value, 500)
    if not value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme.lower() in ("http", "https") and parts.hostname and not parts.username and not parts.password else None

def zip_code(value):
    value = clean(value, 20)
    return value if value and re.fullmatch(r"\d{5}(?:-\d{4})?", value) else None

def phone(value):
    value = clean(value, 50)
    digits = re.sub(r"\D", "", value or "")
    return value if 10 <= len(digits) <= 15 else None

def borough(value):
    return BOROUGHS.get((clean(value, 50) or "").casefold())

def normalize(row: dict, source: Source):
    if source.key == "community_based":
        name = clean(row.get("organization_name"), 300)
        street = clean(row.get("street_address"), 300)
        city = clean(row.get("city"), 100)
        zipcode = zip_code(row.get("postcode"))
        result = dict(name=name, description=clean(row.get("mission"), 4000), borough=borough(row.get("borough")), street_address=street,
                      city=city, state=clean(row.get("state"), 30), zip_code=zipcode, website=website(row.get("website")),
                      email=None, phone=None, category=None, last_updated=None)
    elif source.key == "womens_resource":
        if (clean(row.get("nonprofit"), 10) or "").upper() != "Y":
            return None
        name = clean(row.get("organizationname"), 300)
        street = clean(row.get("address1"), 300)
        city = clean(row.get("city"), 100)
        zipcode = zip_code(row.get("postcode"))
        b = borough(row.get("borough"))
        if not b:
            flags = [label for field, label in (("bronx", "Bronx"), ("brooklyn", "Brooklyn"), ("manhattan", "Manhattan"), ("queens", "Queens"), ("staten_island", "Staten Island")) if (clean(row.get(field), 10) or "").upper() == "Y"]
            b = flags[0] if len(flags) == 1 else None
        categories = [label for field, label in (("aging", "Aging"), ("arts_culture", "Arts & Culture"), ("education", "Education"), ("health", "Health"), ("housing", "Housing"), ("immigration", "Immigration"), ("legal_services", "Legal Services"), ("youth_services", "Youth Services")) if (clean(row.get(field), 10) or "").upper() == "Y"]
        result = dict(name=name, description=clean(row.get("description"), 4000), borough=b, street_address=street,
                      city=city, state="NY" if b else None, zip_code=zipcode, website=website(row.get("url")),
                      email=None, phone=phone(row.get("phone")), category=", ".join(categories) or None, last_updated=None)
    else:
        raise ValueError("unknown source")
    if not name or len(name) < 2:
        return None
    if not (street or zipcode or result["website"]):
        return None
    source_id = clean(row.get(":id"), 100)
    if not source_id:
        return None
    result["source_record_id"] = source_id
    # Cross-source merging requires matching name, street and ZIP. Similar names alone never merge.
    result["match_key"] = hashlib.sha256("|".join((name.casefold(), street.casefold(), zipcode.casefold())).encode()).hexdigest() if street and zipcode else None
    result["source"] = source.key
    result["source_url"] = source.page_url
    result["contact_verification_status"] = "source_reported" if result["phone"] or result["email"] else "none"
    result["lead_qualification_status"] = "unreviewed"
    return result
