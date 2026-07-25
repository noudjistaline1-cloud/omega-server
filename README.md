# AEGIS v3 — Système de trading automatique, intelligent, robuste, sécurisé, auto-apprenant

Système **neuf, indépendant de STALINE**. EA MQL5 (exécution) + backend Python FastAPI
(décision + double apprentissage). Marchés : Crypto, Or (XAU), Forex. Compatible
**n'importe quelle taille de compte**. Testé fonctionnellement (voir section Tests).

## Ce qui est COMPLET en v3

### 1. Filtre calendrier économique (FRED)
Utilise l'API FRED — celle que tu utilises déjà sur STALINE (NFP/JOLTS/Beige Book).
- Récupère les dates de publication des séries économiques configurées
- Bloque le trading FOREX/METAL le jour d'une publication à fort impact (NFP, CPI, FOMC...)
- **Limite honnête et assumée** : l'endpoint FRED `release/dates` renvoie une **date**, pas une
  heure précise. Le filtre bloque donc toute la journée de publication, pas une fenêtre de
  30 minutes. C'est un choix de robustesse — je n'ai pas voulu simuler une précision horaire
  que l'API ne fournit pas.
- Dégradation propre : sans clé FRED configurée, le filtre est inactif, ne bloque rien,
  ne plante rien.
- **Action requise de ta part** : trouver les `release_id` FRED des séries qui t'intéressent
  sur https://fred.stlouisfed.org/releases (l'ID est dans l'URL de chaque publication, ex.
  Employment Situation, Consumer Price Index, FOMC Meetings) et les mettre dans
  `AEGIS_FRED_RELEASE_IDS`. Je n'ai pas deviné ces IDs pour éviter de filtrer sur du faux.

### 2. Modèle ML auto-entraîné (régression logistique en ligne)
En plus des poids "perceptron-like" de la v2 (conservés), un vrai modèle probabiliste :
- Prédit P(trade gagnant | tendance H1, momentum, tendance H4, régime de volatilité, coût du spread)
- S'entraîne par descente de gradient stochastique à CHAQUE `/trade_result` reçu
- Démarre neutre (50%), n'a besoin d'aucun historique — apprend en live, trade après trade
- Sert de second filtre (`ML_LOW_PROBABILITY`) une fois assez de trades appris (20 par défaut)
- Module la taille de position (0.5x à 1.5x le risque de base selon sa confiance)
- **Testé** : après 25 trades simulés à 64% de réussite, la probabilité prédite est passée
  de 0.50 à 0.60 et le lot s'est ajusté automatiquement en conséquence (voir logs de test).

### Le reste (v1 + v2, inchangé)
Sizing universel par % de risque, confirmation multi-timeframe H1/H4, régime de volatilité,
garde-fou d'exposition/corrélation par groupe d'actifs, boucle de feedback fermée,
sécurité (auth, rate limit, validation stricte, kill switch double, circuit breaker),
robustesse (retry/backoff, persistance atomique, fail-safe réseau), dashboard de supervision.

## Architecture

```
MT5 (AEGIS_EA.mq5) --POST /signal--> Backend (aegis_server.py)
                                        |-- filtre calendrier (FRED)
                                        |-- scoring à règles (poids adaptatifs)
                                        |-- gate + sizing par modèle ML
                                        |-- garde-fou exposition
                                     --{action,lot,sl,tp,signal_id,ml_probability}-->
       |                                     ^
       | (position fermée)                   |
       +---------- POST /trade_result --------+  (profit réel -> apprentissage règles + ML)
```

## Sécurité

| Mécanisme | Où | Détail |
|---|---|---|
| Auth API | Backend | Header `X-API-KEY`, comparaison à temps constant |
| Rate limiting | Backend | 120 req/min/IP |
| Validation stricte | Backend | Pydantic, rejet propre des payloads malformés |
| Kill switch double | EA + Backend | Fichier local `AEGIS_KILL.flag` + endpoint `/killswitch` |
| Circuit breaker | EA + Backend (redondant) | Stop si drawdown journalier ≥ seuil |
| Filtre calendrier | Backend | Coupe le trading FOREX/METAL les jours de news majeures |
| Fail-safe réseau | EA | Backend injoignable = **aucun trade** |
| Validation défensive | EA | Toute réponse serveur revalidée avant exécution |
| Pas de secrets en dur | Backend | Clé API + clé FRED via variables d'environnement |

