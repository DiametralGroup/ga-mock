"""Génère la bi-clé RSA-2048 FACTICE committée dans src/ga_mock/keypair.py.

À lancer UNE fois, à la main, depuis la racine du repo :

    uv run --with cryptography python scripts/generate_keypair.py

La sortie est COMMITTÉE : la clé n'authentifie qu'un mock, sa publication est
le mécanisme même qui permet aux consommateurs (tests insights360, .env de
dev) de signer des assertions valides sans infrastructure de secrets. La
régénérer invalide les copies faites chez les consommateurs — ne le faire
qu'en le sachant.

`cryptography` n'est utilisé qu'ICI, pour la génération et la sérialisation
PEM PKCS#8, qui doit être irréprochable (les vrais clients la chargeront). Le
runtime du mock reste en stdlib pur : il ne lit que les entiers N/E/D.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
CIBLE = RACINE / "src" / "ga_mock" / "keypair.py"

MODELE = '''"""Bi-clé RSA-2048 FACTICE, committée en toute connaissance de cause.

Elle n\'authentifie qu\'un mock : sa publication est voulue, c\'est elle qui
permet aux consommateurs (tests insights360, .env de dev) de signer des
assertions valides sans infrastructure de secrets. Générée par
scripts/generate_keypair.py — la régénérer invalide les copies faites chez
les consommateurs.
"""

E = 65537

N = int(
{n_lignes}
    16,
)

D = int(
{d_lignes}
    16,
)

PRIVATE_KEY_ID = "{key_id}"

PEM_PRIVE = """{pem}"""
'''


def _lignes_hex(valeur: int) -> str:
    hexa = f"{valeur:x}"
    morceaux = [hexa[i : i + 84] for i in range(0, len(hexa), 84)]
    return "\n".join(f'    "{morceau}"' for morceau in morceaux)


def main() -> None:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    cle = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombres = cle.private_numbers()
    pem = cle.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    # Auto-contrôle : l'arithmétique stdlib du mock doit produire la MÊME
    # signature que cryptography (PKCS#1 v1.5 est déterministe), sinon la
    # bi-clé committée serait incohérente avec rsa_min.
    sys.path.insert(0, str(RACINE / "src"))
    from ga_mock.rsa_min import sign, verify

    message = b"controle ga-mock"
    attendu = cle.sign(message, padding.PKCS1v15(), hashes.SHA256())
    obtenu = sign(message, nombres.public_numbers.n, nombres.d)
    if attendu != obtenu:
        raise SystemExit("sign() stdlib ≠ cryptography — bi-clé NON écrite")
    if not verify(message, obtenu, nombres.public_numbers.n, nombres.public_numbers.e):
        raise SystemExit("verify() stdlib en échec — bi-clé NON écrite")

    CIBLE.write_text(
        MODELE.format(
            n_lignes=_lignes_hex(nombres.public_numbers.n) + ",",
            d_lignes=_lignes_hex(nombres.d) + ",",
            key_id=secrets.token_hex(20),
            pem=pem,
        )
    )
    print(f"✓ {CIBLE.relative_to(RACINE)} régénéré")


if __name__ == "__main__":
    main()
