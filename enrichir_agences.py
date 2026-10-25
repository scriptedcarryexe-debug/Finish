#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enrichit leboncoin_extraction_final.csv (siren;nom;adresse) avec la fiche Google Maps
de chaque agence, via l'API Searlo (GET /search/places)  ->  agences.csv

Variables d'environnement :
  SEARLO_KEYS       clés Searlo séparées par ";" (utilisées dans cet ordre)     [obligatoire]
  MAX_ROWS          limiter à N agences (0 = tout)                               [défaut 0]
  RETRY_NOT_FOUND   "true" pour retenter les agences marquées introuvables       [défaut false]
  MAX_RUNTIME_MIN   arrêt propre après N minutes (reprise au prochain lancement) [défaut 330]
  MIN_INTERVAL      secondes mini entre 2 requêtes (Searlo : 48/min max)         [défaut 1.3]

Dans agences.csv, `nom` vient du CSV d'entrée et `adresse` de la fiche Maps.
Relancer le script reprend là où il s'est arrêté : les SIREN déjà présents dans
agences.csv sont ignorés.
"""
import csv
import os
import re
import signal
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from types import SimpleNamespace

import requests

# ───────────────────────────── Réglages ─────────────────────────────
API_URL = os.getenv("SEARLO_URL", "https://api.searlo.tech/api/v1/search/places")
INPUT_CSV = "leboncoin_extraction_final.csv"
OUTPUT_CSV = "agences.csv"
REPORT_MD = "rapport_agences.md"

LIMIT, PAGE, GL, HL, PAYS = 2, 1, "fr", "fr", "France"
TIMEOUT_S = float(os.getenv("TIMEOUT_S") or 30)  # temps laissé à Searlo pour récupérer UNE fiche
MAX_ATTEMPTS = 3        # essais par fiche (timeout, 429, 5xx, erreur réseau)
SLOW_S = 15             # au-delà, l'ancien code (timeout 15 s) aurait déclaré la fiche inexistante

MIN_INTERVAL = float(os.getenv("MIN_INTERVAL") or 1.3)
MAX_RUNTIME = float(os.getenv("MAX_RUNTIME_MIN") or 330) * 60
MAX_ROWS = int(os.getenv("MAX_ROWS") or 0)
RETRY_NOT_FOUND = (os.getenv("RETRY_NOT_FOUND") or "").strip().lower() in ("1", "true", "yes")
MAX_CONSECUTIVE_ERRORS = 15
LOW_CONFIDENCE = 0.45   # score en dessous duquel la fiche est signalée "à vérifier"
AMBIGUOUS_MARGIN = 0.15  # écart de score mini entre les 2 résultats pour trancher sans doute
LIST_CAP = 100          # lignes max par section du rapport

OUT_COLUMNS = ["siren", "nom", "adresse", "rating", "rating_count", "phone_number",
               "website", "horaires", "featureId", "placeId"]
CSV_OPTS = dict(delimiter=";", lineterminator="\n")


def log(msg=""):
    print(msg, flush=True)


class AllKeysExhausted(Exception):
    pass


class FicheError(Exception):
    pass


# ───────────────────────────── Clés Searlo ─────────────────────────────
class KeyPool:
    """Utilise les clés dans l'ordre ; passe à la suivante quand une clé est épuisée/invalide."""

    def __init__(self, keys):
        self.keys = keys
        self.i = 0
        self.ok_calls = [0] * len(keys)

    @property
    def key(self):
        return self.keys[self.i]

    @property
    def label(self):
        return f"clé #{self.i + 1} …{self.key[-4:]}"

    def burn(self, reason):
        old = self.label
        self.i += 1
        if self.i >= len(self.keys):
            log(f"   🔑 {old} inutilisable ({reason}) → plus aucune clé disponible")
            raise AllKeysExhausted()
        log(f"   🔑 {old} inutilisable ({reason}) → bascule sur {self.label}")


_last_call = 0.0


def throttle():
    """Searlo limite Maps/Places à 48 requêtes/minute : on espace les appels."""
    global _last_call
    wait = MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def retry_after(resp, default):
    try:
        return max(1, min(int(float(resp.headers.get("Retry-After", default))), 120))
    except (TypeError, ValueError):
        return default


