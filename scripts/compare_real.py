"""Rejeu du mock contre une VRAIE propriété GA4 — le purgatoire de UNVERIFIED.

`docs/UNVERIFIED-FIELDS.md` nomme ce script dans ses `review_triggers` : c'est
le seul instrument qui transforme « plausible » en « attesté ». Chaque cas est
envoyé DEUX fois — au service réel (analyticsdata.googleapis.com) et au mock
en processus (`TestClient`) — puis les deux réponses sont réduites au même
SQUELETTE et comparées.

Ce qui est comparé, et ce qui ne l'est pas :
  • comparé — le statut HTTP, la PRÉSENCE des clés (toute la règle proto3 :
    répétés vides absents, int32 à zéro absents), le TYPE JSON des feuilles
    (chaîne vs nombre : le piège n°1 de cette API), les valeurs énumérées
    (`TYPE_INTEGER`, `RESERVED_TOTAL`, `kind`, `status`) et le wording des
    erreurs ;
  • PAS comparé — les données. La propriété réelle n'est pas le monde de
    Boréal Conseil ; exiger les mêmes chiffres n'aurait aucun sens.

Usage :

    GA_REAL_SA=/chemin/sa.json GA_REAL_PROPERTY=<id de propriete> \\
        uv run python scripts/compare_real.py [--cas id …] [--sortie rapport.json]

Le compte de service n'a besoin que de `analytics.readonly` sur la propriété,
et l'API Data doit être ACTIVÉE sur le projet du compte (sinon 403
SERVICE_DISABLED, et le script le dit franchement).

La crypto reste en stdlib, comme le runtime : la clé privée réelle est lue par
un décodeur DER minimal (PKCS#8 → RSAPrivateKey) puis signée par
`ga_mock.rsa_min`. Aucune dépendance n'entre dans le projet pour un script de
vérification.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "src"))

from ga_mock.rsa_min import b64url, sign  # noqa: E402

HOTE_DATA = "https://analyticsdata.googleapis.com"
HOTE_TOKEN = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"

# Identifiant sur lequel le compte de service n'a AUCUN droit : sert le cas
# « autre propriété ». Volontairement hors de toute plage plausible.
PROPRIETE_ETRANGERE = "1"


# ── Lecture de la clé privée réelle (DER minimal, stdlib) ────────────────────


def _der_lire(donnees: bytes, position: int) -> tuple[int, bytes, int]:
    """Retourne (tag, contenu, position suivante) d'UN élément DER.

    Suffisant pour PKCS#8 : on ne rencontre que SEQUENCE, INTEGER et
    OCTET STRING, tous en encodage défini. Pas un parseur ASN.1 général.
    """
    tag = donnees[position]
    longueur = donnees[position + 1]
    position += 2
    if longueur & 0x80:
        octets = longueur & 0x7F
        longueur = int.from_bytes(donnees[position : position + octets], "big")
        position += octets
    return tag, donnees[position : position + longueur], position + longueur


def _der_entiers(sequence: bytes, combien: int) -> list[int]:
    valeurs: list[int] = []
    position = 0
    while len(valeurs) < combien:
        tag, contenu, position = _der_lire(sequence, position)
        if tag != 0x02:
            raise ValueError(f"INTEGER attendu, tag 0x{tag:02x}")
        valeurs.append(int.from_bytes(contenu, "big"))
    return valeurs


def cle_privee(pem: str) -> tuple[int, int, int]:
    """PEM PKCS#8 non chiffré → (n, e, d).

    PrivateKeyInfo ::= SEQUENCE { version, algorithme, privateKey OCTET STRING }
    où l'OCTET STRING contient RSAPrivateKey ::= SEQUENCE { version, n, e, d, … }.
    """
    corps = "".join(ligne for ligne in pem.splitlines() if ligne and not ligne.startswith("-----"))
    donnees = base64.b64decode(corps)
    _, info, _ = _der_lire(donnees, 0)
    position = 0
    _, _, position = _der_lire(info, position)  # version
    _, _, position = _der_lire(info, position)  # AlgorithmIdentifier
    tag, enveloppe, _ = _der_lire(info, position)
    if tag != 0x04:
        raise ValueError("OCTET STRING attendu pour privateKey")
    _, rsa, _ = _der_lire(enveloppe, 0)
    _, n, e, d = _der_entiers(rsa, 4)
    return n, e, d


# ── Côté service réel ────────────────────────────────────────────────────────


class Reel:
    """Client HTTP du service réel : signe une assertion, échange un bearer,
    appelle. Rien de plus — pas de retry, pas de cache : un rejeu doit être un
    rejeu, pas une simulation de client résilient."""

    def __init__(self, chemin_sa: Path) -> None:
        self.sa = json.loads(chemin_sa.read_text())
        self.n, self.e, self.d = cle_privee(self.sa["private_key"])
        self._jeton = ""
        self._echeance = 0.0

    def _assertion(self) -> str:
        maintenant = int(time.time())
        entete = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        charge = b64url(
            json.dumps(
                {
                    "iss": self.sa["client_email"],
                    "scope": SCOPE,
                    "aud": self.sa.get("token_uri", HOTE_TOKEN),
                    "iat": maintenant,
                    "exp": maintenant + 3600,
                }
            ).encode()
        )
        signature = b64url(sign(f"{entete}.{charge}".encode(), self.n, self.d))
        return f"{entete}.{charge}.{signature}"

    def bearer(self) -> str:
        if self._jeton and time.time() < self._echeance:
            return self._jeton
        donnees = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self._assertion(),
            }
        ).encode()
        statut, _, texte = _http(
            self.sa.get("token_uri", HOTE_TOKEN),
            "POST",
            donnees,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        if statut != 200:
            raise SystemExit(f"échange de jeton refusé ({statut}) : {texte}")
        charge = json.loads(texte)
        self._jeton = str(charge["access_token"])
        self._echeance = time.time() + int(charge.get("expires_in", 3600)) - 120
        return self._jeton

    def appeler(self, cas: Cas, propriete: str) -> Reponse:
        entetes = {"Content-Type": "application/json"}
        if cas.auth == "valide":
            entetes["Authorization"] = f"Bearer {self.bearer()}"
        elif cas.auth == "invalide":
            entetes["Authorization"] = "Bearer ya29.completement.faux"
        corps = cas.charge_utile()
        statut, entetes_reponse, texte = _http(
            HOTE_DATA + cas.chemin.replace("{p}", propriete), cas.methode, corps, entetes
        )
        return Reponse.depuis(statut, entetes_reponse, texte)


def _http(
    url: str, methode: str, corps: bytes | None, entetes: dict[str, str]
) -> tuple[int, dict[str, str], str]:
    requete = urllib.request.Request(url, data=corps, method=methode, headers=entetes)
    try:
        with urllib.request.urlopen(requete) as reponse:
            return reponse.status, dict(reponse.headers), reponse.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


# ── Côté mock ────────────────────────────────────────────────────────────────


def client_mock() -> Any:
    from fastapi.testclient import TestClient

    import ga_mock

    ga_mock.state.reset()
    return TestClient(ga_mock.app), ga_mock


def appeler_mock(client: Any, module: Any, cas: Cas) -> Reponse:
    entetes = {"Content-Type": "application/json"}
    if cas.auth == "valide":
        jeton = client.post(
            "/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": module.build_assertion(),
            },
        ).json()["access_token"]
        entetes["Authorization"] = f"Bearer {jeton}"
    elif cas.auth == "invalide":
        entetes["Authorization"] = "Bearer ya29.completement.faux"
    chemin = cas.chemin.replace("{p}", module.settings.property_id)
    reponse = client.request(cas.methode, chemin, headers=entetes, content=cas.charge_utile())
    return Reponse.depuis(reponse.status_code, dict(reponse.headers), reponse.text)


# ── Squelette : la forme, débarrassée des données ────────────────────────────


# Une chaîne est conservée TELLE QUELLE si elle ressemble à une énumération ou
# à un marqueur de protocole ; sinon elle devient "str". C'est ce qui permet de
# comparer `TYPE_INTEGER`, `RESERVED_TOTAL` ou `analyticsData#runReport` sans
# comparer un nom de ville.
def _chaine(valeur: str) -> str:
    if valeur.startswith("analyticsData#"):
        return valeur
    enum = valeur.replace("_", "")
    if enum.isalnum() and any(c.isalpha() for c in enum) and valeur.upper() == valeur:
        return valeur
    return "str"


def _liste(valeurs: list[Any]) -> list[Any]:
    """Réduction d'une liste à l'UNION de ses formes : le nombre de lignes
    diffère forcément entre deux mondes, la forme non."""
    formes: list[Any] = []
    for element in valeurs:
        forme = squelette(element)
        if forme not in formes:
            formes.append(forme)
    return formes


_SCALAIRES: tuple[tuple[type, str], ...] = ((bool, "bool"), (int, "int"), (float, "float"))


def squelette(valeur: Any) -> Any:
    if isinstance(valeur, dict):
        return {cle: squelette(valeur[cle]) for cle in sorted(valeur)}
    if isinstance(valeur, list):
        return _liste(valeur)
    if isinstance(valeur, str):
        return _chaine(valeur)
    for type_python, nom in _SCALAIRES:
        if isinstance(valeur, type_python):
            return nom
    return "null"


class Reponse:
    __slots__ = ("corps", "entetes", "statut", "texte")

    def __init__(self, statut: int, entetes: dict[str, str], corps: Any, texte: str) -> None:
        self.statut = statut
        self.entetes = entetes
        self.corps = corps
        self.texte = texte

    @classmethod
    def depuis(cls, statut: int, entetes: dict[str, str], texte: str) -> Reponse:
        try:
            corps = json.loads(texte)
        except ValueError:
            corps = texte
        return cls(statut, {k.lower(): v for k, v in entetes.items()}, corps, texte)

    @property
    def message_erreur(self) -> str:
        if isinstance(self.corps, dict) and isinstance(self.corps.get("error"), dict):
            return str(self.corps["error"].get("message", ""))
        return ""

    @property
    def statut_erreur(self) -> str:
        if isinstance(self.corps, dict) and isinstance(self.corps.get("error"), dict):
            return str(self.corps["error"].get("status", ""))
        return ""


# ── Les cas ──────────────────────────────────────────────────────────────────


class Cas:
    """Un cas de rejeu. `chemin` porte `{p}`, remplacé par la propriété de
    chaque côté — le mock ne connaît pas l'identifiant réel et réciproquement."""

    def __init__(
        self,
        identifiant: str,
        chemin: str,
        corps: Any = None,
        *,
        methode: str = "POST",
        auth: str = "valide",
        brut: str | None = None,
        note: str = "",
        sous_ensemble: bool = False,
    ) -> None:
        self.identifiant = identifiant
        self.chemin = chemin
        self.corps = corps
        self.methode = methode
        self.auth = auth
        self.brut = brut
        self.note = note
        # Le mock sert un catalogue PLUS PETIT que la vraie propriété : sur
        # `metadata`, une forme présente côté réel et absente côté mock est un
        # périmètre assumé, pas une divergence. L'inverse — une forme que le
        # mock invente — reste un écart.
        self.sous_ensemble = sous_ensemble

    def charge_utile(self) -> bytes | None:
        if self.brut is not None:
            return self.brut.encode()
        if self.corps is None:
            return None
        return json.dumps(self.corps).encode()


