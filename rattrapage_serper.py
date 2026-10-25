#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rattrapage des agences introuvables / mal matchées via Serper (/places).

Entrées (même dossier) :
  a_rechercher_manuellement.csv   (siren;nom;probleme;...)
  leboncoin_extraction_final.csv  (siren;nom;adresse) -> adresse d'origine, sert au matching
  agences_nettoye.csv             (base à compléter)
Variables : SERPER_KEY [obligatoire] · INCLUDE_INDETERMINE=true pour traiter aussi les cas "3_"
Sorties : agences_serper.csv (résultat + statut + score) et agences_final.csv (base mise à jour)
"""
import csv, os, re, sys, time, unicodedata
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import requests

API = "https://google.serper.dev/places"
COLS = ["siren","nom","adresse","rating","rating_count","phone_number","website","horaires","featureId","placeId"]
ACCEPT, MAYBE = 0.70, 0.45     # score >= ACCEPT : validé · >= MAYBE : à vérifier · sinon : rejeté
SITES_IGNORES = ("leboncoin.fr",)

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()

STOP = {"agence","agences","immobilier","immobiliere","immo","cabinet","sarl","sas","de","du","la","le","les","des","et","l","d"}
def core(s): return " ".join(t for t in norm(s).split() if t not in STOP) or norm(s)

def parse_addr(a):
    """-> (n° de rue, code postal, ville normalisée). Tolère « 86280 Saint-Benoît » et « …, 86280 Saint-Benoît »."""
    a = a or ""
    cps = re.findall(r"\b\d{5}\b", a)
    cp = cps[-1] if cps else ""
    ville = norm(a.split(cp)[-1]) if cp else norm(a.split(",")[-1])
    ville = re.sub(r"\b(france|guyane francaise|la reunion)\b", "", ville).strip()
    m = re.match(r"\s*(\d+)", a)
    return (m.group(1) if m else ""), cp, ville

def score(nom, addr, place):
    """0..1 : adresse (cp 0.45, ville 0.2, n° 0.1) + nom (0.25)."""
    n0, cp0, v0 = parse_addr(addr)
    n1, cp1, v1 = parse_addr(place.get("address", ""))
    s = 0.0
    if cp0 and cp0 == cp1: s += 0.45
    elif cp0 and cp1 and cp0[:2] == cp1[:2]: s += 0.10          # même département
    if v0 and v1 and (v0 == v1 or v0 in v1 or v1 in v0): s += 0.20
    if n0 and n0 == n1: s += 0.10
    s += 0.25 * SequenceMatcher(None, core(nom), core(place.get("title", ""))).ratio()
    return round(s, 2)

def pick(nom, addr, places):
    scored = sorted(((score(nom, addr, p), p) for p in places), key=lambda x: -x[0])
    return scored[0] if scored else (0.0, None)

def clean_site(u):
    u = (u or "").strip()
    if not u or any(d in u.lower() for d in SITES_IGNORES): return ""
    p = urlsplit(u)
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))

def build_row(siren, nom, p):
    return {"siren": siren, "nom": nom, "adresse": p.get("address", ""),
            "rating": p.get("rating", ""), "rating_count": p.get("ratingCount", ""),
            "phone_number": re.sub(r"[^\d+]", "", p.get("phoneNumber", "") or ""),
            "website": clean_site(p.get("website")), "horaires": "",   # /places ne renvoie pas les horaires
            "featureId": "", "placeId": f"cid:{p['cid']}" if p.get("cid") else ""}

def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=";"))

def search(session, key, nom, addr):
    _, cp, ville = parse_addr(addr)
    q = f"{nom} {ville}".strip() if ville else nom
    for attempt in range(3):
        try:
            r = session.post(API, json={"q": q, "gl": "fr", "hl": "fr", "location": "France"},
                             headers={"X-API-KEY": key, "Content-Type": "application/json"}, timeout=30)
        except requests.RequestException as e:
            time.sleep(3 * (attempt + 1)); continue
        if r.status_code == 200: return r.json().get("places") or []
        if r.status_code in (401, 402, 403): sys.exit(f"❌ Serper HTTP {r.status_code} : clé invalide ou crédits épuisés")
        time.sleep(3 * (attempt + 1))
    return None

def main():
    key = os.getenv("SERPER_KEY") or sys.exit("❌ SERPER_KEY manquant")
    incl3 = (os.getenv("INCLUDE_INDETERMINE") or "").lower() in ("1", "true", "yes")
    todo = [r for r in read_csv("a_rechercher_manuellement.csv")
            if r["probleme"][0] in "12" or (incl3 and r["probleme"][0] == "3")]
    addr_src = {r["siren"]: r["adresse"] for r in read_csv("leboncoin_extraction_final.csv")}
    base = read_csv("agences_nettoye.csv")
    owners = {}                                        # cid déjà pris par un autre SIREN
    for r in base:
        if r["placeId"].startswith("cid:") or r["placeId"]: owners.setdefault(r["placeId"], r["siren"])

    session, out, updates = requests.Session(), [], {}
    for i, r in enumerate(todo, 1):
        siren, nom = r["siren"], r["nom"]
        addr = addr_src.get(siren) or r.get("adresse", "")
        places = search(session, key, nom, addr)
        if places is None: statut, sc, row = "erreur_api", 0, None
        else:
            sc, best = pick(nom, addr, places)
            row = build_row(siren, nom, best) if best else None
            statut = "rejete" if not best or sc < MAYBE else ("valide" if sc >= ACCEPT else "a_verifier")
            if row and owners.get(row["placeId"], siren) != siren:
                statut = "a_verifier"; row["__note"] = f"fiche déjà liée à {owners[row['placeId']]}"
        print(f"[{i}/{len(todo)}] {siren} {nom[:35]:35} → {statut:10} {sc:.2f} {(row or {}).get('adresse','')}", flush=True)
        out.append({**(row or {c: "" for c in COLS}), "siren": siren, "nom": nom,
                    "adresse_csv": addr, "statut": statut, "score": sc, "note": (row or {}).get("__note", "")})
        if statut == "valide": updates[siren] = row
        time.sleep(0.2)

    cols = COLS + ["adresse_csv", "statut", "score", "note"]
    with open("agences_serper.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, cols, delimiter=";", lineterminator="\n", extrasaction="ignore"); w.writeheader(); w.writerows(out)
    with open("agences_final.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, COLS, delimiter=";", lineterminator="\n", extrasaction="ignore"); w.writeheader()
        w.writerows(updates.get(r["siren"], r) for r in base)
    from collections import Counter
    print("\nBilan :", dict(Counter(o["statut"] for o in out)))

if __name__ == "__main__":
    main()
