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
| `update.bat` | tout — une passe de moisson pour les nouveaux tournois, le modèle rebâti et vérifié, le calendrier de la semaine, la page, le push des deux dépôts (celui du site et celui-ci) ; dix minutes |
| `site.bat` | la page et le calendrier de la semaine seulement, et le même push ; quelques secondes |
| `harvest.bat` | le long téléchargement des tournois passés, une nuit |
| `tracker.bat` | l'app locale de suivi |

**Mesuré comme une prévision — chacun des 600 tournois les plus récents prédit à partir de
tout ce qui s'était terminé avant son jour, sans que rien ne voie le futur : 2,8 % d'erreur
médiane quand la cup a déjà eu lieu, 5,2 % sur l'ensemble des seuils, 94 % des seuils réels
dans la fourchette annoncée.** Quand la cup n'a jamais eu lieu dans sa région, des
classements récents de son format sont rejoués avec son propre barème et l'erreur est de 4 à
5 % aux rangs 1 à 250, d'environ 6 % jusqu'au rang 1 000, contre 8 à 11 % pour l'échelon du barème qu'ils
remplacent (102 cups depuis le 15 août, chacune lue sur des classements joués avant elle). Le
jeu d'entraînement compte 7 632 tournois et 94 286 seuils lus sur l'API publique
d'Osirion (22 septembre 2026 ; la mise à jour remesure ces chiffres tous les trois jours,
`python -m analysis.validate`, et ils voyagent avec le modèle). [`docs/methodology.md`](docs/methodology.md) explique comment c'est
mesuré et où le modèle cesse d'être crédible.

![Courbe ajustée sur les seuils observés](analysis/figures/curve.png)

---

## Le modèle

Une cascade, la lecture la plus directe d'abord. Chaque échelon ne répond que si celui du
dessus ne le peut pas.

