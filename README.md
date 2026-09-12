# Marriott MMA rate watcher — bot Telegram

## Ce qui marche déjà
- Commandes `/watch`, `/list`, `/stop`, `/help`
- Parsing de `1-10/10` en plage de nuits, et `250€` en prix max
- Résolution de ville générique (tape le nom sur marriott.com comme un humain)
- Boucle de scan (toutes les `SCAN_INTERVAL_SECONDS`, 60s par défaut)
- Dédup par (hôtel, nuit) — un hôtel déjà alerté pour une nuit donnée ne
  revient pas, mais un autre hôtel la même nuit ou le même hôtel une autre
  nuit peuvent redéclencher une alerte
- Filtrage optionnel par liste d'hôtels (envoie un lien marriott.com après
  `/watch` pour restreindre)

## Ce qu'il reste à affiner (important, lis ça avant de lancer)
`scraper.py` contient deux points marqués dans le code :

1. **`resolve_destination()`** — le sélecteur du champ de recherche
   (`input#search-form--input, ...`) est une estimation. Si ça ne marche
   pas, ouvre marriott.com, fais un clic droit sur le champ de destination →
   Inspecter, et donne-moi le vrai sélecteur.

2. **`extract_hotels_from_page()`** — scraping du DOM en solution de repli.
   Marriott charge très probablement les résultats via un appel
   fetch/XHR interne qui renvoie du JSON — ce serait beaucoup plus fiable à
   parser que des classes CSS qui peuvent changer à chaque déploiement.
   Pour finaliser ça : ouvre une recherche dans un navigateur, onglet
   Réseau (Network) → filtre Fetch/XHR, recharge, trouve la requête qui
   renvoie la liste d'hôtels, et envoie-moi son URL + un exemple de réponse
   JSON. Je réécris le parseur dessus directement.

Sans ces deux ajustements, le bot tournera mais risque de ne rien trouver.

## Installation locale (test)
```bash
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium   # dépendances système, si besoin
```

Crée un fichier `.env` :
```
TELEGRAM_BOT_TOKEN=xxxxx
SCAN_INTERVAL_SECONDS=60
DELAY_BETWEEN_NIGHTS_SECONDS=3
```

Lance :
```bash
python bot.py
```

## Déploiement sur Render (comme ton bot Virtuals)
1. Push ce dossier sur GitHub.
2. Nouveau **Background Worker** sur Render (pas un Web Service — ce bot ne
   sert pas de page web, il fait juste polling + scan en boucle).
3. Build command : `pip install -r requirements.txt && playwright install --with-deps chromium`
4. Start command : `python bot.py`
5. Variables d'environnement : `TELEGRAM_BOT_TOKEN` (et les autres si tu
   veux changer les défauts).
6. Si tu veux que les watches survivent aux redéploiements, ajoute un
   **Render Disk** monté sur `/data` et mets `DATA_PATH=/data/watches.json`.

## Point d'attention : anti-bot
Marriott utilise très probablement une protection type Akamai/PerimeterX
sur son moteur de recherche. Scanner beaucoup de nuits toutes les 60
secondes peut se faire repérer (captcha, blocage d'IP). Si ça arrive :
- augmente `SCAN_INTERVAL_SECONDS` et `DELAY_BETWEEN_NIGHTS_SECONDS`
- envisage un service de proxy résidentiel si le blocage persiste
- ou réduis le nombre de nuits scannées par cycle (ex: alterner un
  sous-ensemble des nuits à chaque passage plutôt que tout scanner d'un coup)