def fetch(session, pool, nom, adresse):
    """Appelle /search/places. Retourne (json, durée). Lève FicheError / AllKeysExhausted."""
    params = {"q": nom, "location": f"{adresse}, {PAYS}", "limit": LIMIT,
              "page": PAGE, "gl": GL, "hl": HL}
    transient = 0
    while True:
        throttle()
        t0 = time.monotonic()
        try:
            r = session.get(API_URL, params=params, headers={"x-api-key": pool.key},
                            timeout=(10, TIMEOUT_S))
        except requests.exceptions.RequestException as e:
            transient += 1
            why = (f"timeout {TIMEOUT_S:g}s" if isinstance(e, requests.exceptions.Timeout)
                   else type(e).__name__)
            if transient >= MAX_ATTEMPTS:
                raise FicheError(why)
            log(f"   ⏳ {why} → nouvel essai {transient + 1}/{MAX_ATTEMPTS}")
            time.sleep(3 * transient)
            continue
        dt = time.monotonic() - t0
        code = r.status_code

        if code == 200:
            try:
                data = r.json()
            except ValueError:
                raise FicheError("réponse non-JSON")
            pool.ok_calls[pool.i] += 1
            return data, dt
        if code in (401, 402, 403):          # clé invalide / plus de crédits / compte désactivé
            pool.burn(f"HTTP {code}")
            continue
        if code == 429 or code >= 500:       # limite de débit ou panne temporaire
            transient += 1
            if transient >= MAX_ATTEMPTS:
                raise FicheError(f"HTTP {code}")
            wait = retry_after(r, 10 if code == 429 else 3 * transient)
            log(f"   ⏳ HTTP {code} → pause {wait}s puis essai {transient + 1}/{MAX_ATTEMPTS}")
            time.sleep(wait)
            continue
        raise FicheError(f"HTTP {code} {r.text[:120]!r}")


# ───────────────────────────── Mise en forme ─────────────────────────────
DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_RANGE = re.compile(r"(\d{1,2})[:h](\d{2})\s*[–—-]\s*(\d{1,2})[:h](\d{2})")


def _hm(h, m):
    return f"{int(h)}h" + ("" if m == "00" else m)


def fmt_slot(s):
    s = str(s).replace("\u202f", " ").replace("\u00a0", " ").strip()
    m = _RANGE.fullmatch(s)
    return f"{_hm(m[1], m[2])}-{_hm(m[3], m[4])}" if m else s


def fmt_hours(hours):
    """[{day:'lundi',hours:['10:00–12:00','14:00–18:00']},…] -> 'lundi : 10h-12h 14h-18h | mardi : …'"""
    by_day = {}
    for h in hours or []:
        day = str(h.get("day") or "").strip().lower()
        slots = " ".join(fmt_slot(x) for x in (h.get("hours") or []))
        if day and slots:
            by_day[day] = slots
    order = [d for d in DAYS if d in by_day] + [d for d in by_day if d not in DAYS]
    return " | ".join(f"{d} : {by_day[d]}" for d in order)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


_STOP = {"agence", "agences", "immobilier", "immobiliere", "immobiliers", "immo", "cabinet",
         "sarl", "sas", "sasu", "eurl", "de", "du", "la", "le", "les", "des", "et", "l", "d"}


def _core(s):
    return " ".join(t for t in norm(s).split() if t not in _STOP)


def score(nom, adresse, place):
    """0..1 : 60 % ressemblance du nom, 40 % adresse (code postal identique = 1)."""
    title = place.get("title") or ""
    n = max(SequenceMatcher(None, norm(nom), norm(title)).ratio(),
            SequenceMatcher(None, _core(nom), _core(title)).ratio())
    cp = re.search(r"\b\d{5}\b", adresse or "")
    p_addr = place.get("address") or ""
    if cp and cp.group(0) in p_addr:
        a = 1.0
    else:
        a = SequenceMatcher(None, norm(adresse), norm(p_addr)).ratio()
    return 0.6 * n + 0.4 * a


