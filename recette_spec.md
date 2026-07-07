# Module Odoo v19 Enterprise — Réservations Client & Encaissements Opérationnels (Option B)
**Nom technique (suggestion)** : `stock_customer_reservation_cash`  
**Nom affiché** : *Réservations Clients & Encaissements (Opérationnel)*  
**Version cible** : Odoo v19 Enterprise  
**Objectif** : Gérer des **réservations de stock dédiées à un client** et des **encaissements “opérationnels”** (non systématiquement comptabilisés), avec possibilité **optionnelle** de générer plus tard les écritures via `account.payment` dans les journaux appropriés.

---

## 1) Contexte & Problématique
Certaines commandes sont “anormales” :  
- On doit **réserver du stock** pour un client **sans** confirmer un BC/SO standard.
- On doit **encaisser partiellement** (ou totalement) sans “polluer” systématiquement la comptabilité.
- On doit suivre, par réservation/client :
  - **Stock réservé**
  - **Montant encaissé (opérationnel)**
  - **Montant restant à encaisser**
  - **Montant comptabilisé** (si/ quand la compta le décide)

En parallèle, les flux “normaux” (SO/BC → livraison → facture → paiement) restent inchangés.

---

## 2) Principes de conception (non négociables)
1. **Réservation stock = transfert interne** (Inventory)  
   - Déplacer physiquement/logiquement le stock de `WH/Stock` vers `WH/Reserved` (ou une location dédiée).
2. **Encaissement opérationnel ≠ compta**  
   - Un modèle intermédiaire capture la réalité métier : `reservation.cash_in`.
   - La comptabilisation est **optionnelle**, explicite, contrôlée par droits.
3. **Comptabiliser = générer un objet standard**  
   - Si demandé : création d’un `account.payment` (inbound customer) dans le journal paramétré.
   - Jamais de “mini-compta” maison pour remplacer Odoo Accounting.
4. **Traçabilité totale**  
   - Lien réservation ↔ transfert interne ↔ encaissements opérationnels ↔ (optionnel) paiements compta.
5. **Multi-company / Multi-warehouse**  
   - Isolation des données par société, configuration par entrepôt si nécessaire.

---

## 3) Dépendances
- `stock`
- `stock_account` (si valorisation / liens accounting nécessaires)
- `account`
- Optionnel selon besoins :
  - `sale` (si on veut la conversion vers devis plus tard)
  - `mail` (chatter, activités)
  - `documents` (pièces justificatives, si client Odoo Documents)

---

## 4) Arborescence (indicative)
stock_customer_reservation_cash/
├─ init.py
├─ manifest.py
├─ models/
│ ├─ reservation.py
│ ├─ reservation_line.py
│ ├─ cash_in.py
│ ├─ payment_mode.py
│ ├─ warehouse_config.py
│ └─ account_payment_bridge.py
├─ wizards/
│ ├─ reservation_create_transfer_wizard.py
│ ├─ cash_in_post_wizard.py
│ ├─ cash_in_mark_to_account_wizard.py
│ └─ reservation_release_wizard.py
├─ security/
│ ├─ ir.model.access.csv
│ ├─ security.xml
│ └─ record_rules.xml
├─ data/
│ ├─ sequences.xml
│ └─ default_payment_modes.xml (optionnel)
├─ views/
│ ├─ reservation_views.xml
│ ├─ cash_in_views.xml
│ ├─ payment_mode_views.xml
│ ├─ warehouse_config_views.xml
│ └─ menus.xml
└─ README.md

---

## 5) Modèles (Data Model)

### 5.1 `stock.customer.reservation` — Réservation Client
**But** : porter l’objet métier “réservation” et son état.  
**Champs principaux** :
- Identification
  - `name` (Char) — séquence `RES/%(year)s/%(month)s/%(seq)s`
  - `company_id` (Many2one `res.company`, required)
  - `warehouse_id` (Many2one `stock.warehouse`, required)
  - `partner_id` (Many2one `res.partner`, required)
  - `reservation_date` (Datetime, default=now)
  - `expiry_date` (Datetime, optionnel)
- Lignes
  - `line_ids` (One2many `stock.customer.reservation.line`)
