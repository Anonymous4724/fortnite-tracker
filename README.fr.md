# Fortnite Comp Tracker

*[English version](README.md)*

Prédit les seuils de points des compétitions Fortnite — combien de points il faudra pour
finir à un rang donné — avant le début du tournoi et pendant qu'il se joue.

Les seuils ne sont publiés qu'après coup. Un joueur qui se demande s'il continue à jouer, ou
avec quelle agressivité, avance à l'aveugle. L'app estime la réponse à partir du barème du
tournoi, du nombre d'équipes, et de ce qu'ont donné les éditions précédentes de tournois
comparables.

Quatre lanceurs à la racine font tout le quotidien ; le reste est dans `src/` :

| | |
|---|---|
| `update.bat` | tout — une passe de moisson pour les nouveaux tournois, le modèle rebâti et vérifié, le calendrier de la semaine, la page, le push ; dix minutes |
| `site.bat` | la page et le calendrier de la semaine seulement ; quelques secondes |
| `harvest.bat` | le long téléchargement des tournois passés, une nuit |
| `tracker.bat` | l'app locale de suivi |

**Mesuré comme une prévision — les 600 tournois les plus récents prédits à partir des 6 632
qui les précèdent, sans que rien ne voie le futur : 4,2 % d'erreur médiane quand la cup a déjà
eu lieu, 85 % des seuils réels dans la fourchette annoncée.** Quand la cup n'a jamais eu lieu,
la prévision repose sur le seul barème et l'erreur est de 20 % ; la moitié des tournois d'une
nouvelle saison commencent ainsi. Le jeu d'entraînement compte 7 232 tournois et 79 891 seuils
lus sur l'API publique d'Osirion. [`docs/methodology.md`](docs/methodology.md) explique
comment c'est mesuré et où le modèle cesse d'être crédible.

![Courbe ajustée sur les seuils observés](analysis/figures/curve.png)

---

## Le modèle

Une cascade, la lecture la plus directe d'abord. Chaque échelon ne répond que si celui du
dessus ne le peut pas.

