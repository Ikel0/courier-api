# Fiche de travail : Courier API

## Intention

Je voulais partir d’une question simple : une API peut avoir des routes propres mais rester pénible à intégrer si elle ne donne pas de réponse aux incidents ordinaires. Que se passe-t-il quand le client rejoue une requête ? Quand une intégration webhook est en erreur ? Comment retrouver l’appel sans copier des logs bruts ?

Courier API est donc un terrain de travail autour de ces cas, avec un domaine volontairement lisible : colis, changements de statut et notifications sortantes.

## Décisions prises

| Sujet | Choix | Pourquoi |
| --- | --- | --- |
| Contrat | FastAPI + OpenAPI généré | Garder les exemples, validations et documentation dans le même code. |
| Authentification | Clé API via `X-API-Key` ou Bearer | Couvrir les deux usages les plus fréquents sans ajouter un fournisseur d’identité factice. |
| Rejeu | Table d’idempotence atomique | Une même clé + même corps rejoue la réponse, un corps différent retourne `409`. |
| Pagination | Curseur encodant date + identifiant | Éviter les problèmes de décalage habituels d’un offset pendant des insertions. |
| Webhooks | HMAC SHA-256, horodatage, identifiant de livraison | Donner au destinataire les éléments nécessaires pour vérifier la provenance et dédupliquer. |
| Audit | Request ID transmis et table dédiée | Pouvoir partir d’un identifiant donné par un client jusqu’à l’appel correspondant. |

## Scénario à tester

1. Créer un endpoint webhook avec `shipment.created`.
2. Créer un colis avec une clé d’idempotence.
3. Rejouer exactement la même requête : l’identifiant est identique, l’en-tête de rejeu apparaît.
4. Essayer le même idempotency key avec une autre référence : l’API refuse le conflit.
5. Consulter le journal de livraison webhook et le journal d’audit.

## Ce que le projet ne prétend pas résoudre

SQLite, un rate limiter en mémoire et `BackgroundTasks` sont de bons outils pour rendre ce dépôt utilisable sans services externes. Ils ne remplacent pas Postgres, Redis et une file durable lorsque plusieurs réplicas, des redémarrages ou des garanties de livraison sont nécessaires.

Le contrôle SSRF refuse les URL sans HTTPS, locales et privées. Une défense complète doit également exister au niveau réseau, car la résolution DNS peut changer après l’enregistrement de l’URL.

## Suite crédible

- pousser les tentatives vers une table outbox transactionnelle puis vers un worker ;
- ajouter une stratégie de retry exponentiel avec jitter et une dead-letter queue ;
- signer aussi les réponses sortantes à un niveau de versionnement de schéma ;
- ajouter des tests de contrat avec un consommateur webhook de référence ;
- mesurer les latences par route et par tentative, avec des traces OpenTelemetry.