- Stock (traces)
  - `picking_id` (Many2one `stock.picking`) — transfert interne créé pour réserver
  - `release_picking_ids` (One2many `stock.picking`) — transferts internes de libération (optionnel)
  - `reserved_location_id` (Many2one `stock.location`, computed ou config)
- Encaissements
  - `cash_in_ids` (One2many `reservation.cash_in`)
  - `amount_expected` (Monetary) — valeur attendue/à encaisser (saisie ou calcul)
  - `currency_id` (Many2one `res.currency`, required, par défaut société)
  - `amount_received_operational` (Monetary, computed) — somme cash_in en état `confirmed` + `accounted` (paramétrable)
  - `amount_accounted` (Monetary, computed) — somme cash_in en état `accounted`
  - `amount_due` (Monetary, computed) — `amount_expected - amount_received_operational`
- Statuts & workflow
  - `state` (Selection) :
    - `draft` (brouillon)
    - `reserved` (stock réservé via transfert interne validé)
    - `partially_released` (une partie libérée)
    - `closed` (terminée)
    - `cancelled` (annulée)
- Notes & suivi
  - `note` (Text)
  - `message_follower_ids` / `activity_ids` (si `mail.thread` activé)

**Contraintes** :
- `company_id` doit être cohérent avec `warehouse_id.company_id`
- Impossible de passer à `reserved` sans lignes
- Quantités > 0
- Si lot/serial requis sur produit : gérer dans les moves du picking (voir §7)

---

### 5.2 `stock.customer.reservation.line`
Champs :
- `reservation_id` (Many2one, required)
- `product_id` (Many2one `product.product`, required)
- `product_uom_id` (Many2one `uom.uom`, required)
- `quantity` (Float, required)
- Optionnel :
  - `lot_id` (Many2one `stock.lot`) si suivi lot/serial souhaité
  - `price_unit_ref` (Monetary) si on veut estimer `amount_expected`
  - `amount_line` (Monetary computed) = qty * unit

---

### 5.3 `reservation.cash_in` — Encaissement Opérationnel (modèle intermédiaire)
**But** : tracer l’encaissement côté opérationnel, sans comptabiliser forcément.  
Champs :
- `name` (Char) — séquence `CIN/%(year)s/%(month)s/%(seq)s`
- `reservation_id` (Many2one, required)
- `partner_id` (Many2one, related de `reservation.partner_id`, stored)
- `company_id` (Many2one, related reservation, stored)
- `warehouse_id` (Many2one, related reservation, stored)
- `amount` (Monetary, required)
- `currency_id` (Many2one, required)
- `date` (Date, required, default=today)
- `payment_mode_id` (Many2one `reservation.payment.mode`, required)
- `reference` (Char) — reçu / transaction id / n° chèque
- `attachment_ids` (Many2many `ir.attachment`) — preuves
- Workflow :
  - `state` (Selection) :
    - `draft` (saisie)
    - `confirmed` (validé opérationnellement, verrouillé)
    - `to_account` (marqué à comptabiliser)
    - `accounted` (comptabilisé)
    - `cancelled` (annulé)
    - `refunded` (remboursé, si gestion incluse)
- Pont compta (vides si non posté) :
  - `account_payment_id` (Many2one `account.payment`)
  - `accounting_date` (Date)
  - `accounted_by` (Many2one `res.users`)

**Règles** :
- `confirmed` : champs critiques verrouillés (`amount`, `payment_mode_id`, `date`, `reservation_id`)
- `to_account` ne peut être atteint que si `confirmed`
- `accounted` ne peut être atteint que si `to_account` ET mapping journal OK
- Idempotence : si `account_payment_id` déjà défini → interdiction de re-post

---

### 5.4 `reservation.payment.mode` — Mode de paiement opérationnel (config)
Champs :
- `name` (Char, required)
- `code` (Char, unique, optionnel)
- `company_id` (Many2one, required) — ou multi-company via règles
- `journal_id` (Many2one `account.journal`) — **journal cible** si comptabilisation
- `inbound_payment_method_line_id` (Many2one `account.payment.method.line`, optionnel) — si besoin
- `clearing_policy` (Selection, optionnel) :
  - `immediate` (peut être comptabilisé dès confirmation)
  - `needs_clearance` (ex: chèque/effet, nécessite état cleared)