1. **L'édition précédente, lue telle quelle.** Même cup, même région, même rang, la dernière
   fois. La fourchette est le 80e centile de ce que ce rang a bougé entre deux éditions
   consécutives de cette cup — mesuré, pas supposé. C'est le report de la dernière édition
   promu au rang de modèle, et il est premier parce qu'il n'a pas cessé de gagner : une soirée
   forte monte le rang 1 et le rang 20 ensemble, et toute prévision qui multiplie le niveau
   d'une édition par un ratio pris sur d'autres jette cette corrélation. Deux corrections
   depuis : l'édition lue est la dernière avec la même condition d'accès que la cup à venir
   (`competition.entry`, lu dans l'exigence `currentRanking` d'Epic — une cup réservée aux
   Unreal est un autre effectif qu'une cup ouverte dès Diamant), la fourchette élargie de
   moitié quand aucune n'existe ; et un effectif connu qui n'est pas celui de l'édition
   déplace la valeur le long du terme de quantile de la courbe, plafonné à un cinquième, ce
   qui sur les 600 tournois les plus récents ramène l'erreur de ce barreau sur ces lignes de
   6,3 % à 4,7 % — le terme du rang de référence doit en rester exclu : pris en rapport, il
   dit qu'un effectif plus petit monte les premiers rangs, et les données refusent.
2. **Le niveau fois la forme mesurée.** Le seuil de la cup au rang 20 lors de l'édition
   précédente, fois ce que ce rang valait par rapport au rang 20 sur les éditions de la cup —
   une table, pas une courbe, parce que la table a divisé l'erreur par deux là où elle
   s'applique. La catégorie d'abord, puis la famille toutes régions confondues.
3. **Le niveau fois la courbe**, pour les rangs qu'aucune édition n'a mesurés :

       seuil(rang) = niveau · exp(−a · (q^b − q_ref^b)),    q = rang / effectif

   `a` et `b` sont ajustés sur tous les tournois terminés. La courbe est juste à quelques
   pour cent près autour du rang 20 et dérive en s'en éloignant — 12 % trop basse au rang 1,
   5 % au fond — ce qui est exactement pourquoi elle est troisième.
4. **Le lobby fermé**, pour une finale jouée dans un seul lobby dont aucune édition n'a été
   vue — le cas habituel d'un Round 2 dont le modèle connaît le Round 1. Chaque seuil de
   chaque finale à lobby unique du jeu d'entraînement est divisé par le maximum qu'une équipe
   pouvait marquer sur les parties jouées et rangé par la part du lobby que représente le
   rang, par mode de jeu et taille d'équipe ; l'estimation se lit dans cette table,
   log-linéairement entre deux cases, avec la dispersion entre finales pour fourchette. Cet
   échelon existe parce que les deux du dessous sont mesurés sur des files ouvertes de
   milliers d'équipes : dans un lobby de vingt, le rang 20 est la dernière équipe, et la
   courbe faisait valoir au vainqueur dix fois l'ancre.
5. **Le seul barème**, pour une cup que personne n'a vue. Le seuil au rang 20 est une part du
   maximum qu'une équipe peut marquer, la part étant lue sur les cups du même genre, de la
   même plateforme et du même stade — une finale FNCS sur PC n'est pas une cup mobile de
   créateur — et rapprochée d'un a priori avec un poids `n / (n + 2)`. C'est le maillon
   faible et les résultats ci-dessous en donnent le prix.

Deux choses que les données ont tranchées en chemin :

- **Le nombre d'équipes compte peu** pour le niveau : doubler l'effectif déplace un seuil
  d'environ 3,5 %. Mais il compte beaucoup au fond de l'échelle, et c'est pourquoi la table de
  forme s'arrête au rang 500 et laisse la place à la courbe, qui connaît le terrain.
- **Le niveau a une édition d'âge, pas une médiane.** Lu sur l'édition précédente, il prédit à
  5,4 % ; la médiane des trois dernières, à 5,7 % ; des huit dernières, à 6,4 %. Les cups
  dérivent d'une semaine à l'autre, et un historique plus vieux que la dernière édition
  renseigne sur le mois dernier.

---

## Résultats

Une coupe chronologique, pas aléatoire : les 600 tournois les plus récents — tout ce qui
commence au 25 juillet 2026 — prédits à partir des 6 632 d'avant. Rien ne voit le futur, ni
le modèle ni les références. Prédit à froid, avant tout relevé du classement en direct.

**Quand la cup a déjà eu lieu** (2 286 seuils sur 307 tournois) :

| tranche de rangs | ce modèle | édition précédente | médiane de catégorie | n |
|---|---:|---:|---:|---:|
| 1 – 5 | 5,3 % | 5,4 % | 5,5 % | 876 |
| 6 – 25 | 3,6 % | 4,4 % | 4,8 % | 726 |
| 26 – 100 | 2,5 % | 3,6 % | 5,0 % | 261 |
| 101 – 500 | 4,1 % | 5,2 % | 7,3 % | 347 |
| au-delà de 500 | 7,3 % | 8,7 % | 9,8 % | 76 |
| **tout** | **4,2 %** | **5,0 %** | **5,6 %** | **2 286** |

Le modèle part de l'édition précédente — c'est l'échelon 1 — et tant qu'il ne corrigeait pas
cette édition pour l'effectif, il *était* l'édition précédente partout où une existait à ce
rang, 5,0 % contre 5,0 %, une égalité par construction. C'est l'effectif qui sépare les deux
maintenant : un nombre d'équipes connu qui n'est pas celui de l'édition déplace la valeur le
long du terme de quantile de la courbe, et le modèle bat le report sur lequel il est bâti de
0,8 point, tout l'intervalle au-dessus de zéro (+0,47 à +1,34), et la médiane de catégorie de
1,45. Ce que le report ne sait toujours pas faire, il le fait aussi — une fourchette, une
réponse pour les rangs que la semaine dernière n'a pas publiés, et une réponse pour les cups
qui n'ont pas de semaine dernière.

Ces chiffres ont bougé quand le nommage a été corrigé (les deux lignes du titre, la manche
depuis l'identifiant) : l'erreur de l'édition précédente est passée de 5,7 % à 5,0 %, sa
moyenne de 46 % à 12 %, parce que « l'édition précédente » est désormais la bonne cup et non
celle qui partageait un premier mot. La médiane de catégorie est passée de 8,4 % à 5,6 % pour
la même raison, ce qui explique que l'avance du modèle sur elle soit tombée de 2,7 points à
0,6.

**Quand la cup n'a jamais eu lieu** (2 897 seuils sur 283 tournois — la moitié de
l'échantillon, parce qu'une nouvelle saison apporte de nouveaux formats) : 20 % d'erreur
médiane. Solo Reload, cups mobile, cups de créateurs, prédites depuis leur seul barème. C'est
l'échelon à améliorer en priorité.

**Les finales à lobby unique jamais vues** (103 seuils sur 23 tournois) passaient par ce
même échelon du barème et sa courbe de file ouverte : 124 % d'erreur médiane, le vainqueur
d'une finale Reload à vingt équipes chiffré à 2 900 points quand 300 était le maximum
possible. Lues sur les finales du même format par part du lobby, elles sortent à 13 % — 6 %
une fois écartée une cup dont le classement enregistre 100 points pour chaque finaliste —
avec 84 % d'entre elles dans la fourchette.

**La fourchette est honnête.** 83 % des seuils tombent dans une fourchette qui en annonce
80 % ; au niveau nominal de 89 %, la couverture réelle est de 90 %.

**La première édition comparable vaut 8,6 points** d'erreur médiane (15,1 % sans aucune,
6,5 % avec une) ; les cinq suivantes en valent 1,6 à elles cinq.