1. **L'édition précédente, lue telle quelle.** Même cup, même région, même format, même
   rang, la dernière fois. La fourchette est le 80e centile de ce que ce rang a bougé entre
   deux éditions consécutives de cette cup — mesuré, pas supposé. Une cup qui n'a jamais été
   jouée dans son format — la première semaine en Duo après une saison en Trio, une semaine
   Reload d'une cup Battle Royale — lit sa dernière édition dans un autre format avec la
   fourchette élargie de moitié, sauf si c'est un lobby unique, que l'échelon 4 chiffre mieux
   qu'un lobby d'une autre taille. C'est le report de la dernière édition
   promu au rang de modèle, et il est premier parce qu'il n'a pas cessé de gagner : une soirée
   forte monte le rang 1 et le rang 20 ensemble, et toute prévision qui multiplie le niveau
   d'une édition par un ratio pris sur d'autres jette cette corrélation. Trois corrections
   depuis : l'édition lue est la dernière avec la même condition d'accès que la cup à venir
   (`competition.entry`, lu dans l'exigence `currentRanking` d'Epic — une cup réservée aux
   Unreal est un autre effectif qu'une cup ouverte dès Diamant) quand elle a été jouée dans
   la même saison que la dernière en date — une saison en arrière, une édition à la même
   condition lit moins bien que celle de la semaine passée à l'autre, mesuré —, la fourchette
   élargie de moitié sinon ; un effectif connu qui n'est pas celui de l'édition déplace la
   valeur le long du terme de quantile de la courbe, plafonné à un cinquième, ce qui sur les
   600 tournois les plus récents ramène l'erreur de ce barreau sur ces lignes de 6,3 % à
   4,7 % — le terme du rang de référence doit en rester exclu : pris en rapport, il dit qu'un
   effectif plus petit monte les premiers rangs, et les données refusent ; et une édition lue
   par-dessus un changement de saison est déplacée de ce que les premières cups de la saison
   ont montré à cette tranche de rangs (la médiane du mouvement, rabattue par n / (n + 10)),
   sa fourchette élargie de ce que le mouvement laisse — une nouvelle saison monte toutes les
   cups d'un coup, +5 % en tête et +11 % au-delà du rang 500 à la saison 42, rien du tout à
   la saison 41, et ça ne se sait pas avant que les premières cups de la saison aient été
   jouées. Chaque rang de la table porte la saison et la date de l'édition sur laquelle il a
   été lu, parce que la dernière édition est lue aussi profond qu'elle a été moissonnée et
   qu'un rang au-delà vient d'une plus ancienne ; la page dit laquelle. Voir
   `docs/methodology.md`, « The first edition of a season ». Et depuis le 22 septembre,
   l'édition est lissée : la suite d'éditions jouées comme la dernière — même condition
   d'accès, même saison, même nombre de parties, même barème — moyennée en logarithme, la
   dernière pesant 0,7 et chacune des précédentes 0,7 de ce qui reste, six au plus. L'erreur
   médiane ne bouge pas et la queue se resserre : sur les 1 711 seuils qu'il change dans la
   validation glissante, moyenne 7,24 → 6,59 %, 90e centile 14,0 → 13,4 %.
2. **Le niveau fois la forme mesurée.** Le seuil de la cup au rang 20 lors de l'édition
   précédente, fois ce que ce rang valait par rapport au rang 20 sur les éditions de la cup —
   une table, pas une courbe, parce que la table a divisé l'erreur par deux là où elle
   s'applique. La catégorie d'abord, puis la famille toutes régions confondues.
3. **Le niveau fois l'échelle**, pour les rangs qu'aucune édition n'a mesurés : ce que vaut
   chaque rang par rapport au rang 20 sur toutes les files ouvertes de la même tranche de
   taille de plateau (onze tranches, plus fines que celles de la courbe : le rang 1 000 est
   la dernière place d'une file de 1 100 et le milieu d'une file de 2 900) et du même mode
   de jeu — une médiane par rang sur au moins vingt classements, la ligne du mode d'abord,
   celle de la tranche tous modes confondus en secours. Elle remplace la courbe ajustée

       seuil(rang) = niveau · exp(−a · (q^b − q_ref^b)),    q = rang / effectif

   au fond de l'échelle, où la courbe tombait 9 % trop bas au rang 500 et 13 % trop bas au
   rang 1 000 hors échantillon (deux paramètres ne plient pas les deux bouts d'une échelle)
   quand l'échelle tient à 1 % près ; la courbe, toujours ajustée par tranche de taille de
   plateau, répond pour les files de moins de trois cents équipes et pour une tranche et un
   rang où l'échelle a trop peu de classements.
4. **Des classements récents rejoués avec le barème de la cup**, pour une cup sans édition
   terminée dans sa région. Chaque classement moissonné porte, pour chaque équipe, son
   placement et ses éliminations à chaque partie jouée ; repayées avec le barème de la
   nouvelle cup — les *n* premières parties quand la cup en autorise *n* — les mêmes parties
   donnent le classement que ce barème aurait produit, et son rang 20 est une prévision pour
   le rang 20 de la nouvelle cup. Les donneurs : les six classements les plus récents de la
   même région, taille d'équipe, mode de jeu et plateforme, files ouvertes, ceux joués au
   même nombre de parties d'abord et les autres ramenés par l'exposant par partie ; la
   médiane entre donneurs, fiable jusqu'au tiers des équipes chargées, prolongée le long de
   l'échelle au-delà. Quand la cup a déjà tourné dans d'autres régions, moitié de cette
   lecture et moitié du rejeu, en logarithme, battent chacune prise seule.
   `calendar_snapshot.py` écrit le rejeu à côté des lignes de la semaine, une douzaine de
   nombres par cup ; la page le lit comme cet échelon.
5. **Le lobby fermé**, pour une finale jouée dans un seul lobby dont aucune édition n'a été
   vue — le cas habituel d'un Round 2 dont le modèle connaît le Round 1. Chaque seuil de
   chaque finale à lobby unique du jeu d'entraînement est divisé par le maximum qu'une équipe
   pouvait marquer sur les parties jouées et rangé par la part du lobby que représente le
   rang, par mode de jeu et taille d'équipe ; l'estimation se lit dans cette table,
   log-linéairement entre deux cases, avec la dispersion entre finales pour fourchette. Cet
   échelon existe parce que les deux du dessous sont mesurés sur des files ouvertes de
   milliers d'équipes : dans un lobby de vingt, le rang 20 est la dernière équipe, et la
   courbe faisait valoir au vainqueur dix fois l'ancre. Les dernières places d'un lobby — au-
   delà de 90 % de celui-ci — n'ont pas de chiffre sauf si l'édition précédente les a
   publiées : ce sont des équipes parties après une ou deux parties, et chaque échelon les
   chiffrait quatre fois trop haut.
6. **Le seul barème**, pour une cup que personne n'a vue et sans classement à rejouer. Le seuil au rang 20 est une part du
   maximum qu'une équipe peut marquer, la part étant lue sur les cups du même genre, de la
   même plateforme et du même stade — une finale FNCS sur PC n'est pas une cup mobile de
   créateur, et le second round d'une cup, quelques centaines d'équipes qualifiées sur une
   session plus courte, n'est pas son round ouvert — et rapprochée d'un a priori avec un
   poids `n / (n + 2)`. Les lobbys uniques n'alimentent pas cette part : une heat de seize
   n'a pas de rang 20, et lue à travers la courbe elle a un temps fixé le départ à froid
   mobile au dixième de sa valeur. C'est le maillon faible — 13 % trop bas sur les cups skin
   Battle Royale, 17 % trop haut en Reload — et le rejeu ci-dessus existe à cause de lui.

Deux choses que les données ont tranchées en chemin :

- **Le nombre d'équipes compte peu** pour le niveau : doubler l'effectif déplace un seuil
  d'environ 3,5 %. Mais il compte beaucoup au fond de l'échelle, et c'est pourquoi la table de
  forme propre à une cup s'arrête au rang 500 et laisse la place à l'échelle commune, qui
  est rangée par taille de plateau.
- **Le niveau a une édition d'âge, pas une médiane.** Lu sur l'édition précédente, il prédit à
  5,4 % ; la médiane des trois dernières, à 5,7 % ; des huit dernières, à 6,4 %. Un historique
  plus vieux que la dernière édition ne compte que s'il a été joué de la même façon, et alors
  seulement avec la dernière édition qui pèse le plus — le lissage de l'échelon 1, qui garde
  la médiane et resserre la queue ; une tendance prolongée a perdu partout : les cups
  oscillent, elles ne dérivent pas.

---

## Résultats

Une prévision glissante, pas une coupe aléatoire : les 600 tournois les plus récents — tout
ce qui commence au 17 août 2026 — chacun prédit à partir des 7 032 d'avant la fenêtre et
de chaque tournoi retenu qui a commencé un jour plus tôt, ce que l'app a sous la main le soir
même. Rien ne voit le futur, ni le modèle ni les références. Prédit à froid, avant tout relevé
du classement en direct. (La coupe figeait auparavant le passé au premier jour retenu : la
cinquième semaine d'une cup hebdomadaire était chiffrée d'après la saison d'avant et chaque
édition d'une cup née dans la fenêtre comptait comme un départ à froid ; sur les mêmes soirées
et le même code, cela donnait 12,9 % contre 6,8 % — un chiffre sur la coupe, pas sur le
modèle. `--frozen` l'imprime encore.)

**Quand la cup a déjà eu lieu** (3 469 seuils sur 369 tournois, les lignes où le modèle et
les deux références répondent ; 22 septembre 2026) :

| tranche de rangs | ce modèle | édition précédente | médiane de catégorie | n |
|---|---:|---:|---:|---:|
| 1 – 5 | 4,3 % | 4,2 % | 4,8 % | 1 148 |
| 6 – 25 | 2,1 % | 2,3 % | 3,7 % | 975 |
| 26 – 100 | 2,2 % | 2,4 % | 6,1 % | 496 |
| 101 – 500 | 2,6 % | 3,1 % | 8,6 % | 648 |
| au-delà de 500 | 5,6 % | 9,2 % | 12,0 % | 202 |
| **tout** | **2,8 %** | **3,1 %** | **5,6 %** | **3 469** |

Le modèle part de l'édition précédente — c'est l'échelon 1 — et bat le report sur lequel il
est bâti de 0,28 point, tout l'intervalle au-dessus de zéro (+0,05 à +0,53), et la médiane
de catégorie de 2,8 ; les cinq premiers rangs sont la seule tranche où la semaine dernière
lue telle quelle fait aussi bien. L'avance, c'est l'effectif : un nombre d'équipes connu qui n'est pas
celui de l'édition déplace la valeur le long du terme de quantile de la courbe. Ce que le
report ne sait toujours pas faire, il le fait aussi — une fourchette, une réponse pour les
rangs que la semaine dernière n'a pas publiés, et une réponse pour les cups qui n'ont pas de
semaine dernière.

**Quand la cup n'a jamais eu lieu** (1 950 seuils sur 159 tournois, le premier jour de
chaque nouvelle cup dans chaque région) : 10,7 % d'erreur médiane depuis le seul barème, 96 %
dans la fourchette. La validation ne rejoue pas de classements : c'est l'échelon que le rejeu
ci-dessus remplace sur le site, à 4 à 5 %.

**Les finales à lobby unique jamais vues** (166 seuils sur 34 tournois) passaient par ce
même échelon du barème et sa courbe de file ouverte : 124 % d'erreur médiane, le vainqueur
d'une finale Reload à vingt équipes chiffré à 2 900 points quand 300 était le maximum
possible. Lues sur les finales du même format par part du lobby, elles sortaient alors à
7 %, et à 13,5 % sur les 600 plus récents (175 seuils sur 29 finales, 79 % dans la
fourchette) ; les dernières places d'un lobby sont refusées plutôt que chiffrées.

**La fourchette est large.** 94 % des seuils tombent dans une fourchette qui en annonce
80 % ; au niveau nominal de 89 %, la couverture réelle est de 97 %. Les fourchettes à 50 % et
à 90 % de la page sont mesurées directement, comme quantiles de l'erreur en unités de cette
fourchette, donc elles ont la largeur qu'elles annoncent.

**La première édition comparable vaut 0,9 point** d'erreur médiane (6,8 % sans aucune, 6,0 %
avec une, sur les tournois qui ont au moins six pairs) ; les cinq suivantes en valent 0,5 à
elles cinq.

Mesuré sur une coupe aléatoire à la place — chaque tournoi retiré à son tour avec le reste
en historique — l'édition la plus proche est souvent celle de la semaine *suivante*, et son
résultat n'est pas disponible le soir même. Ce chiffre est plus agréable et faux à publier.

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

Une fenêtre lue avant la fin de sa cup est relue. Interrogée sur une cup qui n'a pas
commencé, l'API répond par un classement vide et `totalPages: 0` ; interrogée pendant, par
un classement qui bouge encore. L'un comme l'autre, gardés sur le disque, étaient lus par
chaque passe suivante comme « cette fenêtre est finie » : un calendrier moissonné un jour à
l'avance figeait ainsi chaque cup à vide, et aucune n'entrait jamais dans la base. Une page
écrite avant la fin de la fenêtre plus une demi-heure ne compte désormais plus comme une
réponse, et la fenêtre est retéléchargée depuis la page zéro.

La page où tombe chaque palier — le rang de qualification, les rangs payés — est téléchargée
avec la première passe, une requête par palier, et le rang du palier est enregistré à côté
des quinze rangs fixes : le palier est le rang qu'on demande au modèle, et une première
passe de trois pages n'atteignait jamais un palier à quatre mille, si bien que le modèle
lisait ce rang sur n'importe quelle édition plus ancienne qui le portait. Une fenêtre déjà
dans la base est re-dérivée, en place, quand elle a plus de pages sur le disque que la
dernière dérivation n'en a lues — les passes profondes, les pages des paliers —, et une
reconstruction re-dérive aussi chaque fenêtre en place : la ligne reste, et avec elle les
relevés du flux en direct, qu'une version antérieure supprimait avec la ligne.

```bash
python src/harvest_osirion.py --check              # trois appels, dit ce qu'il voit
python src/harvest_osirion.py --passes 3,10        # le calendrier, puis les classements — des heures
python src/refresh.py --fetch --publish            # rattraper, reconstruire le modèle, mettre le site à jour
python src/refresh.py --page --publish             # la page et le calendrier de la semaine, en quelques secondes (site.bat) : pas de modèle
python src/pull_live.py                            # les relevés du flux en direct des derniers jours, vers la base
python src/rescore.py --event ID --window ID       # une cup terminée rejouée depuis les classements d'avant, face à son classement
python -m analysis.rescore                         # le rejeu face aux échelons à froid, hors échantillon, depuis la mi-août
python src/import_session.py session-*.json        # une soirée suivie sur le prédicteur, vers la base
python src/import_session.py --list                # quels tournois portent assez de relevés pour tester
```

`refresh.py` est la commande du quotidien : une passe de moisson légère pour les nouvelles
fenêtres — trois pages chacune, dix pour les fenêtres des trois dernières semaines, dont le
rejeu d'une nouvelle cup est fait — la dérivation, les relevés du flux, tous les trois jours
les tables de rythme, la validation et les poids d'une réponse en direct (`analysis.live`,
`analysis.validate`, `analysis.blend`), l'export (refusé tant qu'il ne reproduit pas le modèle), le calendrier
de la semaine avec le rejeu à côté de chaque nouvelle cup, la construction du site, et un
push. Sa docstring contient la ligne du Planificateur de tâches qui le lance chaque matin. Le
calendrier est ce que le flux en direct lit pour savoir quelles cups sont en cours, et une cup
annoncée entre deux passages tournerait sans être suivie : le workflow du dépôt du prédicteur
le rafraîchit depuis GitHub toutes les trois heures, en gardant les cellules de rejeu écrites
ici, et `refresh.py` reprend ces commits avant d'écrire quoi que ce soit.

