[English version](manual.md)

# Fortnite Comp Tracker

Suivi et prédiction des seuils de points des compétitions Fortnite, avec récupération
automatique des classements.

Tu lances l'app, tu vois les tournois en cours, tu cliques sur celui que tu veux suivre. L'app lit
le classement toute seule toutes les 10 minutes et te dit où vont finir les seuils. L'interface est
bilingue et démarre en anglais ; le sélecteur **EN/FR** est en haut à droite de chaque page.

---

## Installation

Il faut Python 3.10 ou plus.

```bash
pip install -r requirements.txt
python src/app.py
```

Le navigateur s'ouvre sur `http://127.0.0.1:5000`. Sous Windows : double-clic sur
**`tracker.bat`**.

Au premier lancement, l'app demande ta **clé API Cito** — compte gratuit sur citoapi.com,
500 requêtes par mois. Elle est enregistrée dans `cito_key.txt`, à garder pour toi.

Une base créée par une version précédente est mise à jour automatiquement, sans rien perdre.

---

## Comment ça marche

### L'accueil

Deux sections : les **tournois en direct**, récupérés depuis l'API, et les **tournois suivis**,
regroupés par catégorie et par région.

Un bouton *Démarrer les prédictions* sur un tournoi en direct suffit : l'app crée la compétition
avec ses horaires, sa région et son format, lit le classement complet, et ouvre la vue de suivi.
Une requête.

### La vue de suivi, en trois volets

**À gauche, le format.** Horaires, fin d'acquisition, nombre de parties, format scellé ou libre,
parties déjà jouées, et le **barème déduit automatiquement** des parties du classement. C'est aussi
là que tu règles tes **objectifs** — voir plus bas.

**Au centre, le classement.** Toutes les équipes, avec leur score, leurs éliminations et leurs
parties jouées. Les lignes correspondant à tes objectifs sont surlignées et restent visibles même
si elles sont très bas dans le tableau. Une recherche permet de retrouver une équipe.

**Clique sur une équipe** pour dérouler son historique complet : chaque partie avec son heure, son
placement, ses éliminations et ses points. Ce détail arrive dans la même réponse que le classement,
il ne coûte donc **aucune requête supplémentaire** — sur un tournoi de 1271 équipes, ça représente
plus de 5 000 parties enregistrées d'un coup.

**À droite, les prédictions.** Le seuil final estimé pour chaque rang suivi, tes objectifs en
premier, avec la fourchette et le détail des modèles. Plus la courbe d'évolution avec le
prolongement prédit.

Le classement se rafraîchit **toutes les 10 minutes** automatiquement, avec un compte à rebours
visible. Un bouton permet de forcer un relevé immédiatement après une partie.

### L'estimation de départ

Au moment où tu démarres un suivi, l'app calcule une estimation des seuils **avant d'avoir lu le
moindre relevé** : uniquement à partir du barème, du nombre de parties, et de l'historique des
tournois de la même catégorie. Cette estimation est **figée** et reste affichée en haut du volet
des prédictions pendant tout le tournoi, puis se compare au résultat réel à la fin.

C'est la mesure qui compte pour prédire une compétition qui n'a jamais eu lieu. La page
*Historique* trace son erreur tournoi après tournoi : plus l'historique d'une catégorie grandit,
plus cette courbe descend.

Quand aucun tournoi de la catégorie exacte n'existe encore, l'app le dit et élargit franchement la
fourchette plutôt que de faire semblant.

### Entraîner l'estimation sans attendre

Onglet **Entraîner**. L'estimation à froid a besoin d'éditions passées de la même catégorie ;
plutôt que d'attendre des semaines, relève les seuils finaux sur Fortnite Tracker et saisis-les :
une ligne par édition, une colonne par rang.

