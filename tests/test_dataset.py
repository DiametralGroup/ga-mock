"""Le monde : déterminisme, volumétrie, saisonnalité, campagnes, invariants."""

from datetime import date, timedelta

from ga_mock.dataset import build_day, planned_new

SEED = 42


def test_deterministe_a_l_octet_pres():
    d = date(2026, 6, 3)
    assert build_day(SEED, d) == build_day(SEED, d)
    assert build_day(7, d) != build_day(SEED, d)


def test_bornes_de_volumetrie():
    # première semaine de juin 2026 : ni férié, ni campagne (webinar-finops
    # démarre le 8) — le régime « ordinaire » du site.
    for jour in range(1, 6):
        n = len(build_day(SEED, date(2026, 6, jour)))
        assert 15 <= n <= 170


def test_semaine_tres_superieure_au_weekend():
    lundi = date(2026, 6, 1)
    ouvres = [len(build_day(SEED, lundi + timedelta(days=i))) for i in range(5)]
    weekend = [len(build_day(SEED, lundi + timedelta(days=i))) for i in (5, 6)]
    assert sum(ouvres) / len(ouvres) >= 3 * (sum(weekend) / len(weekend))


def test_campagne_presente_et_convertissante():
    d = date(2026, 2, 18)  # recrutement-cyber-2026 en régime de croisière
    en_campagne = [s for s in build_day(SEED, d) if s.campaign == "recrutement-cyber-2026"]
    assert en_campagne, "les extras de campagne doivent exister mi-février 2026"
    assert all(s.source == "linkedin.com" and s.medium == "cpc" for s in en_campagne)
    assert all(s.channel_group == "Paid Social" for s in en_campagne)

    candidatures = 0
    for j in range(14):
        for s in build_day(SEED, date(2026, 2, 9) + timedelta(days=j)):
            if s.campaign == "recrutement-cyber-2026":
                candidatures += dict(s.events).get("job_apply", 0)
    assert candidatures > 0


def test_invariants_d_un_jour():
    d = date(2026, 5, 20)
    sessions = build_day(SEED, d)
    nouveaux = [s for s in sessions if s.is_new]
    # la marge volumétrique garantit min(planned_new, organique) == planned_new
    assert len(nouveaux) == planned_new(SEED, d)
    assert all(s.is_new for s in sessions[: len(nouveaux)])
    assert len({s.session_id for s in sessions}) == len(sessions)
    for s in sessions:
        assert s.landing_page == s.pages[0]
        assert s.engagement_seconds <= s.duration_seconds
        cle = any(nom in ("generate_lead", "job_apply") for nom, _ in s.events)
        assert s.engaged == (s.engagement_seconds >= 10 or cle or len(s.pages) >= 2)
        assert s.user_id.split(".")[0] <= f"{d:%Y%m%d}"
        if s.is_new:
            assert s.user_id.startswith(f"{d:%Y%m%d}.")
        assert dict(s.events)["page_view"] == len(s.pages)


def test_ids_de_nouveaux_visiteurs_uniques_sur_30_jours():
    vus: set[str] = set()
    for j in range(30):
        for s in build_day(SEED, date(2026, 4, 1) + timedelta(days=j)):
            if s.is_new:
                assert s.user_id not in vus
                vus.add(s.user_id)
