# STALINE Creative Engine Bridge 2.3

Cette version conserve le service Render/Printify existant et change la logique vers **creation-first**.

## À déployer dans le dépôt existant
- Remplacer `main.py`.
- Ajouter `STALINE_CREATIVE_ENGINE_2_3.md`.
- Ajouter `creative_schema_2_3.json`.
- Conserver `requirements.txt` si ton service actuel fonctionne.
- Conserver les variables Render existantes (`PRINTIFY_API_TOKEN`, `PRINTIFY_SHOP_ID`).
- Conserver les trois références dans `assets/`, notamment `staline_last_lookbook_reference.png`.

## Logique
ChatGPT crée d'abord la pièce originale. Le bridge sélectionne ensuite le fournisseur/blueprint compatible avec la création approuvée. Les images de modèles/lookbook sont séparées des artworks de production.

## Render
Build: `pip install -r requirements.txt`
Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Ne mets jamais les tokens Printify dans GitHub.
