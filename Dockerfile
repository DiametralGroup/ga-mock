# syntax=docker/dockerfile:1

# ── Étape 1 : construction de l'environnement ────────────────────────────────
# L'image de base vient de Docker Hub, PAS du proxy Harbor : ce Dockerfile doit
# construire partout (laptops hors VPN, GitHub Actions). Côté cluster, l'image
# publiée est tirée via harbor.build.graal.systems/ghcr-proxy/... pour
# satisfaire la policy Kyverno `only-harbor-images`.
FROM python:3.12-slim AS builder
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install --no-cache-dir uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# `--frozen` : on installe le lockfile, le build ne résout jamais.
# `--no-dev` : pytest et ruff n'ont rien à faire dans l'image expédiée.
# `--no-editable` : SANS cette option uv installe le projet en editable, un
#                   lien vers /build/src qui n'existe plus dans l'étape finale
#                   — échec au démarrage sur "No module named ga_mock".
RUN uv sync --frozen --no-dev --no-editable

# ── Étape 2 : image finale ───────────────────────────────────────────────────
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/opt/venv/bin:$PATH"
# uid/gid FIXES (65532) : les charts consommateurs posent runAsNonRoot +
# seccomp + drop ALL, et l'image tolère readOnlyRootFilesystem — aucune
# écriture au runtime (PYTHONDONTWRITEBYTECODE inclus).
RUN groupadd --gid 65532 mock && \
    useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin mock
COPY --from=builder /opt/venv /opt/venv
USER 65532:65532
EXPOSE 8000
# Le healthcheck vit DANS l'image : les consommateurs s'appuient sur
# `depends_on: condition: service_healthy` sans rien ajouter chez eux. Il lit
# GA_MOCK_PORT pour rester juste quand le bind est déplacé (sidecar Tekton).
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import os,urllib.request,sys; port=os.environ.get('GA_MOCK_PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://localhost:{port}/health').status==200 else 1)"
CMD ["python", "-m", "ga_mock"]