## Robustesse

- Retry + backoff progressif sur tous les appels HTTP
- Mapping position→signal_id persisté sur disque côté EA (survit à un crash MT5)
- État serveur (poids règles, poids ML, exposition, circuit breaker) persisté en écriture atomique
- Cache calendrier FRED avec dégradation gracieuse (garde le cache précédent si l'API est down)
- Purge automatique des signaux en attente > 3 jours
- Respect strict des contraintes broker

## Sizing universel

```
risk_amount = equity * (risk_percent * volatility_scale * ml_confidence_scale / 100)
lot_brut    = risk_amount / (sl_points * tick_value)
lot_final   = clamp(round_to_step(lot_brut), lot_min, lot_max)
```

## Déploiement

```bash
cd AEGIS
pip install -r requirements.txt
export AEGIS_API_KEY="une_cle_longue_et_aleatoire"
export AEGIS_DAILY_LOSS_HALT_PCT=3.0
export AEGIS_MAX_GROUP_EXPOSURE=2
export AEGIS_LEARNING_RATE=0.05
export AEGIS_ML_LEARNING_RATE=0.10
export AEGIS_ML_MIN_SAMPLES=20
export AEGIS_ML_MIN_PROBABILITY=0.40

# Optionnel — filtre calendrier économique (sinon désactivé proprement)
export AEGIS_FRED_API_KEY="ta_cle_fred"
export AEGIS_FRED_RELEASE_IDS="10,50"   # à vérifier sur fred.stlouisfed.org/releases
export AEGIS_CALENDAR_GROUPS="FOREX,METAL"

python aegis_server.py
```

Côté MT5 : identique à la v2 (voir `AEGIS_EA.mq5`, inputs `InpServerURL` / `InpTradeResultURL`
/ `InpApiKey`). Aucun changement EA nécessaire pour le calendrier ou le ML — tout est côté backend.

Dashboard : ouvrir `dashboard.html`, renseigner l'URL backend + la clé API.

## Tests effectués

Le backend a été testé fonctionnellement avant livraison (`TestClient` FastAPI) :
- `/health`, `/signal`, `/trade_result`, `/stats` répondent correctement
- Auth invalide → 401, payload malformé → 422 (pas de crash silencieux)
- Cycle complet signal → trade_result → apprentissage vérifié sur 25 trades simulés :
  les poids par règles ET le modèle ML convergent de façon cohérente avec le taux de
  réussite simulé (64% → `ml_p` monte de 0.50 à 0.60, lot ajusté de 0.20 à 0.22)

Ce qui n'a PAS pu être testé ici (nécessite ton environnement) : l'EA MQL5 en conditions
réelles MT5 (compilation, WebRequest, OnTradeTransaction), et le filtre calendrier FRED
(nécessite une vraie clé API et un accès réseau que ce sandbox n'a pas).

## Arrêt d'urgence

- **Local immédiat** : fichier vide `AEGIS_KILL.flag` dans `MQL5/Files/`.
- **Global** : `POST /killswitch {"engage": true}` avec la clé API.

## Limites restantes, assumées

- Le modèle ML est une régression logistique en ligne — volontairement simple pour rester
  interprétable et sans dépendance à un pipeline d'entraînement externe. Un modèle plus
  lourd (gradient boosting, réseau de neurones) demanderait un historique de données
  conséquent et une infrastructure de réentraînement périodique — étape distincte si tu
  la veux un jour, une fois que tu auras accumulé des centaines de trades réels.
- Le filtre calendrier bloque à la journée, pas à l'heure (limite de l'API FRED elle-même).
- Le pont avec le World Scanner de STALINE n'a pas été fait — tu as choisi un système séparé.