- `active` (Boolean)

---

### 5.5 Configuration par entrepôt : `stock.warehouse` extension
Ajouter :
- `reserved_location_id` (Many2one `stock.location`)
- `reservation_internal_picking_type_id` (Many2one `stock.picking.type`) — type “Réservation Client”
- `reservation_release_picking_type_id` (Many2one, optionnel) — type “Libération Réservation”
- `reservation_sequence_id` (Many2one `ir.sequence`, optionnel)

---

## 6) Menus & Vues

### Menus
- Inventory / Sales (au choix) → **Réservations Clients**
  - Réservations (liste, kanban)
  - Encaissements opérationnels
  - Configuration
    - Modes de paiement (mapping journaux)
    - Paramètres par entrepôt

### Vues principales
- `stock.customer.reservation` :
  - Header boutons : `Réserver`, `Libérer`, `Clôturer`, `Annuler`
  - Smart buttons :
    - Transfert de réservation (picking_id)
    - Encaissements (count)
    - Encaissements comptabilisés (count)
  - Onglets :
    - Lignes produits
    - Encaissements
    - Notes & preuves
- `reservation.cash_in` :
  - Boutons : `Confirmer`, `Marquer à comptabiliser`, `Comptabiliser`, `Annuler`
  - Affichage lien `account_payment_id` si existant

---

## 7) Workflows (fonctionnel)

### 7.1 Créer une réservation (Draft)
1. Créer `Réservation`
2. Renseigner client, entrepôt, lignes produits
3. Définir `amount_expected` :
   - soit manuel
   - soit calculé sur base `price_unit_ref` (option)

### 7.2 Action “Réserver” (stock)
**Préconditions** :
- lignes non vides
- configuration entrepôt OK (`reserved_location_id` + picking type)
**Traitement** :
- Créer un `stock.picking` interne (type = “Réservation Client”)
- Moves : `WH/Stock` → `WH/Reserved`
- `origin` = réservation.name
- `partner_id` = client (si possible/utile)
- Valider le picking (réservation effective)
**Résultat** :
- réservation.state = `reserved`
- réservation.picking_id défini

### 7.3 Encaissement opérationnel (sans compta)
1. Créer un `Encaissement` lié à la réservation
2. État `draft` → `confirmed` (bouton Confirmer)
3. Mise à jour des computed sur la réservation (encaissé, restant)

### 7.4 Marquer “à comptabiliser”
- Action sur `cash_in` : `confirmed` → `to_account`
- Peut être fait en masse (liste)

### 7.5 Comptabiliser (optionnel)
**Préconditions** :
- `cash_in.state = to_account`
- `payment_mode.journal_id` défini
- Droits : groupe compta uniquement
**Traitement** :
- Créer `account.payment` :
  - type = inbound
  - partner_type = customer
  - partner_id = client
  - amount = cash_in.amount
  - date = cash_in.date (ou date compta paramétrable)
  - journal_id = payment_mode.journal_id
  - ref / memo = réservation.name + cash_in.reference
  - contrepartie : compte “Acomptes clients / Encaissements à affecter” (voir §8)
- Post du paiement (selon politique)
- Lier `cash_in.account_payment_id`
- `cash_in.state = accounted`

---

## 8) Comptes comptables & politique (recommandation)
**But** : permettre l’enregistrement comptable sans facture, sans impacter immédiatement la vente.

Recommandé :
- Un compte passif/attente : **“Acomptes clients”** / **“Customer Deposits”**
- Ou un compte de transit : **“Encaissements à affecter”**

Implémentation :
- Paramètre société : `reservation_deposit_account_id` (Many2one `account.account`)
- Utilisé lors de la génération du `account.payment` (si nécessaire via config payment modes / bridge)

> Note : le comportement exact de la contrepartie dépend de la configuration Accounting (outstanding accounts).
> Le module doit proposer une config explicite, et si manquante → bloquer la comptabilisation.

---

## 9) Sécurité & Droits
### Groupes
- `group_reservation_user` (Ops/Commercial)
  - CRUD sur réservations
  - CRUD sur encaissements (draft/confirmed)
  - Interdit : comptabiliser