Tu donnes la catégorie, la région, le nombre de parties et le barème — repris automatiquement si
la catégorie est déjà connue — puis les seuils. À l'enregistrement, l'app affiche immédiatement ce
qu'elle prédira au prochain tournoi de cette catégorie, avant et après ta saisie, avec le
resserrement de la fourchette.

Trois ou quatre éditions suffisent à sortir du régime « aucune référence ». Aucune requête API
n'est consommée : c'est de la saisie pure.

Ces entrées n'apparaissent pas dans la liste des tournois suivis — seulement dans l'historique,
où elles servent de référence.

### Comparer ce qui est comparable

Une **Division 1** et une **Division 5** portent presque le même nom mais n'ont ni le même niveau
ni parfois le même barème ; une cup **Reload Zero Build** n'a rien à voir avec une Battle Royale.
L'app cherche donc les tournois comparables dans cet ordre :

1. même catégorie exacte, même région ;
2. même catégorie, toutes régions ;
3. même série et même manche, si le tournoi vient d'une série manuelle ;
4. en dernier recours, même région et même mode — signalé comme approximatif.

La catégorie est le nom du tournoi débarrassé du numéro de semaine et de la région, ce qui
regroupe les éditions successives du même tournoi sans mélanger les divisions.

### Les objectifs

Le barème est le même partout, mais **le rang qui qualifie ou qui donne le skin change selon la
région**. Tu définis donc, pour chaque catégorie de tournoi et chaque région, les rangs qui
comptent : « Qualification top 500 », « Skin top 1000 ». L'app s'en souvient et les reprend
automatiquement la fois suivante.

Ces rangs deviennent les cibles principales des prédictions ; l'app enregistre en plus des rangs
de repère (1, 5, 10, 20, 50, 100, 250, 500, 1000) pour alimenter le calibrage.

### Le quota

En haut à droite : les requêtes consommées ce mois-ci et le nombre de tournois encore suivables.
Un rafraîchissement toutes les 10 minutes sur une session de 3 h fait 18 requêtes, soit environ
**27 tournois par mois** dans l'offre gratuite.

La liste des tournois en direct est mise en cache quelques minutes : recharger la page ne coûte
rien.

### Le ménage

Au lancement, les tournois **terminés sur lesquels tu n'as jamais lancé de prédiction** sont
effacés. Ceux que tu as suivis sont conservés et servent de référence pour les estimations
futures. Les compétitions saisies à la main ne sont jamais touchées.

### Ce qui est déduit sans rien saisir

| Information | D'où elle vient |
|---|---|
| **Estimation de départ** | Le rapport barème/seuil appris sur les éditions passées de la même catégorie, appliqué au barème et au nombre de parties de ce tournoi. |
| **Barème** | Chaque partie donne placement, élims et points marqués : deux équations suffisent à retrouver la table complète. Vérifié à 100 % sur données de test. |
| **Format scellé ou libre** | Si toutes les équipes démarrent la même partie à la même minute, c'est scellé ; l'intervalle entre parties en découle. |
| **Nombre de parties** | Le maximum observé, complété par les éditions précédentes du même tournoi pour ne pas le sous-estimer en début de session. |
| **Mode d'équipe** | La taille des équipes du classement : Solo, Duo, Trio ou Squad. |
| **Parties jouées** | La médiane du haut du classement, plus fiable que l'estimation par l'heure. |

### La saisie manuelle

Toujours disponible dans l'onglet *Saisie manuelle*, avec les séries, les barèmes enregistrés et
les relevés à la main. Utile si l'API ne répond pas en plein tournoi, ou pour un tournoi absent de
la liste.

---

## Le déroulé d'un tournoi

### 1. Créer la compétition

Nom, région, mode (Solo / Duo / Trio / Squad), type de partie, heure de début et de **fin officielle**.
Trois réglages de plus, pré-remplis selon le mode :