def pick_best(nom, adresse, places):
    """Retire les doublons internes à la réponse, puis garde le meilleur candidat."""
    seen, uniq = set(), []
    for p in places:
        key = p.get("placeId") or p.get("featureId") or (p.get("title"), p.get("address"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    ranked = sorted(((score(nom, adresse, p), -i, p) for i, p in enumerate(uniq)), reverse=True)
    best_score, _, best = ranked[0]
    margin = best_score - ranked[1][0] if len(ranked) > 1 else None
    return best, best_score, margin, len(places) - len(uniq)


def maps_address(p):
    """Adresse Maps, sans le nom de l'établissement que Google répète parfois en tête."""
    addr = (p.get("address") or "").strip()
    title = (p.get("title") or "").strip()
    if title and addr.lower().startswith(title.lower()):
        addr = addr[len(title):].lstrip(" ,;-").strip() or addr
    return addr


def empty_row(siren, nom):
    """Agence introuvable sur Maps : tous les champs Maps (dont l'adresse) restent vides."""
    return {c: "" for c in OUT_COLUMNS} | {"siren": siren, "nom": nom}


def build_row(siren, nom, p):
    def val(k):
        v = p.get(k)
        return "" if v is None else v
    return {
        "siren": siren, "nom": nom, "adresse": maps_address(p),
        "rating": val("rating"), "rating_count": val("ratingCount"),
        "phone_number": val("phoneNumber"), "website": val("website"),
        "horaires": fmt_hours(p.get("hours")),
        "featureId": p.get("featureId") or p.get("cid") or "", "placeId": val("placeId"),
    }


# ───────────────────────────── Fichiers ─────────────────────────────
def read_input(path):
    if not os.path.exists(path):
        sys.exit(f"❌ {path} introuvable (il doit être à la racine du repo)")
    rows, seen = [], set()
    st = Counter()
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f, delimiter=";")
        missing = {"siren", "nom", "adresse"} - set(rd.fieldnames or [])
        if missing:
            sys.exit(f"❌ colonnes manquantes dans {path} : {sorted(missing)}")
        for r in rd:
            siren = (r["siren"] or "").strip()
            if not siren or not (r["nom"] or "").strip():
                st["lignes_invalides"] += 1
            elif siren in seen:
                st["siren_en_double"] += 1
            else:
                seen.add(siren)
                rows.append({"siren": siren, "nom": r["nom"].strip(),
                             "adresse": (r["adresse"] or "").strip()})
    return rows, st


def read_done(path):
    done = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f, delimiter=";"):
                if r.get("siren"):
                    done[r["siren"]] = r
    return done


def rewrite(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, OUT_COLUMNS, extrasaction="ignore", **CSV_OPTS)
        w.writeheader()
        w.writerows(rows)


# ───────────────────────────── Compte rendu ─────────────────────────────
def capped(items, fmt):
    out = [f"- {fmt(x)}" for x in items[:LIST_CAP]]
    if len(items) > LIST_CAP:
        out.append(f"- … et {len(items) - LIST_CAP} autres (voir les logs du run)")
    return out or ["- aucun"]