- `group_reservation_accountant` (Compta)
  - Peut marquer à comptabiliser
  - Peut comptabiliser
  - Peut annuler comptabilisation selon règles (souvent interdit si payment posté)

### Règles d’accès
- Multi-company standard
- Les utilisateurs ne voient que leur société (record rules)
- Encaissements : lecture possible par ops, mais actions compta restreintes

---

## 10) Règles de validation & cas limites

### Stock
- Si produit suivi par lot/serial :
  - Wizard “Réserver” doit gérer la saisie des lots (ou laisser le picking en `waiting` pour saisie manuelle)
- Si stock insuffisant :
  - soit bloquer la réservation (strict)
  - soit autoriser partiel (config) et créer moves partiels

### Encaissements
- Montants négatifs interdits
- Si `amount_received_operational > amount_expected` :
  - autoriser (sur-encaissement) mais afficher alerte
  - ou bloquer (paramètre)

### Annulation
- Annuler une réservation `reserved` :
  - exiger libération stock (transfert inverse) avant `cancelled` (ou wizard qui libère automatiquement)
- Annuler un encaissement `accounted` :
  - par défaut **interdit** si paiement posté
  - sinon action “Rembourser” (option) créant un paiement inverse (V2)

---

## 11) Wizards (spécifications)

### 11.1 Wizard “Réserver”
- Vérifie config entrepôt
- Prépare picking interne + moves
- Option : “Valider automatiquement le picking” (bool) sinon laisser en brouillon

### 11.2 Wizard “Libérer”
- Permet de libérer tout ou partie
- Crée picking interne inverse `Reserved` → `Stock`
- Met à jour état réservation (`partially_released` si partiel)

### 11.3 Wizard “Comptabiliser encaissement”
- Confirmation (journal, date compta, compte)
- Post `account.payment`
- Écriture du lien sur `cash_in`

---

## 12) Rapports & KPIs (minimum)
Sur la réservation :
- KPI “Réservé (valeur/quantité)”
- KPI “Encaissé opérationnel”
- KPI “Reste à encaisser”
- KPI “Comptabilisé”
Exports :
- Liste des réservations avec colonnes : client, entrepôt, état, encaissé, restant, expiré

---

## 13) Scénarios d’acceptance tests (DoD)

### Scénario A — Réserver + encaisser sans compta
1. Créer réservation avec 2 lignes produits
2. Cliquer “Réserver” → picking interne créé et validé
3. Créer encaissement draft 1 000 → confirmer
4. Vérifier :
   - picking visible depuis smart button
   - `amount_received_operational = 1 000`
   - `amount_due = amount_expected - 1 000`
   - aucun `account.payment` créé

### Scénario B — Comptabiliser plus tard
1. Sur encaissement confirmé → marquer “à comptabiliser”
2. Utilisateur compta clique “Comptabiliser”
3. Vérifier :
   - `account.payment` créé dans le journal paramétré
   - `cash_in.state = accounted`, lien payment OK
   - `amount_accounted` mis à jour

### Scénario C — Stock insuffisant
- Si strict : action “Réserver” bloque avec message clair
- Si partiel : picking créé avec quantités disponibles seulement + état réservation partiel

### Scénario D — Annulation réservation
- Réservation en `reserved` → annuler : wizard propose libération totale → picking inverse validé → état `cancelled`

---

## 14) Paramètres & Données initiales

### Séquences
- `RES/YYYY/MM/####`
- `CIN/YYYY/MM/####`

### Données optionnelles
- Modes de paiement : Espèce, Virement, Chèque, Effet (sans imposer de journaux)

---

## 15) Non-scope (V1)
- Gestion TVA / facturation d’acomptes
- Conversion automatique en devis/BC standard (possible en V2)
- Rapprochement bancaire automatique
- Gestion avancée “clearing” (remise chèque/effet, rejet) — V2

---

## 16) Notes techniques
- Favoriser `mail.thread` pour suivi si utile
- Utiliser `company_dependent` sur certains paramètres si nécessaire
- Les actions “post” doivent être **idempotentes** et sécurisées :
  - pas de double `account.payment`
  - verrouillage sur `cash_in` après confirmation
