# Courier API

Courier API est un projet personnel pour travailler les choix qui rendent une API agréable à intégrer et exploitable quand elle tourne : contrat OpenAPI, authentification, idempotence, pagination par curseur, webhooks signés, audit et sondes de santé.

L’interface à la racine est un playground. Elle appelle la vraie API et permet de générer une clé de démonstration éphémère. Ce n’est pas une réclamation d’expérience client ou de volume de production.

## Ce qui est concret

- FastAPI et contrat disponible sur [`/docs`](http://localhost:8000/docs) et `/openapi.json`
- clés statiques `X-API-Key` **ou** `Authorization: Bearer …`
- clés de playground limitées dans le temps, créées avec `POST /v1/keys/demo`
- `Idempotency-Key` stockée de manière atomique, avec rejeu de réponse et conflit si le corps change
- liste de livraisons et de colis paginée par curseur stable
- webhooks enregistrés, signés en HMAC SHA-256 et accompagnés d’un journal de tentatives/retry
- identifiant de requête `X-Request-ID`, journal d’audit SQLite et métriques simples
- `GET /healthz` (liveness), `GET /readyz` (lecture SQLite), `GET /health` (playground)
- image Docker, Blueprint Render et CI GitHub Actions

## Démarrer localement

Python 3.11 ou plus récent est recommandé.

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
export PYTHONPATH=src
uvicorn courier_api.main:app --reload
```

Ensuite ouvrir <http://localhost:8000>. La documentation OpenAPI reste disponible sur <http://localhost:8000/docs>.

## Test en moins d’une minute

Ouvre l’interface puis clique sur **Tester le parcours complet**. Courier enchaîne la création d’une clé de démo, le dépôt d’une livraison et la lecture du journal. Chaque réponse reste visible dans le studio et le bouton peut être rejoué pour observer l’idempotence.

Courier ne dépend volontairement d’aucune source publique : son sujet est le comportement d’une API, pas l’enrichissement de données. Si le backend ne répond pas, l’interface l’indique et présente un scénario local sans le faire passer pour une réponse réelle.

## Parcours détaillé

1. Dans le playground, créer une clé de démo de 30 minutes.
2. Ouvrir `POST /v1/deliveries`, conserver l'`idempotency_key` proposée et envoyer la demande.
3. Renvoyer la même demande : la réponse est rejouée avec `Idempotency-Replayed: true` au lieu de créer une seconde livraison.
4. Ouvrir `GET /v1/deliveries` ou le journal de l'interface pour retrouver le `trace_id`.

Le mode local de l'interface est volontairement signalé quand le backend ne répond pas. Il sert uniquement à parcourir le contrat, pas à faire croire que la requête a été envoyée.

Pour injecter le jeu de données fictif de l’interface sans faire aucun appel réseau :

```bash
PYTHONPATH=src python -m courier_api.seed
```

## Essayer l’API

La clé de l’exemple est uniquement locale. Changez-la dans un environnement partagé.

```bash
export COURIER_KEY='local_demo_key_change_me'

curl -X POST http://localhost:8000/v1/shipments \
  -H "X-API-Key: $COURIER_KEY" \
  -H 'Idempotency-Key: order-2026-042-create' \
  -H 'Content-Type: application/json' \
  -d '{
    "reference":"ORDER-2026-042",
    "recipient": {
      "name":"Maya N’Diaye", "email":"maya@example.test",
      "address_line1":"12 rue des Lilas", "postal_code":"75011",
      "city":"Paris", "country_code":"FR"
    },
    "service_level":"express",
    "parcels":[{"weight_grams":850,"length_cm":20,"width_cm":15,"height_cm":8}]
  }'
```

Le même appel avec la même clé d’idempotence retourne le même colis et l’en-tête `Idempotency-Replayed: true`. Avec un corps différent, l’API retourne `409` : c’est volontaire.

Créer une intégration webhook :

```bash
curl -X POST http://localhost:8000/v1/webhook-endpoints \
  -H "X-API-Key: $COURIER_KEY" \
  -H 'Idempotency-Key: webhooks-billing-2026' \
  -H 'Content-Type: application/json' \
  -d '{
    "url":"https://example.com/hooks/courier",
    "events":["shipment.created","shipment.status_changed"]
  }'
```

La réponse donne un `secret` une seule fois. Il sert à vérifier le contenu reçu :

```text
signature = HMAC_SHA256(secret, "<X-Courier-Timestamp>.<raw request body>")
X-Courier-Signature: v1=<signature>
```

## Surfaces principales

| Groupe | Endpoint | Rôle |
| --- | --- | --- |
| Service | `GET /healthz`, `GET /readyz`, `GET /metrics` | Liveness, readiness, métriques |
| Playground | `POST /v1/keys/demo` | Génère une clé Bearer limitée à 60 min |
| Playground | `POST/GET /v1/deliveries` | Crée/lit une demande sortante avec trace et idempotence |
| Colis | `POST/GET /v1/shipments` | Crée/liste des envois, curseurs stables |
| Colis | `PATCH /v1/shipments/{id}/status` | Transition d’état contrôlée |
| Webhooks | `POST/GET /v1/webhook-endpoints` | Enregistre et liste les abonnements |
| Webhooks | `GET .../{id}/deliveries`, `POST .../{delivery}/retry` | Consulte/rejoue les tentatives |
| Opérations | `GET /v1/audit-events` | Journal compact des appels |

Les mutations nécessitent une clé API. Les routes de création exigent une clé d’idempotence.

## Vérifier le projet

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m py_compile src/courier_api/*.py
docker build -t courier-api .
docker run --rm -p 10000:10000 \
  -e COURIER_API_KEYS='local:replace-this-key' \
  courier-api
```

La CI reprend les tests sur chaque push et pull request.

## Déploiement Render

Le fichier [`render.yaml`](render.yaml) peut être utilisé comme Blueprint. Render génère une valeur de `COURIER_API_KEYS`; une valeur brute est volontairement supportée et devient une clé nommée `render` en interne.

Pour une vraie intégration, il faut au minimum :

1. remplacer SQLite par Postgres et chiffrer les secrets webhook au repos ;
2. déplacer l’envoi sortant vers une file durable et un worker séparé ;
3. externaliser la limite de débit vers Redis ;
4. appliquer une politique d’egress/DNS contre le rebinding, en plus de la garde SSRF applicative ;
5. désactiver `COURIER_ENABLE_DEMO_KEYS` et `COURIER_SEED_DEMO_DATA`.

Ces limites sont documentées pour ne pas présenter le projet comme une plateforme prête à recevoir du trafic métier critique telle quelle.

## Structure

```text
src/courier_api/
  main.py       routes, middleware request-id, OpenAPI
  security.py   clés, rate limit, idempotence, curseurs, signature
  db.py         schéma SQLite et repositories
  delivery.py   tentatives sortantes et journalisation
  seed.py       scénario fictif idempotent pour l’interface
web/            playground statique servi par FastAPI
tests/          tests d’intégration HTTP et sécurité
```

Voir aussi la [fiche de travail](docs/working-paper.md) pour les hypothèses, les choix et la suite envisagée.