La façon de nommer un tournoi décide quelles éditions comptent comme la même cup, donc les
règles sont peu nombreuses et écrites. Le nom, ce sont les deux lignes du titre d'Epic —
« FNCS | Division 2 » n'est pas « FNCS », et « Fortnite | Performance Evaluation » est une
cup hebdomadaire, pas le jeu. La manche vient de l'identifiant de la fenêtre et de lui seul :
le numéro `round` qu'Epic met à côté est un compteur de semaines, et le lire comme une
manche a un jour classé la semaine 2 d'une cup hebdomadaire comme son « Round 2 », une
catégorie par semaine. Quand ces règles changent, la construction suivante s'en aperçoit —
la base porte la version qui l'a construite — et re-dérive tous les tournois moissonnés,
sept minutes environ, pour que les anciennes et les nouvelles éditions ne se retrouvent
jamais sous des noms différents. Epic renomme des cups entre deux saisons en gardant
l'identifiant — « Solo Victory Cup » est devenue « Solo Victory Cup Battle Royale » à la
saison 42, « FNCS Division 2 » est devenue « FNCS Division 2 Practice » —, donc chaque
édition d'une série est rangée sous le nom qu'Epic donne à la cup aujourd'hui, clé sur
l'identifiant de série lu dans celui de l'événement ; une ligne saisie à la main sous un
ancien nom suit, et les paliers saisis pour elle aussi. Sous le nom du jour, une cup
renommée commençait chaque saison sans aucun historique. Ce qu'une cup fait gagner est lu dans le même catalogue :
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
python -m analysis.blend         # le poids des relevés d'une cup face à son historique, sur les soirées du flux
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
| `src/calibration.py` | le modèle : la cascade à six échelons, les tables mesurées de forme, l'échelle commune par taille de plateau, les tables de niveau et de lobby fermé, l'ajustement de la courbe |
| `src/predict.py` | prédictions pendant une session — rythme, extrapolation, ma course |
| `src/harvest_osirion.py`, `src/osirion.py` | le jeu d'entraînement, depuis l'API publique d'Osirion, de façon reprenable |
| `src/refresh.py` | la commande unique : passe de moisson, dérivation, export, calendrier, construction, push |
| `src/pull_live.py` | les relevés du flux en direct — chaque cup qu'il a suivie, lue toutes les dix minutes, avec le nombre de pages du classement et, là où l'API le pagine en entier, le nombre exact d'équipes classées à chaque relevé — rangés contre les tournois que la moisson a construits, rapprochés par les identifiants d'Epic ; le classement vingt minutes après la fin est rangé comme résultat final aux rangs pour lesquels la moisson n'a rien ; `refresh.py` le lance |
| `src/import_session.py` | une soirée suivie sur le prédicteur, relue dans la base sous forme de relevés et de résultat — relevés saisis à la main et relevés pris par le flux en direct du site (marqués `auto`) confondus |
| `src/calendar_snapshot.py` | la semaine à venir, écrite à côté du prédicteur en `calendar.js`, avec la table de rejeu à côté de chaque cup sans édition dans sa région ; sans la base (le workflow du dépôt) il garde les tables du dernier passage |
| `src/rescore.py` | une cup que personne n'a vue, chiffrée en rejouant des classements récents de son format avec son propre barème |
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