- Logging minimal + messages utilisateurs clairs

---

## 17) Évolutions V2 (backlog recommandé)
1. Conversion réservation → devis/commande (création SO) + options de sourcing
2. Wizard “Appliquer acompte” (reclassement/lettrage selon règles comptables)
3. Politique clearing (chèque/effet) : deposited/cleared/rejected
4. Expiration automatique des réservations + relâche auto (cron)
5. Packages par réservation (au lieu de sous-emplacements client)

---

## 18) Deliverables attendus (développement)
- Modèles + contraintes
- Vues + menus
- Sécurité (groups + rules)
- Wizards (réserver/libérer/comptabiliser)
- Séquences
- Tests (au moins tests de logique : computed + transitions d’état)
- Documentation utilisateur courte (dans ce README)


# Spécifications — Module Odoo v19 Enterprise
## Réservation Stock par Client (depuis Devis) + Encaissements liés

**UPDATE V2**

---

# 1) Objectif & principes
Ce module introduit un **processus de réservation stock “avant confirmation”** sur les **devis (sale.order)**, avec :
- une **étape intermédiaire “Réservé”** sur le devis,
- la **création automatique d’un emplacement de réservation dédié par client** (sous un emplacement parent configuré),
- la **création d’un transfert interne** (Stock → Réservation/Client) lors de la réservation,
- un **modèle Réservation** lié **1–1** au devis, reflétant **l’intégralité des informations du devis** (y compris lignes, quantités, montants),
- la génération du **BL (delivery)** à la confirmation **avec source = emplacement de réservation du client**,
- une relation (liée au couple devis/réservation) vers un modèle **Encaissements**.

> Flux standard Odoo (devis → commande → BL → facture) reste inchangé pour les cas “normaux”, mais ici on ajoute une *pré-étape* au devis.

---

# 2) Configuration (obligatoire)
## 2.1 Emplacement parent de réservation
Le module doit permettre de configurer un **emplacement parent “Réservations Clients”**.
- Type : `stock.location` (usage = internal)
- Localisation : idéalement sous l’entrepôt (WH)
- Scope : par société, et/ou par entrepôt (selon votre design)

### Option recommandée
Configurer **par entrepôt** :
- `stock.warehouse.reservation_parent_location_id` (Many2one vers `stock.location`)

## 2.2 Sous-emplacement dédié par client
Sous cet emplacement parent, le module gère automatiquement un sous-emplacement **par client** :
- Nom : basé sur le client (avec unicité stable)
- Usage : internal
- Création : automatique au moment de la réservation si inexistant
- Stockage du lien : sur `res.partner` (champ company-dependent recommandé)

### Champ recommandé (partner)
- `res.partner.property_reservation_location_id` (Many2one `stock.location`, company_dependent)

> IMPORTANT : ne pas utiliser uniquement le nom comme clé d’unicité.  
> Utiliser un pattern stable : `RES-{partner.id}` ou `ClientName (#{partner.id})`.

---

# 3) Devis : ajout de l’étape “Réservé” avant confirmation
## 3.1 Extension du workflow
Le devis doit intégrer une étape **intermédiaire** :
- Draft/Sent → **Reserved** → Sale (confirmé)

### Implémentation recommandée
Étendre `sale.order.state` en ajoutant `reserved` **ou** ajouter un champ `reservation_state` et l’afficher en statusbar.

**Recommandation** : utiliser `sale.order.state` étendu pour un statusbar clair :
- `draft`, `sent`, `reserved`, `sale`, `done`, `cancel`

## 3.2 Action contextuelle “Réserver”
Sur un devis en `draft` ou `sent`, une action (bouton et/ou action contextual “Action” menu) :
- **Réserver**
déclenche la réservation.

### Comportement attendu de l’action “Réserver”
1) Vérifier présence de lignes produits stockables (type = storable/consumable selon règles).
2) Vérifier la configuration `reservation_parent_location_id`.
3) Trouver ou créer l’emplacement `Reservation/<Client>`.
4) Créer un **transfert interne** :
   - Source : emplacement stock principal de l’entrepôt (ex. `WH/Stock`)
   - Destination : `Reservation Parent / Client`
   - Lignes : produits + quantités du devis (uniquement produits stockables)