def build_report(st, pool, total_in, total_unique, in_stats, n_done_before, n_todo, stop, elapsed, owners):
    c = st.count
    dup_groups = {k: v for k, v in owners.items() if len(v) > 1}
    processed = c["trouvees"] + c["introuvables"] + c["erreurs"]
    remaining = total_unique - n_done_before - c["trouvees"] - c["introuvables"]
    avg = (sum(st.latencies) / len(st.latencies)) if st.latencies else 0
    L = [
        "# Compte rendu — enrichissement des agences (Searlo Places)",
        "",
        f"- Fin : {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} — durée {elapsed / 60:.1f} min",
        f"- Arrêt : **{stop}**",
        "",
        "## Volumes",
        f"- Lignes lues dans `{INPUT_CSV}` : {total_in} "
        f"({in_stats['siren_en_double']} SIREN en double ignorés, "
        f"{in_stats['lignes_invalides']} lignes invalides ignorées)",
        f"- Déjà dans `{OUTPUT_CSV}` avant ce run (reprise) : {n_done_before}",
        f"- À traiter dans ce run : {n_todo} — traitées : **{processed}** — reste à faire au total : **{remaining}**",
        f"  - ✅ fiches trouvées : **{c['trouvees']}**",
        f"  - ❌ introuvables : **{c['introuvables']}**",
        f"  - 💥 erreurs (non écrites, seront retentées au prochain run) : **{c['erreurs']}**",
        "",
        "## Résultats multiples et doublons",
        f"- Réponses avec 2 résultats : {c['deux_resultats']} "
        f"(dont {c['ambigus']} ambiguës, {c['faible_confiance']} à faible confiance)",
        f"- Même fiche renvoyée 2 fois dans une réponse (dédoublonné) : {c['doublon_reponse']}",
        f"- Fiches Maps attribuées à plusieurs SIREN : **{len(dup_groups)}** groupe(s)",
        "",
        "## Performance",
        f"- Latence moyenne {avg:.1f}s — max {max(st.latencies, default=0):.1f}s",
        f"- Fiches récupérées entre {SLOW_S}s et {TIMEOUT_S:g}s (ratées avec un timeout de {SLOW_S}s) : "
        f"**{len(st.slow)}**",
        f"- Réponses en mode `lite` (infos réduites) : {c['lite']}",
        "",
        "## Requêtes réussies par clé (≈ crédits utilisés)",
    ]
    L += [f"- clé #{i + 1} …{k[-4:]} : {n}" for i, (k, n) in enumerate(zip(pool.keys, pool.ok_calls))]

    L += ["", f"## Ambiguës (2 résultats proches) — {len(st.ambigus)}"]
    L += capped(st.ambigus, lambda x: x)
    L += ["", f"## Faible confiance — {len(st.faibles)}"]
    L += capped(st.faibles, lambda x: x)
    L += ["", f"## Fiches partagées entre plusieurs SIREN — {len(dup_groups)}"]
    L += capped(list(dup_groups.items()),
                lambda kv: f"`{kv[0]}` : " + " ; ".join(f"{s} ({n})" for s, n in kv[1]))
    L += ["", f"## Récupérées lentement ({SLOW_S}s–{TIMEOUT_S:g}s) — {len(st.slow)}"]
    L += capped(st.slow, lambda x: x)
    L += ["", f"## Introuvables — {len(st.notfound)}"]
    L += capped(st.notfound, lambda x: x)
    L += ["", f"## Erreurs — {len(st.errors)}"]
    L += capped(st.errors, lambda x: x)
    return "\n".join(L) + "\n"


