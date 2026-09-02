# Fortnite Comp Tracker

*[English version](README.md)*

Prédit les seuils de points des compétitions Fortnite — combien de points il faudra pour
finir à un rang donné — avant le début du tournoi et pendant qu'il se joue.

Les seuils ne sont publiés qu'après coup. Un joueur qui se demande s'il continue à jouer, ou
avec quelle agressivité, avance à l'aveugle. L'app estime la réponse à partir du barème du
tournoi, du nombre d'équipes, et de ce qu'ont donné les éditions précédentes de tournois
comparables.

**Mesuré hors échantillon : 6,4 % d'erreur médiane sur la prédiction à froid, 86 % des seuils
réels dans la fourchette annoncée**, sur 290 seuils vérifiés à la main issus de 66 tournois,
en retirant un tournoi à la fois de l'historique.
[`docs/methodology.md`](docs/methodology.md) explique comment c'est mesuré et où le modèle
cesse d'être crédible.

![Courbe ajustée sur les seuils observés](analysis/figures/curve.png)

---

## Le modèle

    seuil(rang) = niveau · exp(−a · (q^b − q_ref^b)),    q = rang / effectif

`niveau` est le seuil du tournoi au rang 20 ; `a = 1,255` et `b = 0,370` décrivent la forme du
classement et sont partagés entre tournois. La forme dépend de la façon dont Fortnite compte
le placement et les éliminations ; le niveau, lui, change d'une semaine à l'autre.

Deux conséquences font tout l'intérêt de l'outil :

- **Le nombre d'équipes compte peu.** L'élasticité `d ln(seuil) / d ln(effectif) = a·b·(q^b − q_ref^b)`
  vaut environ 0,05 : doubler le nombre d'équipes déplace le seuil de 3,5 %. Une estimation
  grossière de la participation suffit donc — ce qui tombe bien, puisque c'est justement ce
  qu'on ne peut pas savoir à l'avance.
- **Le niveau se lit au rang 20, pas au rang 1.** D'une édition à l'autre d'un même tournoi, le
  rythme bouge de 7,7 % au rang 1 mais de 3,4 % au rang 20. Le rang 1, c'est une équipe en
  forme ; le rang 20, c'est une statistique de population. S'ancrer au rang 1 coûte 2,8 points
  d'erreur, la seule chose que les données tranchent nettement sur ce choix
  ([`analysis/anchor.py`](analysis/anchor.py)).

Quand aucune édition comparable n'existe, le rapport barème → points est rapproché d'un a
priori avec un poids `n / (n + 2)`. C'est le maillon faible du modèle, et la note de
méthodologie en donne le prix : 34,6 % d'erreur médiane, contre 5,9 % dès qu'une seule
édition comparable existe.

---

## Résultats

Un tournoi retiré à la fois, prédit à froid — avant tout relevé du classement en direct.

| tranche de rangs | n | erreur médiane | moyenne | couverture |
|---|---:|---:|---:|---:|
| 1 – 5 | 94 | 8,7 % | 12,2 % | 85 % |
| 6 – 25 | 94 | 5,7 % | 12,6 % | 82 % |
| 26 – 100 | 47 | 9,5 % | 12,7 % | 81 % |
| 101 – 500 | 38 | 6,0 % | 10,9 % | 97 % |
| au-delà de 500 | 17 | 2,2 % | 3,3 % | 100 % |
| **tout** | **290** | **6,4 %** | **11,7 %** | **86 %** |

Face aux deux références à battre, sur les 206 lignes où les trois répondent : ce modèle
5,68 %, le report du seuil de l'édition précédente 6,03 %, la médiane de la catégorie 7,56 %.
Avec un bootstrap sur les tournois, l'écart au report vaut +0,36 point pour un intervalle à
95 % de [−3,24 ; +4,07] — **ce n'est pas une victoire**, et la note de méthodologie le dit.
Le modèle gagne sa place au-delà du rang 500 (2,0 % contre 9,5 %, parce que la courbe
extrapole vers des rangs qu'aucune édition n'a publiés) et sur les 84 lignes sur 290 où aucune
édition comparable n'existe et où les références n'ont rien à dire.