RAPPORT = "/v1beta/properties/{p}:runReport"
LOT = "/v1beta/properties/{p}:batchRunReports"
META = "/v1beta/properties/{p}/metadata"

# Fenêtre RELATIVE partout : le monde du mock est ancré en juillet 2026, la
# propriété réelle vit à l'heure réelle — seules les dates relatives donnent
# des lignes des DEUX côtés.
PLAGE = [{"startDate": "30daysAgo", "endDate": "yesterday"}]
VIDE = [{"startDate": "2015-01-01", "endDate": "2015-01-07"}]


def _base(**surcharges: Any) -> dict[str, Any]:
    corps: dict[str, Any] = {"dateRanges": list(PLAGE), "metrics": [{"name": "sessions"}]}
    corps.update(surcharges)
    return corps


CAS: list[Cas] = [
    # ── Formes nominales ────────────────────────────────────────────────────
    Cas("nu", RAPPORT, _base(), note="1 métrique, 0 dimension : ligne sans dimensionValues"),
    Cas(
        "dimension_date",
        RAPPORT,
        _base(
            dimensions=[{"name": "date"}],
            metrics=[{"name": "sessions"}, {"name": "totalUsers"}],
        ),
    ),
    Cas("vide", RAPPORT, _base(dateRanges=list(VIDE)), note="omission de rows/rowCount"),
    Cas(
        "metriques_flottantes",
        RAPPORT,
        _base(
            dimensions=[{"name": "date"}],
            metrics=[
                {"name": "engagementRate"},
                {"name": "bounceRate"},
                {"name": "averageSessionDuration"},
                {"name": "screenPageViewsPerSession"},
            ],
        ),
        note="sérialisation des flottants et types d'en-tête",
    ),
    Cas(
        "types_metriques",
        RAPPORT,
        _base(
            metrics=[
                {"name": "sessions"},
                {"name": "keyEvents"},
                {"name": "userEngagementDuration"},
                {"name": "sessionKeyEventRate"},
            ]
        ),
        note="TYPE_INTEGER / TYPE_FLOAT / TYPE_SECONDS attendus",
    ),
    Cas("limit_nombre", RAPPORT, _base(dimensions=[{"name": "date"}], limit=3)),
    Cas("limit_chaine", RAPPORT, _base(dimensions=[{"name": "date"}], limit="3")),
    Cas("limit_zero", RAPPORT, _base(dimensions=[{"name": "date"}], limit=0)),
    Cas("limit_enorme", RAPPORT, _base(dimensions=[{"name": "date"}], limit=300000)),
    Cas("limit_negative", RAPPORT, _base(dimensions=[{"name": "date"}], limit=-1)),
    Cas("offset", RAPPORT, _base(dimensions=[{"name": "date"}], limit=2, offset="3")),
    Cas(
        "deux_plages",
        RAPPORT,
        _base(
            dateRanges=[
                {"startDate": "14daysAgo", "endDate": "8daysAgo"},
                {"startDate": "7daysAgo", "endDate": "yesterday"},
            ],
            dimensions=[{"name": "date"}],
        ),
        note="dimension implicite dateRange : présence ET position",
    ),
    Cas(
        "plages_nommees",
        RAPPORT,
        _base(
            dateRanges=[
                {"startDate": "14daysAgo", "endDate": "8daysAgo", "name": "avant"},
                {"startDate": "7daysAgo", "endDate": "yesterday", "name": "apres"},
            ],
            dimensions=[{"name": "date"}],
        ),
    ),
    Cas(
        "agregations",
        RAPPORT,
        _base(
            dimensions=[{"name": "date"}],
            metricAggregations=["TOTAL", "MAXIMUM", "MINIMUM"],
        ),
        note="marqueurs RESERVED_* et forme des lignes d'agrégation",
    ),
    Cas(
        "agregation_count",
        RAPPORT,
        _base(dimensions=[{"name": "date"}], metricAggregations=["COUNT"]),
        note="COUNT est dans l'enum du discovery — le mock le refuse",
    ),
    Cas(
        "quota",
        RAPPORT,
        _base(returnPropertyQuota=True),
        note="liste EXACTE des seaux de PropertyQuota",
    ),
    Cas(
        "lignes_vides",
        RAPPORT,
        _base(dimensions=[{"name": "date"}], dateRanges=list(VIDE), keepEmptyRows=True),
    ),
    Cas(
        "tri_metrique",
        RAPPORT,
        _base(
            dimensions=[{"name": "date"}],
            orderBys=[{"metric": {"metricName": "sessions"}, "desc": True}],
        ),
    ),
    Cas(
        "tri_dimension",
        RAPPORT,
        _base(
            dimensions=[{"name": "date"}],
            orderBys=[{"dimension": {"dimensionName": "date", "orderType": "ALPHANUMERIC"}}],
        ),
    ),
    Cas(
        "filtre_chaine",
        RAPPORT,
        _base(
            dimensions=[{"name": "deviceCategory"}],
            dimensionFilter={
                "filter": {
                    "fieldName": "deviceCategory",
                    "stringFilter": {"matchType": "EXACT", "value": "desktop"},
                }
            },
        ),
    ),
    Cas(
        "filtre_regexp",
        RAPPORT,
        _base(
            dimensions=[{"name": "pagePath"}],
            dimensionFilter={
                "filter": {
                    "fieldName": "pagePath",
                    "stringFilter": {"matchType": "PARTIAL_REGEXP", "value": "^/"},
                }
            },
        ),
    ),
    Cas(
        "filtre_vide",
        RAPPORT,
        _base(
            dimensions=[{"name": "sessionCampaignName"}],
            dimensionFilter={
                "notExpression": {"filter": {"fieldName": "sessionCampaignName", "emptyFilter": {}}}
            },
        ),
        note="emptyFilter existe dans le discovery — absent du mock",
    ),
    Cas(
        "filtre_metrique",
        RAPPORT,
        _base(
            dimensions=[{"name": "date"}],
            metricFilter={
                "filter": {
                    "fieldName": "sessions",
                    "numericFilter": {
                        "operation": "GREATER_THAN",
                        "value": {"int64Value": "1"},
                    },
                }
            },
        ),
    ),
    Cas(
        "filtre_champ_non_demande",
        RAPPORT,
        _base(
            dimensions=[{"name": "date"}],
            dimensionFilter={
                "filter": {
                    "fieldName": "country",
                    "stringFilter": {"value": "France"},
                }
            },
        ),
        note="filtrer sur une dimension non demandée : le mock refuse en 400",
    ),
    Cas(
        "dates_relatives",
        RAPPORT,
        _base(
            dateRanges=[{"startDate": "today", "endDate": "today"}],
            dimensions=[{"name": "date"}],
        ),
    ),
    Cas(
        "metadata",
        META,
        None,
        methode="GET",
        sous_ensemble=True,
        note="le mock sert 20 dimensions et 15 métriques, pas tout le catalogue",
    ),
    Cas(
        "metadata_zero",
        "/v1beta/properties/0/metadata",
        None,
        methode="GET",
        sous_ensemble=True,
        note="idem — properties/0 décrit le schéma commun",
    ),
    Cas(
        "lot",
        LOT,
        {"requests": [_base(), _base(dimensions=[{"name": "date"}], limit=2)]},
    ),
    Cas("lot_six", LOT, {"requests": [_base()] * 6}, note="borne des 5 sous-rapports"),
    Cas("lot_vide", LOT, {"requests": []}),
    # ── Champs de requête hors périmètre du mock ────────────────────────────
    Cas(
        "devise",
        RAPPORT,
        _base(currencyCode="USD"),
        note="champ réel du RunReportRequest, ignoré par le mock",
    ),
    Cas(
        "cohorte",
        RAPPORT,
        {
            "cohortSpec": {
                "cohorts": [
                    {
                        "name": "c0",
                        "dimension": "firstSessionDate",
                        "dateRange": {"startDate": "14daysAgo", "endDate": "8daysAgo"},
                    }
                ],
                "cohortsRange": {"granularity": "DAILY", "endOffset": 5},
            },
            "dimensions": [{"name": "cohort"}, {"name": "cohortNthDay"}],
            "metrics": [{"name": "cohortActiveUsers"}],
        },
        note="le mock refuse cohortSpec en 400 : divergence de PÉRIMÈTRE assumée",
    ),
    # ── Erreurs ─────────────────────────────────────────────────────────────
    Cas("sans_bearer", RAPPORT, _base(), auth="aucune", note="401 + WWW-Authenticate"),
    Cas("bearer_invalide", RAPPORT, _base(), auth="invalide"),
    Cas(
        "autre_propriete",
        f"/v1beta/properties/{PROPRIETE_ETRANGERE}:runReport",
        _base(),
        note="403 PERMISSION_DENIED",
    ),
    Cas("propriete_non_numerique", "/v1beta/properties/abc:runReport", _base()),
    Cas("dimension_inconnue", RAPPORT, _base(dimensions=[{"name": "pasUneDimension"}])),
    Cas("metrique_inconnue", RAPPORT, _base(metrics=[{"name": "pasUneMetrique"}])),
    Cas("sans_metrique", RAPPORT, {"dateRanges": list(PLAGE)}),
    Cas("sans_plage", RAPPORT, {"metrics": [{"name": "sessions"}]}),
    Cas(
        "plage_inversee",
        RAPPORT,
        _base(dateRanges=[{"startDate": "yesterday", "endDate": "30daysAgo"}]),
    ),
    Cas(
        "date_invalide",
        RAPPORT,
        _base(dateRanges=[{"startDate": "01/01/2026", "endDate": "today"}]),
    ),
    Cas("cinq_plages", RAPPORT, _base(dateRanges=list(PLAGE) * 5)),
    Cas(
        "dix_dimensions",
        RAPPORT,
        _base(
            dimensions=[
                {"name": n}
                for n in (
                    "date",
                    "week",
                    "month",
                    "yearMonth",
                    "dayOfWeek",
                    "country",
                    "region",
                    "city",
                    "browser",
                    "deviceCategory",
                )
            ]
        ),
    ),
    Cas(
        "onze_metriques",
        RAPPORT,
        _base(
            metrics=[
                {"name": n}
                for n in (
                    "sessions",
                    "totalUsers",
                    "activeUsers",
                    "newUsers",
                    "engagedSessions",
                    "engagementRate",
                    "bounceRate",
                    "averageSessionDuration",
                    "userEngagementDuration",
                    "screenPageViews",
                    "eventCount",
                )
            ]
        ),
    ),
    Cas("doublon_dimension", RAPPORT, _base(dimensions=[{"name": "date"}, {"name": "date"}])),
    Cas("champ_inconnu", RAPPORT, _base(pasUnChamp=1), note="clé JSON inconnue"),
    Cas("json_invalide", RAPPORT, None, brut="{ceci n'est pas du json"),
    Cas("corps_absent", RAPPORT, None, brut=""),
    Cas("route_inconnue", "/v1beta/properties/{p}:runNothing", _base()),
    Cas("mauvais_verbe", RAPPORT, None, methode="GET"),
    Cas(
        "tri_non_demande",
        RAPPORT,
        _base(
            dimensions=[{"name": "date"}],
            orderBys=[{"metric": {"metricName": "totalUsers"}}],
        ),
    ),
    Cas(
        "agregation_invalide",
        RAPPORT,
        _base(metricAggregations=["MOYENNE"]),
    ),
]