# ───────────────────────────── Programme principal ─────────────────────────────
def main():
    keys = [k for k in re.split(r"[;,\s]+", os.getenv("SEARLO_KEYS", "")) if k]
    if not keys:
        sys.exit("❌ SEARLO_KEYS vide : ajoute le secret SEARLO_KEYS (clés séparées par « ; »)")
    pool = KeyPool(keys)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    def _on_sigterm(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _on_sigterm)

    rows, in_stats = read_input(INPUT_CSV)
    done = read_done(OUTPUT_CSV)
    if RETRY_NOT_FOUND:
        kept = {s: r for s, r in done.items() if r.get("placeId") or r.get("featureId")}
        if len(kept) < len(done):
            log(f"🔁 RETRY_NOT_FOUND : {len(done) - len(kept)} introuvables remis dans la file")
            rewrite(OUTPUT_CSV, kept.values())
            done = kept
    n_done_before = len(done)

    todo = [r for r in rows if r["siren"] not in done]
    if MAX_ROWS:
        todo = todo[:MAX_ROWS]

    # fiche Maps -> [(siren, nom)] pour repérer les fiches attribuées à plusieurs SIREN
    owners = defaultdict(list)
    for s, r in done.items():
        k = r.get("featureId") or r.get("placeId")
        if k:
            owners[k].append((s, r.get("nom", "")))

    log(f"▶ {len(rows)} agences en entrée · {n_done_before} déjà faites · {len(todo)} à traiter "
        f"· limit={LIMIT} page={PAGE} gl={GL} hl={HL} · timeout {TIMEOUT_S:g}s/fiche "
        f"· {len(keys)} clé(s) : " + ", ".join(f"…{k[-4:]}" for k in keys))

    new_file = not os.path.exists(OUTPUT_CSV) or os.path.getsize(OUTPUT_CSV) == 0
    fout = open(OUTPUT_CSV, "a" if not new_file else "w", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(fout, OUT_COLUMNS, extrasaction="ignore", **CSV_OPTS)
    if new_file:
        writer.writeheader()
        fout.flush()

    st = SimpleNamespace(count=Counter(), latencies=[], slow=[], ambigus=[], faibles=[],
                         notfound=[], errors=[])
    c = st.count
    stop = "terminé"
    consecutive_errors = 0
    t_start = time.monotonic()
    width = len(str(len(todo)))
    session = requests.Session()

    try:
        for n, r in enumerate(todo, 1):
            if time.monotonic() - t_start > MAX_RUNTIME:
                stop = "limite de temps atteinte — relancer le workflow pour reprendre"
                break
            siren, nom, adresse = r["siren"], r["nom"], r["adresse"]
            tag = f"[{n:>{width}}/{len(todo)}] {siren}"

            try:
                data, dt = fetch(session, pool, nom, adresse)
            except AllKeysExhausted:
                stop = "toutes les clés Searlo sont épuisées"
                break
            except FicheError as e:
                c["erreurs"] += 1
                consecutive_errors += 1
                st.errors.append(f"{siren} — {nom} : {e}")
                log(f"{tag} 💥 {nom[:45]} → ERREUR : {e}")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    stop = f"{MAX_CONSECUTIVE_ERRORS} erreurs consécutives — arrêt de sécurité"
                    break
                continue
            consecutive_errors = 0
            st.latencies.append(dt)
            if data.get("lite"):
                c["lite"] += 1
            slow = ""
            if dt >= SLOW_S:
                slow = f" 🐢 récupérée en {dt:.0f}s"
                st.slow.append(f"{siren} — {nom} : {dt:.0f}s")

            places = data.get("places") or []
            if not places:
                writer.writerow(empty_row(siren, nom))
                fout.flush()
                c["introuvables"] += 1
                st.notfound.append(f"{siren} — {nom} ({adresse})")
                log(f"{tag} ❌ {nom[:45]} → introuvable · {dt:.1f}s · {pool.label}{slow}")
                continue

            best, sc, margin, n_dup = pick_best(nom, adresse, places)
            row = build_row(siren, nom, best)
            key = row["featureId"] or row["placeId"]

            flags = []
            if len(places) >= 2:
                c["deux_resultats"] += 1
            if n_dup:
                c["doublon_reponse"] += 1
                flags.append("même fiche renvoyée 2×")
            if margin is not None and margin < AMBIGUOUS_MARGIN:
                c["ambigus"] += 1
                msg = f"{siren} — {nom} → « {best.get('title')} » (écart {margin:.2f})"
                st.ambigus.append(msg)
                flags.append(f"ambigu (écart {margin:.2f})")
            if sc < LOW_CONFIDENCE:
                c["faible_confiance"] += 1
                st.faibles.append(f"{siren} — {nom} → « {best.get('title')} » (score {sc:.2f})")
                flags.append(f"faible confiance ({sc:.2f})")
            if key and owners[key]:
                flags.append("fiche déjà liée à " + ", ".join(s for s, _ in owners[key]))
            if key:
                owners[key].append((siren, nom))

            writer.writerow(row)
            fout.flush()
            c["trouvees"] += 1

            warn = any(f.startswith(("ambigu", "faible", "fiche déjà")) for f in flags)
            rating = f"{row['rating']}★ ({row['rating_count']})" if row["rating"] != "" else "pas de note"
            log(f"{tag} {'⚠️' if warn else '✅'} {nom[:40]} → « {str(best.get('title'))[:40]} » · {rating} "
                f"· {row['phone_number'] or 'pas de tél'} · {len(places)} rés. · {dt:.1f}s · {pool.label}"
                f"{slow}{' · ' + ' · '.join(flags) if flags else ''}")
    except KeyboardInterrupt:
        stop = "interrompu (annulation du run)"
    finally:
        fout.close()

    if stop == "terminé" and c["erreurs"]:
        stop = f"terminé — {c['erreurs']} erreur(s) seront retentées au prochain run"

    report = build_report(st, pool, len(rows) + in_stats["siren_en_double"] + in_stats["lignes_invalides"],
                          len(rows), in_stats, n_done_before, len(todo), stop, time.monotonic() - t_start, owners)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(report)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)
    log("\n" + "=" * 70 + "\n" + report)

    if stop.startswith(("toutes les clés", f"{MAX_CONSECUTIVE_ERRORS} erreurs")):
        sys.exit(1)


if __name__ == "__main__":
    main()