Mesuré sur une coupe aléatoire à la place — chaque tournoi retiré à son tour avec le reste
en historique — le même modèle donne 7,0 % contre 4,2 % pour l'édition la plus proche. Ce
chiffre est plus agréable et faux à publier : l'édition la plus proche est souvent celle de
la semaine *suivante*, et son résultat n'est pas disponible le soir même.

---

## Lancer l'app

Python 3.10 ou plus. Aucune dépendance en dehors de Flask.

```bash
pip install -r requirements.txt
python src/app.py             # ouvre http://127.0.0.1:5000
```

Windows : double-clic sur `tracker.bat`.

Le jeu d'entraînement vient de l'API publique gratuite d'[Osirion](https://osirion.gg) — le
calendrier des tournois, le barème de chaque épreuve, et les classements.
`harvest_osirion.py` le télécharge, de façon reprenable, et `data/` est ignoré par git : les
conditions de l'API n'autorisent pas à republier sa sortie, donc la base est quelque chose
qu'on construit, pas qu'on clone.

```bash
python src/harvest_osirion.py --check              # trois appels, dit ce qu'il voit
python src/harvest_osirion.py --passes 3,10        # le calendrier, puis les classements — des heures
python src/refresh.py --fetch --publish            # rattraper, reconstruire le modèle, mettre le site à jour
python src/refresh.py --page --publish             # la page et le calendrier de la semaine, en quelques secondes (site.bat) : pas de modèle
python src/pull_live.py                            # les relevés du flux en direct des derniers jours, vers la base
python src/import_session.py session-*.json        # une soirée suivie sur le prédicteur, vers la base
python src/import_session.py --list                # quels tournois portent assez de relevés pour tester
```

`refresh.py` est la commande du quotidien : une passe de moisson légère pour les nouvelles
fenêtres, la dérivation, l'export (refusé tant qu'il ne reproduit pas le modèle), le
calendrier de la semaine, la construction du site, et un push. Sa docstring contient la ligne
du Planificateur de tâches qui le lance chaque matin. Une fois par semaine est le minimum,
quoi qu'on saute par ailleurs : le calendrier publié couvre sept jours, et c'est lui que le
flux en direct lit pour savoir quelles cups sont en cours — plus vieux que ça, le flux ne
suit plus rien.