# ── Comparaison ──────────────────────────────────────────────────────────────


def _differences_dict(attendu: dict, obtenu: dict, chemin: str) -> list[str]:
    ecarts: list[str] = []
    for cle in sorted(set(attendu) | set(obtenu)):
        sous = f"{chemin}.{cle}" if chemin else cle
        if cle not in obtenu:
            ecarts.append(f"{sous} : absent du mock (réel = {json.dumps(attendu[cle])})")
        elif cle not in attendu:
            ecarts.append(f"{sous} : en trop dans le mock ({json.dumps(obtenu[cle])})")
        else:
            ecarts.extend(_differences(attendu[cle], obtenu[cle], sous))
    return ecarts


def _differences_liste(attendu: list, obtenu: list, chemin: str) -> list[str]:
    if not attendu and obtenu:
        return [f"{chemin} : réel vide, mock non vide"]
    if attendu and not obtenu:
        return [f"{chemin} : réel non vide, mock vide"]
    manquantes = [f for f in attendu if f not in obtenu]
    surnumeraires = [f for f in obtenu if f not in attendu]
    return [f"{chemin}[] : forme absente du mock — {json.dumps(f)}" for f in manquantes] + [
        f"{chemin}[] : forme en trop dans le mock — {json.dumps(f)}" for f in surnumeraires
    ]


