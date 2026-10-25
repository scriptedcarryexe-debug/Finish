# Compte rendu — enrichissement des agences (Searlo Places)

- Fin : 25/10/2026 16:37:59 — durée 0.4 min
- Arrêt : **terminé**

## Volumes
- Lignes lues dans `leboncoin_extraction_final.csv` : 7503 (0 SIREN en double ignorés, 0 lignes invalides ignorées)
- Déjà dans `agences.csv` avant ce run (reprise) : 0
- À traiter dans ce run : 10 — traitées : **10** — reste à faire au total : **7493**
  - ✅ fiches trouvées : **10**
  - ❌ introuvables : **0**
  - 💥 erreurs (non écrites, seront retentées au prochain run) : **0**

## Résultats multiples et doublons
- Réponses avec 2 résultats : 3 (dont 2 ambiguës, 0 à faible confiance)
- Même fiche renvoyée 2 fois dans une réponse (dédoublonné) : 0
- Fiches Maps attribuées à plusieurs SIREN : **0** groupe(s)

## Performance
- Latence moyenne 2.4s — max 4.5s
- Fiches récupérées entre 15s et 30s (ratées avec un timeout de 15s) : **0**
- Réponses en mode `lite` (infos réduites) : 10

## Requêtes réussies par clé (≈ crédits utilisés)
- clé #1 …496e : 10
- clé #2 …bc67 : 0
- clé #3 …5630 : 0

## Ambiguës (2 résultats proches) — 2
- 538913138 — - LG IMMO - Cabinet GARÉCHÉ → « - LG IMMO - Cabinet Garéché » (écart 0.00)
- 500316377 — 1001 Adresse → « 1001 Adresses » (écart 0.00)

## Faible confiance — 0
- aucun

## Fiches partagées entre plusieurs SIREN — 0
- aucun

## Récupérées lentement (15s–30s) — 0
- aucun

## Introuvables — 0
- aucun

## Erreurs — 0
- aucun
