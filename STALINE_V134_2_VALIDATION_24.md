# STALINE — Validation des 24 corrections

## EA V131.33
1. FASTCAP/ShadowSL/CloseGuard/Secure80/BELOCK/ProfitLock/ProfitTrail/VSL/RVP/CascadeLock: sorties profit centralisées par `EXIT_CanCloseCentral`.
2. MSE central: IMPULSE, PULLBACK, REACCEL, EXHAUSTION, REVERSAL, CHAOS.
3. MSE: vitesse, accélération, retracement, durée, ATR relatif, structure, volume/tick-flow proxy, spread/liquidité, MFE/MAE.
4. TP1: 0.75R → BE+buffer, pas de fermeture mécanique du runner.
5. TP2: 1.35R → lock 20–30% uniquement si la sortie est autorisée par MSE.
6. TP3: 3R → runner, trailing structurel/1.5R et extension.
7. FASTCAP: seuil de pic remis à 5€; filet zéro-cross séparé.
8. MR_REV: passe par MSE avant reverse.
9. HEDGE2: passe par MSE avant hedge.
10. Cooldown: 3 pertes consécutives → 60s; un gain ne crée pas de cooldown.
11. EVENT_REARM: OnTradeTransaction queue le symbole; OnTick réarme sans attendre un timer de refresh.
12. Broker retry: RetryCount/Pipeline_RetryCount conservés; pas de cooldown marché déguisé.
13. Indicateurs: SymbolSelect/SymbolIsSynchronized via ST_EnsureSymbolReady.
14. Handles ATR/ADX/EMA: cache persistant ajouté pour les helpers critiques; libération à OnDeinit.
15. Fermetures directes MqlTradeRequest: garde centrale ajoutée aux chemins de fermeture critiques; le seul fallback req2 restant est précédé par la requête protégée.
16. Secure80: le flag emergency ne bypass plus MSE pour un gain positif; seuls les vrais hard-emergency callers peuvent le faire.
17. Anti-giveback: ZERO_CROSS/FASTCAP_ZERO est la seule exception profit courte distance au gate MSE.
18. Timer: EventSetMillisecondTimer(100), sans attendre un refresh seconde après clôture.

## Serveur V134.2
19. `/score`: erreur interne → réponse structurée `NO_TRADE` HTTP 200, jamais 500.
20. `v30_local_edge_recovery`: présent et limité aux stages qui le demandent; le fallback global `/score` est fail-closed.
21. DFE: 4 piliers, macro 45%, historique 25%, trades 5%, micro 25%; conflit historique/micro arbitré au lieu d'un veto historique souverain.
22. OMEGA: profils XAU/XAG/BTC déjà micro-renforcés; profil XAG indépendant; asset micro profile ajouté.
23. CoinGecko: Binance primaire, cache 15 min, stagger, backoff 300s après 429, CryptoCompare/Yahoo en fallback; suppression de l'appel parallèle systématique CoinGecko+Binance.
24. Diagnostic/robustesse: `/score/stability` jusqu'à 100 passages; CCE symmetry check déjà différé; `py_compile` et import test passent.

## Vérifications effectuées
- Python AST / `py_compile`: PASS.
- Import du serveur final: PASS sans NameError/SyntaxError/Traceback au démarrage.
- Test DFE micro: PASS.
- Test profil BTC/XAU/XAG: fonctions présentes.
- Test fallback `/score` avec exception injectée: retourne `NO_TRADE`, `DEGRADED_PIPELINE_FALLBACK`, HTTP 200.
- MQL5: présence des nouvelles fonctions et paramètres vérifiée statiquement. MetaEditor/compilateur MQL5 n'est pas disponible dans l'environnement de travail, donc le `.mq5` n'est pas déclaré « compilé » ici.

## Dépendance externe non fabriquée
Le serveur peut démarrer sans `HISTORICAL_STATS_ENGINE.py`; dans ce cas le pilier historique 10 ans reste indisponible/neutre et le serveur ne doit pas en faire un veto souverain. Je n'ai pas fabriqué de faux dataset 10 ans pour prétendre que cette donnée existe.