def _differences(attendu: Any, obtenu: Any, chemin: str = "") -> list[str]:
    """Diff RÉCURSIF de deux squelettes. `attendu` = le réel, `obtenu` = le
    mock : le vocabulaire dit qui fait autorité."""
    if isinstance(attendu, dict) and isinstance(obtenu, dict):
        return _differences_dict(attendu, obtenu, chemin)
    if isinstance(attendu, list) and isinstance(obtenu, list):
        return _differences_liste(attendu, obtenu, chemin)
    if attendu != obtenu:
        return [f"{chemin} : réel={json.dumps(attendu)} mock={json.dumps(obtenu)}"]
    return []


MANQUE_COTE_MOCK = " : absent du mock"
FORME_MANQUANTE = "[] : forme absente du mock"


def comparer(cas: Cas, reel: Reponse, mock: Reponse) -> dict[str, Any]:
    ecarts: list[str] = []
    if reel.statut != mock.statut:
        ecarts.append(f"statut HTTP : réel={reel.statut} mock={mock.statut}")
    if reel.statut_erreur != mock.statut_erreur:
        ecarts.append(f"error.status : réel={reel.statut_erreur!r} mock={mock.statut_erreur!r}")
    bruts = _differences(squelette(reel.corps), squelette(mock.corps))
    perimetre: list[str] = []
    for ecart in bruts:
        if cas.sous_ensemble and (MANQUE_COTE_MOCK in ecart or FORME_MANQUANTE in ecart):
            perimetre.append(ecart)
        else:
            ecarts.append(ecart)
    if reel.statut == 401:
        entete_reel = reel.entetes.get("www-authenticate", "")
        entete_mock = mock.entetes.get("www-authenticate", "")
        if bool(entete_reel) != bool(entete_mock):
            ecarts.append("WWW-Authenticate : présence divergente")
    return {
        "cas": cas.identifiant,
        "note": cas.note,
        "statut_reel": reel.statut,
        "statut_mock": mock.statut,
        "message_reel": reel.message_erreur,
        "message_mock": mock.message_erreur,
        "www_authenticate_reel": reel.entetes.get("www-authenticate", ""),
        "www_authenticate_mock": mock.entetes.get("www-authenticate", ""),
        "ecarts": ecarts,
        "perimetre": perimetre,
        "squelette_reel": squelette(reel.corps),
        "squelette_mock": squelette(mock.corps),
    }