5) Valider le transfert (réservation effective par déplacement de stock).
6) Créer (ou mettre à jour) l’enregistrement **Réservation** lié au devis (voir §4).
7) Mettre le devis à l’état **Reserved**.
8) Ajouter des smart buttons :
   - Devis → Réservation
   - Réservation → Devis
   - Devis/Réservation → Encaissements
   - Réservation → Transfert interne (picking)

---

# 4) Modèle Réservation (1 devis = 1 réservation)
## 4.1 Modèle
**Nom technique (suggestion)** : `sale.customer.reservation`

## 4.2 Unicité (1–1)
- `sale_order_id` (Many2one `sale.order`, required, unique)
- `reservation_id` (Many2one sur `sale.order` pour accès rapide)
- Contrainte SQL côté réservation :
  - `UNIQUE(sale_order_id)`

## 4.3 Champs obligatoires (miroir du devis)
La réservation doit contenir **toutes les informations essentielles identiques au devis** :

**En-tête**
- `name` (séquence unique) — ex : `RSV/%(year)s/%(month)s/%(seq)s`
- `date` (Datetime, default now)
- `sale_order_id` (devis)
- `quotation_number` (related `sale_order_id.name`, stored)
- `partner_id` (related, stored)
- `company_id` (related, stored)
- `warehouse_id` (Many2one, stored si nécessaire)
- `internal_picking_id` (Many2one `stock.picking`) — transfert interne de réservation
- `internal_picking_name` (related picking.name, stored)
- `currency_id` (related du devis, stored)
- `amount_total` (related du devis, stored)
- `amount_received` (computed depuis encaissements)
- `amount_due` (computed) = `amount_total - amount_received`

**Lignes**
- `line_ids` (One2many) miroir des lignes du devis :
  - produit, description, qty, uom
  - price_unit
  - taxes
  - subtotal / total (si nécessaire)

> Les lignes de réservation doivent être **identiques en tous points** aux lignes du devis, y compris quantités et montants.

## 4.4 Synchronisation devis → réservation (obligatoire)
Toute modification du devis impactant :
- n° devis
- date
- client
- lignes (produits, quantités, prix)
- montants (total)
doit être **répercutée** sur la réservation.

### Règles de sync (V1 recommandé)
- Hook sur `sale.order.write` et `sale.order.line.write/create/unlink` :
  - appeler `sale_order._sync_reservation()`
- `sale_order._sync_reservation()` met à jour :
  - champs d’en-tête (montants/partenaire/date)
  - lignes (miroir exact)

### Impact stock lors des changements (conforme au besoin)
Lorsque le devis est déjà “Reserved” :
- si qty augmente → créer un **transfert interne d’ajustement** Stock → Reservation/Client (delta +)
- si qty diminue → créer un **transfert interne de retour** Reservation/Client → Stock (delta -)
- si produit remplacé → traiter comme (delta - ancien) + (delta + nouveau)

> Alternative (si vous préférez simplifier) : bloquer la modification des lignes une fois réservé et imposer un wizard “Modifier réservation” qui applique les changements et génère les transferts d’ajustement.  
> Mais le besoin mentionne explicitement la répercussion des modifications, donc prévoir au moins un mécanisme contrôlé.

---

# 5) Confirmation commande : BL depuis l’emplacement de réservation du client
## 5.1 Règle
Si un devis est en état `reserved` et est confirmé :
- Odoo génère un BL (delivery picking)
- **Source location** du BL doit être : `Reservation Parent / Client`
- Destination : `Customers` (emplacement client final standard)

## 5.2 Implémentation (technique)
À la génération des pickings depuis `sale.order` :
- sur `_prepare_picking()` / création du picking :
  - forcer `picking.location_id = partner_reservation_location_id`
- sur les stock moves si nécessaire :
  - forcer `move.location_id` à la même source

## 5.3 Contrôles avant confirmation
- Si le devis est `reserved` : vérifier que la réservation et son transfert interne existent et sont cohérents.
- Option stricte : refuser la confirmation si la réservation n’est pas “faite” (picking interne non validé) ou si stock réservé insuffisant.

---