La façon de nommer un tournoi décide quelles éditions comptent comme la même cup, donc les
règles sont peu nombreuses et écrites. Le nom, ce sont les deux lignes du titre d'Epic —
« FNCS | Division 2 » n'est pas « FNCS », et « Fortnite | Performance Evaluation » est une
cup hebdomadaire, pas le jeu. La manche vient de l'identifiant de la fenêtre et de lui seul :
le numéro `round` qu'Epic met à côté est un compteur de semaines, et le lire comme une
manche a un jour classé la semaine 2 d'une cup hebdomadaire comme son « Round 2 », une
catégorie par semaine. Quand ces règles changent, la construction suivante s'en aperçoit —
la base porte la version qui l'a construite — et re-dérive tous les tournois moissonnés,
sept minutes environ, pour que les anciennes et les nouvelles éditions ne se retrouvent
jamais sous des noms différents. Ce qu'une cup fait gagner est lu dans le même catalogue :
les tables par rang comptent à partir de 1, les tables en percentile sont une part de
l'effectif, et les tables en score ne sont pas des positions du tout. Le prédicteur affiche
ces paliers et les demande en premier. Qui peut entrer y est lu aussi — l'exigence
`currentRanking:<échelle>:<n>` d'Epic, gardée en `competition.entry` — parce que la même cup
réservée aux Unreal une semaine et ouverte dès Diamant la suivante, ce sont deux effectifs de
tailles différentes, et le premier barreau du modèle lit l'édition précédente avec la même
condition d'accès.

Les classements en direct pendant un tournoi viennent de l'API [Cito](https://citoapi.com) —
une clé gratuite donne 500 requêtes par mois, gardée dans `cito_key.txt`, ignoré par git.
Tout sauf l'import en direct fonctionne sans clé.

L'interface est en anglais par défaut, avec un bouton FR dans l'en-tête
([`i18n.py`](i18n.py) contient les traductions).

### La couche d'analyse

```bash
pip install -r analysis/requirements.txt
python -m analysis.fit           # ré-estime a et b, avec intervalles
python -m analysis.diagnostics   # résidus, corrélation intra-tournoi, hétéroscédasticité
python -m analysis.validate      # prédit les tournois les plus récents, contre deux références
python -m analysis.anchor        # pourquoi le niveau se lit au rang 20
python -m analysis.shape         # la forme est-elle une courbe, une table, une fonction du terrain ?
python -m analysis.figures       # régénère les figures
python -m analysis.live          # rejoue les classements partie par partie : ce que vaut un seuil en cours de cup
```

`analysis/` utilise numpy, pandas, scipy et matplotlib. L'app, non : rien sous `analysis/`
n'est importé par `app.py` ni par le modèle, donc l'app tourne toujours sur un Python nu. La
flèche ne va que dans un sens.

---

## Organisation

Les lanceurs, la base et les notes à la racine ; le code dans `src/`, lancé par
chemin (`python src/refresh.py`) ; la couche de recherche dans `analysis/`, lancée
comme module depuis la racine (`python -m analysis.validate`).

| | |
|---|---|
| `update.bat`, `site.bat`, `harvest.bat`, `tracker.bat` | les quatre choses qu'une personne lance ; `refresh.bat` est `update.bat` avec des options, et ce que la tâche planifiée appelle |
| `data/` | la base, les pages moissonnées, les clés, le journal — jamais commités |
| `src/app.py` | routes Flask, et la validation d'entrée que traverse chaque écriture |
| `src/calibration.py` | le modèle : la cascade à cinq échelons, les tables mesurées de forme, de niveau et de lobby fermé, l'ajustement de la courbe |
| `src/predict.py` | prédictions pendant une session — rythme, extrapolation, ma course |
| `src/harvest_osirion.py`, `src/osirion.py` | le jeu d'entraînement, depuis l'API publique d'Osirion, de façon reprenable |
| `src/refresh.py` | la commande unique : passe de moisson, dérivation, export, calendrier, construction, push |
| `src/pull_live.py` | les relevés du flux en direct — chaque cup qu'il a suivie, lue toutes les dix minutes — rangés contre les tournois que la moisson a construits, rapprochés par les identifiants d'Epic ; `refresh.py` le lance |
| `src/import_session.py` | une soirée suivie sur le prédicteur, relue dans la base sous forme de relevés et de résultat — relevés saisis à la main et relevés pris par le flux en direct du site (marqués `auto`) confondus |
| `src/calendar_snapshot.py` | la semaine à venir, écrite à côté du prédicteur en `calendar.js` |
| `src/export_model.py` | `model.json` pour le prédicteur, refusé tant qu'il ne reproduit pas le modèle Python |
| `src/scoring_infer.py` | retrouve le barème d'un tournoi à partir des totaux, par moindres carrés |
| `src/db.py` | schéma SQLite, migrations, la taxonomie, toutes les requêtes |
| `src/cito.py` | client de l'API de classements en direct |
| `src/i18n.py` | chaînes sources anglaises, traductions françaises |
| `analysis/` | numpy/pandas/scipy : ré-estimation, diagnostics, validation croisée, figures ; `validation.json` est ce que la dernière validation a mesuré, emporté dans le modèle pour que le site ne cite rien qu'il n'ait pas gagné |
| `docs/` | [note de méthodologie](docs/methodology.md), [manuel](docs/manual.fr.md) |