# ── Vocabulaire : les VALEURS que rendent les dimensions ─────────────────────

# La forme ne suffit pas. Un mock qui rend `mobile` là où GA rend `mobile` mais
# `Ile-de-France` là où GA rend `Île-de-France` produit du code consommateur
# qui casse en prod sur un `==`. Ces dimensions ont un vocabulaire BORNÉ : leurs
# valeurs réelles sont comparables telles quelles, sans comparer des chiffres.
VOCABULAIRE: tuple[str, ...] = (
    "date",
    "week",
    "month",
    "yearMonth",
    "dayOfWeek",
    "deviceCategory",
    "newVsReturning",
    "sessionDefaultChannelGroup",
    "sessionMedium",
    "sessionSource",
    "operatingSystem",
    "browser",
    "country",
    "region",
    "city",
)


def _valeurs(reponse: Reponse) -> list[str]:
    if not isinstance(reponse.corps, dict):
        return []
    return [ligne["dimensionValues"][0]["value"] for ligne in reponse.corps.get("rows", [])]


def _forme_valeur(valeur: str) -> str:
    """Signature typographique d'une valeur : c'est elle qui trahit un écart de
    casse ou d'accent, là où deux mondes n'ont de toute façon pas les mêmes
    villes."""
    marques = []
    if valeur != valeur.lower():
        marques.append("Maj")
    if any(ord(c) > 127 for c in valeur):
        marques.append("accents")
    if valeur.startswith("(") and valeur.endswith(")"):
        marques.append("(marqueur)")
    if valeur.isdigit():
        marques.append(f"{len(valeur)}chiffres")
    return "+".join(marques) or "minuscules-ascii"