1. **Le départ à froid, ce qu'il en reste.** Une cup sans édition dans sa région est
   désormais rejouée depuis les classements récents de la région avec son propre barème, à
   4 à 5 % là où l'échelon du barème lisait 8 à 11 %. Ce qui reste : les donneurs sont les
   classements de la saison en cours, donc les premières cups froides d'une nouvelle saison
   rejouent la saison d'avant, sans le déplacement que l'échelon direct applique ; un format
   sans aucun classement sur le disque — les cups Arena, dont le nombre de parties n'est pas
   fixe — retombe sur le barème et ses 14 % ; et le rejeu n'est fiable qu'au tiers de la
   profondeur chargée, ce qui est pourquoi la moisson lit les fenêtres les plus récentes dix
   pages de profondeur.
2. **Le terrain est censuré à 9 950.** Le classement Osirion s'arrête à la page 100, donc
   toute épreuve de plus de dix mille rosters enregistre la même taille. L'endpoint Cito
   renvoie les classements entiers et lèverait ce plafond.
3. **`b` n'est pas un seul nombre.** Ajusté par tranche de taille de plateau, il va de 1,25
   sous mille équipes à 0,15 au-delà du plafond de l'API, et c'est pourquoi la courbe est
   désormais ajustée par tranche. À l'intérieur d'une tranche, c'est une valeur de travail
   plus qu'une mesure — les tranches sont tracées à la main, et le `q` de la tranche du
   plafond est une convention.