| Réglage | À quoi ça sert |
|---|---|
| **Durée d'une partie** | 30 min en Battle Royale et Zero Build, 18 en Reload, 12 en Blitz. |
| **Délai du tracker** | Le temps que met Fortnite Tracker à publier une partie terminée (5 min par défaut). |
| **Nombre de parties** | Combien de parties comptent dans la session (10 pour une cash cup, 6 pour une finale). |
| **Comptage des parties** | *Maximum* ou *scellé* — voir juste en dessous. |

#### Maximum ou scellé ?

Deux formats, qui ne se prédisent pas de la même façon :

**Maximum** — chacun joue quand il veut, jusqu'à N parties. C'est le cas des opens et des cash cups.
L'app ne peut qu'estimer le nombre de parties déjà jouées à partir de l'heure, en supposant qu'elles
s'enchaînent régulièrement. Tu peux corriger cette estimation en saisissant le nombre exact de parties
dans chaque relevé.

**Scellé** — toutes les parties démarrent à heure fixe et tout le monde en joue le même nombre. C'est
le format des finales. Tu donnes le nombre de parties et l'intervalle entre deux lancements, et l'app
en déduit le calendrier complet :

```
Partie 1 : lancée 19:00 · classement à jour 19:35
Partie 2 : lancée 19:35 · classement à jour 20:10
…
Partie 6 : lancée 21:55 · classement à jour 22:30
```

Là, plus rien n'est estimé : l'app sait exactement combien de parties sont déjà dans le classement à
chaque instant, et la fin d'acquisition est celle de la dernière partie du calendrier — pas l'heure de
fermeture affichée. Le bandeau du haut affiche un compte à rebours jusqu'à la prochaine partie, et le
calendrier indique quelle ligne est déjà comptabilisée : c'est le bon moment pour faire un relevé.

### 2. Saisir des relevés

Le bouton *Maintenant* remplit l'heure. Tu tapes les seuils lus sur Fortnite Tracker, et si tu veux :

- **Parties jouées** — le compteur s'incrémente tout seul après chaque ajout. C'est ce qui permet la
  prédiction dès la première partie (voir plus bas).
- **Mes points** — tes points à toi, pour la carte *Ma course*.

Double-clic sur n'importe quelle valeur du tableau pour la corriger.

### 3. Lire les prédictions

Elles se recalculent à chaque ajout : seuil final estimé par top avec sa fourchette, ce qu'il te reste
à faire pour chaque objectif, et la tendance des prochaines compétitions du même type.

### 4. Terminer le tournoi

Le bouton **Terminer le tournoi** fait trois choses d'un coup :

1. il marque le tournoi comme terminé ;
2. il reprend le dernier relevé comme résultats définitifs (modifiables juste après) ;
3. il enregistre le tournoi dans son propre fichier, dans le dossier `data/tournaments/`.

Le nom du fichier est construit pour que le dossier reste trié et lisible :

```
2026-08-28_1900_EU_Solo_battle-royale_solo-cash-cup-2.json
   date     heure  région  mode   type de partie      nom du tournoi
```