def comparer_vocabulaire(reel: Reel, client: Any, module: Any, propriete: str) -> list[dict]:
    resultats = []
    for dimension in VOCABULAIRE:
        cas = Cas(
            f"vocabulaire:{dimension}",
            RAPPORT,
            _base(
                dateRanges=[{"startDate": "365daysAgo", "endDate": "yesterday"}],
                dimensions=[{"name": dimension}],
                limit=200,
            ),
        )
        valeurs_reelles = _valeurs(reel.appeler(cas, propriete))
        valeurs_mock = _valeurs(appeler_mock(client, module, cas))
        formes_reelles = {_forme_valeur(v) for v in valeurs_reelles}
        formes_mock = {_forme_valeur(v) for v in valeurs_mock}
        resultats.append(
            {
                "dimension": dimension,
                "reel": sorted(valeurs_reelles)[:40],
                "mock": sorted(valeurs_mock)[:40],
                "communes": sorted(set(valeurs_reelles) & set(valeurs_mock))[:40],
                "formes_reelles": sorted(formes_reelles),
                "formes_mock": sorted(formes_mock),
                "formes_divergentes": sorted(formes_reelles ^ formes_mock),
            }
        )
    return resultats


def main() -> int:
    analyseur = argparse.ArgumentParser(description=__doc__)
    analyseur.add_argument("--cas", nargs="*", help="n'exécuter que ces identifiants")
    analyseur.add_argument("--sortie", type=Path, help="écrit le rapport complet en JSON")
    analyseur.add_argument(
        "--details", action="store_true", help="affiche les squelettes des cas divergents"
    )
    analyseur.add_argument(
        "--vocabulaire",
        action="store_true",
        help="compare les VALEURS des dimensions à vocabulaire borné",
    )
    options = analyseur.parse_args()

    chemin_sa = os.environ.get("GA_REAL_SA")
    propriete = os.environ.get("GA_REAL_PROPERTY")
    if not chemin_sa or not propriete:
        print("GA_REAL_SA et GA_REAL_PROPERTY sont requis.", file=sys.stderr)
        return 2

    reel = Reel(Path(chemin_sa))
    client, module = client_mock()

    if options.vocabulaire:
        vocabulaire = comparer_vocabulaire(reel, client, module, propriete)
        for entree in vocabulaire:
            print(f"== {entree['dimension']}")
            print(f"   réel : {', '.join(entree['reel'][:20]) or '(aucune ligne)'}")
            print(f"   mock : {', '.join(entree['mock'][:20]) or '(aucune ligne)'}")
            print(f"   formes réel={entree['formes_reelles']} mock={entree['formes_mock']}")
            if entree["formes_divergentes"]:
                print(f"   ✗ formes divergentes : {entree['formes_divergentes']}")
        if options.sortie:
            options.sortie.write_text(json.dumps(vocabulaire, indent=2, ensure_ascii=False))
            print(f"rapport → {options.sortie}")
        return 0

    selection = [c for c in CAS if not options.cas or c.identifiant in options.cas]

    resultats = []
    for cas in selection:
        reponse_reelle = reel.appeler(cas, propriete)
        if reponse_reelle.statut == 403 and "SERVICE_DISABLED" in reponse_reelle.texte:
            print(
                "L'API Data n'est pas activée sur le projet du compte de service.\n"
                + reponse_reelle.message_erreur,
                file=sys.stderr,
            )
            return 3
        resultats.append(comparer(cas, reponse_reelle, appeler_mock(client, module, cas)))

    divergents = [r for r in resultats if r["ecarts"]]
    largeur = max(len(r["cas"]) for r in resultats)
    for resultat in resultats:
        marque = "✗" if resultat["ecarts"] else "✓"
        print(
            f"{marque} {resultat['cas']:<{largeur}}  "
            f"{resultat['statut_reel']}/{resultat['statut_mock']}"
            + (f"  {len(resultat['ecarts'])} écart(s)" if resultat["ecarts"] else "")
        )
        for ecart in resultat["ecarts"]:
            print(f"    · {ecart}")
        for hors in resultat.get("perimetre", []):
            print(f"    ~ hors périmètre assumé : {hors}")
        if options.details and resultat["ecarts"]:
            print(f"    réel : {json.dumps(resultat['squelette_reel'], ensure_ascii=False)}")
            print(f"    mock : {json.dumps(resultat['squelette_mock'], ensure_ascii=False)}")

    print(f"\n{len(resultats) - len(divergents)}/{len(resultats)} cas conformes")
    if options.sortie:
        options.sortie.write_text(json.dumps(resultats, indent=2, ensure_ascii=False))
        print(f"rapport → {options.sortie}")
    return 1 if divergents else 0


if __name__ == "__main__":
    raise SystemExit(main())