4. **Le raffinement en direct, mesuré.** Chaque classement
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
   partagent un pot fixe — un relevé n'affine donc plus que son propre rang. La combinaison
   des relevés et de l'historique est pondérée par la précision, et depuis le 22 septembre
   les deux largeurs sont ramenées aux unités de l'erreur typique de chaque côté, mesurée
   tous les trois jours sur les soirées du flux (`analysis/blend.py`) : rejouée dans la page
   sur une semaine tenue à l'écart, l'erreur médiane à mi-session est passée de 3,4 à 3,1 %,
   la prévision d'une cup qui a une édition précédente a parcouru 12 % sur sa soirée au lieu
   de 17 %, et les fourchettes — désormais mesurées sur des réponses en direct
   (`pace.live_bands`) — ont contenu 46 % et 88 % des finaux là où elles en annoncent 50 et
   90 %. Le 90e centile en fin de session est un demi-point moins bon : c'est ce que le
   prochain rejeu doit regarder. Un morceau mesuré à part, c'est la fin de cup : le tassement lu sur les
   heures de partie dit que le tableau est final et certain vingt minutes après le buzzer,
   alors que le tableau que la page lit est la copie publiée par Osirion, qui arrive plus tard
   et peut être 5 % en dessous du final. Cette largeur est donc mesurée sur les soirées du
   flux (`pace.tail_feed`), séparée selon que le classement a été relu à l'identique ou non.