# 6) Encaissements (liés au couple Devis/Réservation)
## 6.1 Modèle encaissements
Le module doit fournir (ou relier à) un modèle Encaissements.
**Nom technique (suggestion)** : `sale.reservation.cash_in`

**Relations**
- `reservation_id` (Many2one, required)
- `sale_order_id` (related de `reservation_id.sale_order_id`, stored)

**Champs attendus (minimum)**
- `name` (séquence unique) — ex : `CIN/%(year)s/%(month)s/%(seq)s`
- `date`
- `amount`, `currency_id`
- `payment_mode_id` (config, optionnel)
- `reference` + pièces jointes
- `state` : draft / confirmed / cancelled
- Optionnel : `account_payment_id` si vous voulez générer plus tard un paiement comptable

## 6.2 Unicité Devis ↔ Réservation
- Un devis a une seule réservation.
- Les encaissements se rattachent à cette réservation (donc indirectement au devis).

## 6.3 Montant dû
Sur devis **et** réservation :
- `amount_received` = somme des encaissements (state=confirmed)
- `amount_due` = `amount_total - amount_received`

---

# 7) UI / Navigation
## 7.1 Devis (sale.order) — ajouts
- Statusbar incluant l’étape **Reserved**
- Bouton/action :
  - `Réserver` (visible en draft/sent si pas déjà réservé)
  - Option : `Annuler réservation` / `Dé-réserver` (retour stock)
- Smart buttons :
  - Réservation (1)
  - Encaissements
  - Transfert interne (si utile)

## 7.2 Réservation
- Form view avec :
  - Infos devis (numéro, client, montants)
  - Lignes miroir
  - Smart buttons :
    - Devis
    - Transfert interne
    - Encaissements

## 7.3 Encaissements
- Liste + form filtrées par devis/réservation
- Boutons : confirmer / annuler
- Smart button retour devis/réservation

---

# 8) Séquences (obligatoires)
Créer des séquences uniques :
- Réservation : `RSV/%(year)s/%(month)s/%(seq)s`
- Encaissement : `CIN/%(year)s/%(month)s/%(seq)s`

---

# 9) Sécurité (minimum)
- Groupe “Utilisateur” (lecture/écriture sur devis réservés + réservations + encaissements) **additif** aux groupes Sales/Stock pertinents.
- Groupe “Manager” (configuration emplacements, actions avancées).
- ACL explicites pour modèles custom.
- Record rules multi-société si `company_id`.

---

# 10) Cas d’usage (Acceptance tests)
## A — Réservation depuis devis
1) Créer devis (draft) avec lignes stockables.
2) Cliquer **Réserver**.
3) Attendu :
   - emplacement `Reservation Parent / Client` existe (créé si besoin)
   - transfert interne créé et validé
   - réservation créée (1–1) et liée au devis
   - devis passe à l’état **Reserved**

## B — Modification devis après réservation (delta stock)
1) Sur devis réservé, augmenter qty d’un produit.
2) Attendu :
   - lignes réservation mises à jour
   - transfert interne d’ajustement créé Stock → Reservation/Client (delta)
   - montants identiques devis/réservation

## C — Confirmation commande (BL depuis réserve client)
1) Confirmer un devis réservé.
2) Attendu :
   - BL généré avec source = `Reservation/Client`
   - destination = Customers
   - flux standard continue

## D — Encaissement lié
1) Sur devis réservé, créer encaissement, confirmer.
2) Attendu :
   - amount_received et amount_due corrects sur devis et réservation

---

# 11) Hors scope (si vous voulez rester “court” en V1)
- Génération automatique d’account.payment (si vous voulez rester 100% opérationnel)
- TVA / factures d’acompte
- Gestion avancée de clearing (chèque/effet : remis/encaissé/rejeté)

---

# 12) Notes techniques (guidelines de dev)
- Éviter `sudo()` dans les actions métier.
- Garantir l’idempotence :
  - Un devis ne peut pas réserver deux fois (réutiliser la réservation existante si relancée).
- Utiliser des méthodes explicites :
  - `sale.order.action_reserve_stock()`
  - `sale.order._sync_reservation()`
  - `reservation._create_or_get_partner_location()`
  - `sale.order._prepare_picking()` override pour forcer source location sur BL

---
Fin des spécifications