Les fourchettes sont un peu larges : 86 % de couverture réelle pour 80 % annoncés.

---

## Lancer l'app

Python 3.10 ou plus. Aucune dépendance en dehors de Flask.

```bash
pip install -r requirements.txt
python app.py                 # ouvre http://127.0.0.1:5000
```

Windows : double-clic sur `run_windows.bat`.

Les classements en direct viennent de l'API [Cito](https://citoapi.com) — une clé gratuite
donne 500 requêtes par mois. L'app la demande au premier lancement et la garde dans
`cito_key.txt`, ignoré par git. Tout sauf l'import en direct fonctionne sans clé : le modèle
s'entraîne sur les seuils qu'on saisit soi-même.

L'interface est en anglais par défaut, avec un bouton FR dans l'en-tête
([`i18n.py`](i18n.py) contient les traductions).

### La couche d'analyse

```bash
pip install -r analysis/requirements.txt
python -m analysis.fit           # ré-estime a et b, avec intervalles
python -m analysis.diagnostics   # résidus, corrélation intra-tournoi, hétéroscédasticité
python -m analysis.validate      # un tournoi retiré à la fois, contre deux références
python -m analysis.anchor        # pourquoi le niveau se lit au rang 20
python -m analysis.figures       # régénère les figures
```

`analysis/` utilise numpy, pandas, scipy et matplotlib. L'app, non : rien sous `analysis/`
n'est importé par `app.py` ni par le modèle, donc l'app tourne toujours sur un Python nu. La
flèche ne va que dans un sens.

---

## Organisation

| | |
|---|---|
| `app.py` | routes Flask, et la validation d'entrée que traverse chaque écriture |
| `calibration.py` | le modèle : ajustement de la courbe, cascade d'ancres, rapprochement |
| `predict.py` | prédictions pendant une session — rythme, extrapolation, ma course |
| `scoring_infer.py` | retrouve le barème d'un tournoi à partir des totaux, par moindres carrés |
| `db.py` | schéma SQLite, migrations, toutes les requêtes |
| `cito.py` | client de l'API de classements |
| `i18n.py` | chaînes sources anglaises, traductions françaises |
| `analysis/` | numpy/pandas/scipy : ré-estimation, diagnostics, validation croisée, figures |
| `docs/` | [note de méthodologie](docs/methodology.md), [manuel](docs/manual.fr.md) |

Les vérifications, toutes exécutables :

```bash
python backtest.py       # erreur en avançant dans le temps, sur les tournois suivis
python check_forms.py    # chaque champ de formulaire survit à l'aller-retour vers la base
python fuzz_api.py       # 2 856 requêtes malformées, 0 erreur serveur
python selfcheck.py      # tout ce qui précède plus les deux langues, d'un coup
python cleanup.py --list # ce que le dossier contient et que plus aucun code n'atteint
```

`check_forms.py` existe pour une raison qui mérite d'être rappelée : un champ de formulaire a
été perdu un jour entre le navigateur et la base, et une après-midi de résultats saisis à la
main avec lui. Il envoie maintenant une valeur reconnaissable dans chaque champ de chaque
formulaire et les relit tous.

---

## Ce que je corrigerais ensuite

Dans l'ordre que justifient les chiffres :

1. `b` n'est pas identifié. 214 observations sur 225 se situent à `q < 0,25` ; en se limitant à
   cette plage, `b` passe de 0,46 à 0,29. C'est une valeur de travail, pas une mesure.
2. `calibration.fit_curve` normalise par le seuil *observé* au rang 20 au lieu de traiter le
   niveau comme un paramètre libre, ce qui atténue `a` de 8 % et `b` de 18 %.
3. Trois seuils au rang 50 ressemblent à des pages de classement tronquées lues comme des
   résultats. Ils portent toute la queue gauche — sans eux, l'erreur de forme est de 4 %, pas
   de 6 %.
4. Les modèles en direct ne sont pas vraiment testés : deux tournois seulement ont été suivis
   minute par minute.

---

Licence MIT. Fortnite est une marque d'Epic Games ; ce projet n'a aucun lien avec eux et
n'utilise aucune ressource du jeu.