5. **Le rythme est celui d'un genre de cup, pas de toutes les cups confondues.** Rejouée par
   famille — mode de jeu, taille d'équipe, durée de la fenêtre, plafond de parties — la part de
   sa valeur finale qu'un seuil a atteinte à mi-session va de 0,42 à 0,53, et à l'heure de
   0,87 à 0,94 : une cup Battle Royale de deux heures a une partie de trente minutes encore en
   l'air au buzzer, une cup plafonnée à dix parties Reload courtes n'a plus rien à jouer.
   `analysis/live.py` mesure désormais la courbe et la queue par famille — et par genre de
   cup et plateforme à l'intérieur, parce qu'à mi-session les cups d'entraînement FNCS
   avaient atteint 45 % de leur valeur finale là où les cups skin du même format en étaient
   à 56 %, et qu'une famille confondant les deux chiffrait chaque nouvelle cup skin trop haut
   pendant deux heures — et par cup (ses éditions les plus récentes, à moins de 45 jours et
   du même format, sinon la famille), et — parce que la moisson rejoue un classement à partir des
   équipes qui finissent dans ses premières pages, et que dans une cup Solo grand public les
   joueurs en tête à mi-session qui s'arrêtent ensuite n'y sont pas, si bien que le rejeu y
   court 4 à 9 % trop bas — par cup telle que le flux l'a lue, que la page préfère dès que
   quatre soirées ont été suivies. Le fond d'une file ouverte a son propre rythme, fixé par la
   profondeur dans le peloton plutôt que par le rang : le millième d'un peloton de deux mille
   est fini au dernier cinquième de la session, le millième de dix mille garde le rythme du
   haut. Ce rapport est mesuré par tranche de rang / peloton sur les soirées du flux
   (`pace.depth`), et le peloton qu'il lui faut, c'est le nombre de pages du classement, que
   le flux garde maintenant à chaque relevé. Deux corrections viennent du premier jour des
   qualifications FNCS Solo : au plafond de l'API (9 950 comptés pour dix mille ou plus), aucun
   rang n'est lu comme la moitié grand public, et dans une qualification FNCS, dont le fond
   joue pour la qualification, un rang n'est pas lu plus profond que la tranche 0,1–0,2 — le
   premier cinquième d'une cup ordinaire — d'une table désormais mesurée sur les autres cups
   seulement. Les fourchettes d'une réponse en direct ont leurs
   propres multiplicateurs (`pace.live_bands`), mesurés sur les mêmes soirées. Backtest
   glissant sur 110 soirées : erreur médiane 5,0 → 4,2 % entre trois et cinq dixièmes de la
   session, 4,7 → 3,6 % entre cinq et sept, 2,1 → 1,4 % dans les dix minutes après la clôture.
   Voir `docs/methodology.md`, « The pace of a kind of cup ».

---

Licence MIT. Fortnite est une marque d'Epic Games ; ce projet n'a aucun lien avec eux et
n'utilise aucune ressource du jeu.