Les vérifications, toutes exécutables :

```bash
python src/backtest.py       # erreur en avançant dans le temps, sur les tournois suivis
python src/test_osirion.py   # le lecteur d'API, contre des réponses de la forme réelle
python src/check_forms.py    # chaque champ de formulaire survit à l'aller-retour vers la base
python src/fuzz_api.py       # 2 856 requêtes malformées, 0 erreur serveur
python src/selfcheck.py      # tout ce qui précède plus les deux langues, d'un coup
python src/cleanup.py --list # ce que le dossier contient et que plus aucun code n'atteint
```

`check_forms.py` existe pour une raison qui mérite d'être rappelée : un champ de formulaire a
été perdu un jour entre le navigateur et la base, et une après-midi de résultats saisis à la
main avec lui. Il envoie maintenant une valeur reconnaissable dans chaque champ de chaque
formulaire et les relit tous.

---

## Ce que je corrigerais ensuite

Dans l'ordre que justifient les chiffres :

1. **Le départ à froid.** La moitié des tournois d'une nouvelle saison n'ont pas d'édition
   précédente, et ils sont prédits à 20 %. La signature qui les regroupe — genre de cup,
   plateforme, stade — a fait passer l'erreur sur la part de 12,0 % à 9,9 % en isolation, et
   n'a rien changé sur la coupe chronologique, parce que le groupe d'un format nouveau est
   généralement vide lui aussi. Un plus proche voisin sur le barème, le terrain et le nombre
   de parties est la chose évidente à essayer ensuite.
2. **Le terrain est censuré à 9 950.** Le classement Osirion s'arrête à la page 100, donc
   toute épreuve de plus de dix mille rosters enregistre la même taille. L'endpoint Cito
   renvoie les classements entiers et lèverait ce plafond.
3. **`b` n'est pas identifié.** Au fil des réajustements de la journée il est allé de 0,38 à
   0,90 sur des données de même nature. Ça compte moins qu'avant — la courbe est troisième
   dans la cascade — mais un nombre aussi instable est une valeur de travail, pas une mesure.
4. **Le raffinement en direct est mesuré pour une moitié et pas l'autre.** Chaque classement
   moissonné porte l'historique partie par partie de chaque équipe, donc `analysis/live.py`
   peut reconstruire le classement à tout instant de la session ; il trouve qu'à la moitié
   des parties un seuil est à la moitié de sa valeur finale, à ±15 % d'une cup à l'autre, et
   le prédicteur met désormais sa propre échelle à ce que disent les relevés face à cette
   courbe. Sa première version faisait passer les relevés par la courbe des files ouvertes
   dans un lobby de vingt équipes, et transformait 153 points à mi-parcours en 506. Le même
   rejeu mesure jusqu'où un relevé se propage dans l'échelle, et la réponse dépend du
   format : dans une file ouverte tout le classement bouge ensemble (pente 0,86 entre rangs),
   un relevé chiffre donc tous les rangs ; dans un lobby fermé les rangs bougent
   indépendamment (pente 0,00, corrélation −0,09) parce que les mêmes vingt équipes se
   partagent un pot fixe — un relevé n'affine donc plus que son propre rang. Ce qui reste non
   mesuré, c'est la combinaison des relevés et de l'historique : pondérée par la précision,
   ce qui est fondé, et non validée sur des tournois tenus à l'écart, ce qui est le prochain
   rejeu à lancer.

---

Licence MIT. Fortnite est une marque d'Epic Games ; ce projet n'a aucun lien avec eux et
n'utilise aucune ressource du jeu.