*Rouvrir le tournoi* annule la clôture. *Enregistrer dans tournaments/* réécrit le fichier à la demande
(par exemple après avoir corrigé un seuil). *Télécharger ce tournoi* donne le même fichier dans le
navigateur, et il se réimporte depuis la page d'accueil.

### 5. Enchaîner

**Dupliquer pour la prochaine fois** recrée la compétition avec les mêmes réglages (région, mode,
durées, rangs, barème) à la date que tu indiques, sans les relevés. Le numéro dans le nom est
incrémenté quand il y en a un : « Cash Cup #3 » devient « Cash Cup #4 ».

---

## Séries, manches et barèmes

### Barèmes enregistrés

Onglet **Barèmes** : tu crées un système de points sous le nom que tu veux (« FNCS Duo 2026 »,
« Cash Cup R2 »…), et tu le choisis ensuite d'un menu déroulant à la création d'un tournoi ou d'une
série. Une ligne par palier (`1 = 60`, `11-15 = 20`) plus les points par élimination.

Modifier un barème n'affecte pas les tournois déjà créés : chacun garde la copie qu'il avait au
départ, pour que l'historique reste exact.

### Séries

Onglet **Séries** : une série, c'est un tournoi qui revient — une FNCS avec son open et sa finale.
Tu définis ses manches **une fois** :

| Manche | Jour | Heure | Durée | Rangs suivis |
|---|---|---|---|---|
| Open | J+0 | 19:00 | 3 h | 100, 500, 1000, 10000 |
| Final | J+1 | 19:00 | 3 h | 1, 3, 5, 10, 20 |

Ensuite, chaque semaine, tu donnes une date et le bouton **Créer les manches** génère l'édition
complète : les deux tournois, aux bonnes heures, avec les bons rangs et le bon barème.

**Fais une série par région.** Les seuils EU et NAC n'ont rien à voir ; en les séparant, l'historique
de chacune reste propre et les prédictions ne se contaminent pas.

---

## L'algorithme apprend de ton historique

À chaque fois que tu termines un tournoi, l'app mesure trois choses sur les compétitions comparables
et les réinjecte dans les prédictions suivantes. C'est visible dans la carte **Ce que l'app a appris**.

**1. L'exposant du modèle « par partie ».** De combien le seuil monte quand le nombre de parties
double. Vaut 0,93 au départ, puis se mesure sur tes propres courbes, rang par rang.

**2. La fiabilité de chaque modèle.** L'app rejoue chaque compétition terminée à 25 %, 50 % et 75 %
de la session, compare ce que chaque modèle annonçait au vrai résultat, et en déduit qui mérite
d'être écouté. Ces poids remplacent les valeurs par défaut dès la première compétition terminée.

**3. Le rapport entre le barème et les seuils atteints.** L'app calcule la valeur d'une « partie de
référence » dans ton barème (un top 10 plus deux élims), la multiplie par le nombre de parties de la
session, et mesure quel multiple de ce total il faut pour finir dans chaque top. Ce rapport est
**indépendant du barème** : si tes points de placement augmentent de 50 %, les seuils prévus
augmentent de 50 % aussi.

C'est ce troisième point qui permet la **prédiction avant le premier relevé** : dès la création d'un
tournoi, tu as une estimation de chaque seuil, calculée avec le barème et le nombre de parties. Ces
deux réglages la pilotent directement — un barème multiplié par 1,5 donne des seuils multipliés par
1,5, et passer de 10 à 6 parties les ramène à 60 %. En format scellé, le nombre de parties étant une
certitude, la fourchette est un peu plus serrée. Sur des
Mesuré, pas supposé : en retirant un tournoi à la fois de l'historique et en le prédisant à froid,
l'erreur médiane est de 6,4 % et 86 % des seuils réels tombent dans la fourchette annoncée. C'est
plus précis en profondeur de classement (2,2 % au-delà du rang 500) que dans le top 5 (8,7 %), où
une équipe en forme suffit à déplacer le seuil. Le détail est dans `docs/methodology.md`.

### Vérifier que ça s'améliore

```bash
python src/backtest.py --learning-curve
python src/backtest.py --learning-curve --stage Final
```

Rejoue ton historique dans l'ordre chronologique et affiche l'erreur de prédiction selon le nombre de
compétitions déjà apprises, avec 0, 1, 3 et 5 relevés. C'est la mesure honnête de ce que
l'apprentissage t'apporte sur *tes* données.

### Les limites

Le rapport barème/seuil suppose que la difficulté relative reste stable. Si Epic change la *forme* du
barème (beaucoup plus de points pour la victoire, par exemple), la transposition reste approximative
tant que tu n'as pas terminé un tournoi avec le nouveau barème. Et sur un format que tu n'as jamais
joué, l'app n'a rien à apprendre : elle retombe sur les réglages par défaut, en le disant.

---

## Les points tombent après l'heure de fin

Une partie lancée juste avant la fermeture du tournoi compte quand même. Si la session ferme à 22 h,
une partie lancée à 21 h 59 peut durer jusqu'à 22 h 29, et Fortnite Tracker met encore ~5 min à
l'afficher : **les seuils bougent en réalité jusqu'à 22 h 35**.

L'app appelle ça la *fin d'acquisition* :

```
fin d'acquisition = fin officielle + durée d'une partie + délai du tracker
```

C'est cette heure-là que visent toutes les prédictions, et c'est jusque-là que va le graphique — le
trait vertical marque la fin officielle. Le bandeau en haut de la page affiche les deux comptes à
rebours en direct.

Sans ça, les prédictions sous-estiment systématiquement les seuils finaux : la dernière partie de tout
le monde manque à l'appel.

---

## Comment marchent les prédictions

### Pendant la compétition

Cinq modèles tournent en parallèle sur la série de relevés de chaque rang (six avec l'estimation
issue du seul barème, utilisée avant le premier relevé) :

| Modèle | Idée |
|---|---|
| **Par partie jouée** | `seuil × (parties totales / parties jouées)^b`. Marche **dès le premier relevé**. L'exposant `b` est inférieur à 1 (un joueur n'enchaîne pas dix top 1) ; il vaut 0,93 par défaut et se recale sur tes données dès qu'il y a 3 relevés. |
| **Forme historique** | Sur les compétitions passées du même type, à 50 % de la session on avait en moyenne X % du total final. On applique ce ratio aux points actuels. Marche aussi dès le premier relevé. |
| **Courbe puissance** | `points = a × progression^b` sur le temps : capte une montée qui accélère ou s'essouffle. |
| **Rythme moyen** | Cadence constante depuis le début. |
| **Rythme récent** | Idem sur les 4 derniers relevés : réagit si la cadence change. |

Chaque modèle est **backtesté sur tes propres relevés** (on lui donne les k premiers, on lui demande le
k+1ᵉ, on mesure l'erreur). Le résultat affiché est la moyenne pondérée par l'inverse du carré de cette
erreur, et un modèle nettement moins bon que le meilleur est purement **écarté** — c'est visible dans
« détail des modèles ». Tant qu'il y a moins de trois relevés, aucun backtest n'est possible : les
modèles sont alors pondérés par une confiance a priori qui privilégie *Par partie* et *Forme
historique*.

La fourchette basse-haute vient de la dispersion entre modèles retenus et de leur erreur de backtest,
élargie selon le temps restant, avec un plancher plus large quand il n'y a qu'un ou deux relevés.

### Ma course

Si tu saisis tes propres points, l'app affiche ton rang estimé (interpolé entre les seuils connus), ton
rythme par partie, et pour chaque top : le seuil visé, l'écart, les points par partie à tenir sur les
parties restantes, et **ce que ça représente concrètement** — « top 15 avec 2 élims ou top 10 sans
élim ». Cette traduction utilise le barème du tournoi.

### Saisir un barème

Le champ « points de placement » accepte une table **collée telle quelle**, depuis osirion.gg,
une page de règles ou un tableur :

```
1st	65 (+9)	32          1 = 65              Top 1 : 65
2nd	56 (+4)	28          2-3 = 55            Top 2-3 : 55
```

Les ordinaux (`1st`, `21st`, `1er`, `2e`), le préfixe `#`, et les écarts entre parenthèses sont
gérés. Quand la table contient **plusieurs colonnes de points**, l'app le détecte et te laisse
choisir laquelle utiliser, avec un aperçu des premières valeurs — puis remet le champ au propre.
Une ligne « Eliminations : 2 » dans le bloc collé remplit aussi les points par élimination.

### Le barème

Choisi dans un menu déroulant (voir *Barèmes* plus haut) ou modifié directement dans *Réglages du
tournoi*. Trois barèmes sont fournis en point de départ — **vérifie-les dans les règles officielles
du tournoi, ils changent d'une saison à l'autre**.

### Entre compétitions

Les seuils finaux des compétitions comparables terminées (résultats définitifs en priorité, dernier
relevé à défaut) sont combinés en trois lectures : moyenne des 5 dernières, médiane, et tendance
linéaire. La page *Historique & tendances* les filtre par région / mode / type de partie.

Une compétition compte comme terminée si elle a été clôturée par le bouton, si ses résultats
définitifs sont saisis, ou à défaut si son dernier relevé couvre 90 % de la session.

### Vérifier la fiabilité sur tes propres données

```bash
python src/backtest.py
python src/backtest.py --region EU --team-mode Solo
```

Rejoue chaque compétition terminée comme si elle s'arrêtait à 15 %, 25 %, 50 %, 75 % et 90 %, et
compare la prédiction au vrai seuil final. Affiche l'erreur moyenne par top et le taux de couverture de
la fourchette. C'est le meilleur moyen de savoir à partir de quand tu peux faire confiance aux chiffres.

---

## Données

Tout est en local, à côté du script :

```
data/tracker.db        la base complète
data/tournaments/      un fichier JSON par tournoi terminé
```

- **Export CSV** : une ligne par relevé et par rang, colonnes `type` (`reading` / `final_result`),
  `games`, `my_points` (noms de colonnes fixes, non traduits).
- **Export JSON** : sauvegarde complète, réimportable depuis la page d'accueil. Un fichier de tournoi
  seul se réimporte aussi.
- Variable d'environnement `FNT_DB` pour changer l'emplacement de la base.

---

## Récupération automatique des seuils

Il n'existe pas d'API publique gratuite et sans clé pour les classements de tournois :

- L'API officielle d'Epic (`events-public-service-live.ol.epicgames.com/api/v1/events/Fortnite/leaderboards/...`)
  exige un jeton OAuth lié à un compte Epic et n'est pas documentée publiquement.
- Fortnite Tracker n'expose pas les seuils de tournoi dans son API publique.
- Des services tiers proposent un endpoint de classement avec une clé API et un palier gratuit limité
  (par ex. `api-fortnite.com` : `/api/v2/events/:eventId/windows/:eventWindowId/leaderboard`, ou
  `citoapi.com`), de l'ordre de quelques centaines de requêtes par mois.

### Le branchement lui-même

Pour brancher l'une de ces API plus tard, tout passe par une fonction :

```python
import db

with db.session() as conn:
    db.add_snapshot(conn, comp_id=3, ts="2026-08-28 20:45",
                    points={1: 158, 20: 129, 50: 119, 100: 112, 500: 91, 1000: 75},
                    games=6, my_points=104, note="import auto")
```

Et pour les résultats définitifs : `db.set_finals(conn, comp_id, {1: 268, 20: 214, ...})`.
Un script lancé toutes les 10 minutes pendant la compétition suffit ; le reste de l'app ne change pas.

---

## Fichiers

```
update.bat, site.bat, harvest.bat, tracker.bat   les lanceurs, à la racine
src/app.py           serveur Flask + API JSON
src/cito.py          accès à l'API Cito (tournois en direct, classements)
src/tracking.py      import d'un tournoi et rafraîchissement du suivi
src/scoring_infer.py déduction du barème à partir des parties
src/db.py            modèle de données SQLite
src/predict.py       modèles de prédiction et suivi personnel
src/calibration.py   ce que l'app apprend de l'historique
src/backtest.py      fiabilité et courbe d'apprentissage sur tes tournois
src/cleanup.py       ménage dans le dossier (double-clic : src/cleanup.bat)
src/templates/       pages HTML
src/static/          CSS + moteur de graphiques SVG maison
data/            ta base et tes tournois archivés
```

Seule dépendance : Flask. Les graphiques sont dessinés en SVG sans bibliothèque externe, donc l'app
marche sans connexion internet.
