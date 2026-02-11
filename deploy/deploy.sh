#!/bin/bash
set -e

# =============================================================================
# Deploy Script - Microservices Standard
# =============================================================================
# Este script despliega un microservicio a una VM en GCP usando Docker y
# Artifact Registry. Lee la configuración desde el .env en la raíz del proyecto.
#
# Arquitectura:
# - docker-compose.yml (local) → desarrollo
# - docker-compose.prod.yml (local) → se copia como docker-compose.yml en VM
# =============================================================================

# Colores para output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# -----------------------------------------------------------------------------
# Load Environment Variables
# -----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_ROOT/.env"
COMPOSE_PROD_FILE="$PROJECT_ROOT/docker-compose.prod.yml"

if [ ! -f "$ENV_FILE" ]; then
    log_error "No se encontró .env en $PROJECT_ROOT"
    exit 1
fi

if [ ! -f "$COMPOSE_PROD_FILE" ]; then
    log_error "No se encontró docker-compose.prod.yml en $PROJECT_ROOT"
    exit 1
fi

log_info "Cargando configuración desde $ENV_FILE"

# Cargar variables del .env
set -a
source "$ENV_FILE"
set +a

# -----------------------------------------------------------------------------
# Validate Required Variables
# -----------------------------------------------------------------------------

REQUIRED_VARS=(
    "GCP_PROJECT"
    "GCP_REGION"
    "ARTIFACT_REGISTRY_REPO"
    "VM_USER"
    "VM_HOST"
    "SSH_KEY"
    "VM_DEPLOY_PATH"
)

missing_vars=()
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var}" ]; then
        missing_vars+=("$var")
    fi
done

if [ ${#missing_vars[@]} -ne 0 ]; then
    log_error "Variables requeridas no configuradas:"
    for var in "${missing_vars[@]}"; do
        echo "  - $var"
    done
    exit 1
fi

# -----------------------------------------------------------------------------
# Derived Variables
# -----------------------------------------------------------------------------

# Normalizar nombre del proyecto a minúsculas (requisito de Docker)
PROJECT_NAME=$(basename "$PROJECT_ROOT" | tr '[:upper:]' '[:lower:]')

# Detectar nombre del servicio desde docker-compose.prod.yml
# Busca la primera línea que tenga un nombre de servicio (indentación + nombre + :)
SERVICE_NAME=$(grep -E '^\s{2}[a-zA-Z0-9_-]+:\s*$' "$COMPOSE_PROD_FILE" | head -1 | sed 's/://g' | xargs)

if [ -z "$SERVICE_NAME" ]; then
    log_error "No se pudo detectar el nombre del servicio en docker-compose.prod.yml"
    log_info "Asegúrate de que el archivo tiene la estructura correcta"
    exit 1
fi

log_info "Servicio detectado: $SERVICE_NAME"

# Nombre de la imagen que Docker Compose genera: {project}-{service}
COMPOSE_IMAGE_NAME="${PROJECT_NAME}-${SERVICE_NAME}"

# Nombre de la imagen en Artifact Registry
AR_IMAGE_NAME="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${ARTIFACT_REGISTRY_REPO}/${PROJECT_NAME}"

SSH_CMD="ssh -i $SSH_KEY ${VM_USER}@${VM_HOST}"
SCP_CMD="scp -i $SSH_KEY"

# -----------------------------------------------------------------------------
# Pre-flight Checks
# -----------------------------------------------------------------------------

log_info "Ejecutando validaciones..."

# Check gcloud auth
if ! gcloud auth print-access-token &>/dev/null; then
    log_error "No autenticado con gcloud. Ejecuta: gcloud auth login"
    exit 1
fi

# Check VM directory exists
if ! $SSH_CMD "test -d $VM_DEPLOY_PATH"; then
    log_error "El directorio $VM_DEPLOY_PATH no existe en la VM"
    log_info "Créalo manualmente: ssh -i $SSH_KEY ${VM_USER}@${VM_HOST} 'mkdir -p $VM_DEPLOY_PATH'"
    exit 1
fi

# Check VM .env exists
if ! $SSH_CMD "test -f $VM_DEPLOY_PATH/.env"; then
    log_error "No existe .env en $VM_DEPLOY_PATH en la VM"
    log_info "Crea el archivo con los secrets de producción"
    exit 1
fi

log_success "Validaciones completadas"

# -----------------------------------------------------------------------------
# Build Image
# -----------------------------------------------------------------------------

log_info "Construyendo imagen Docker con docker-compose.prod.yml..."
cd "$PROJECT_ROOT"

# Build usando docker-compose.prod.yml (incluye platform: linux/amd64)
docker compose -f docker-compose.prod.yml build

log_success "Imagen construida: ${COMPOSE_IMAGE_NAME}:latest"

# -----------------------------------------------------------------------------
# Tag and Push to Artifact Registry
# -----------------------------------------------------------------------------

log_info "Haciendo push a Artifact Registry..."

# Tag the image (Docker Compose nombra las imágenes como {project}-{service})
docker tag "${COMPOSE_IMAGE_NAME}:latest" "${AR_IMAGE_NAME}:latest"

# Push to AR
docker push "${AR_IMAGE_NAME}:latest"

log_success "Imagen subida a Artifact Registry"

# -----------------------------------------------------------------------------
# Copy Docker Compose File to VM
# -----------------------------------------------------------------------------

log_info "Copiando docker-compose.prod.yml a la VM como docker-compose.yml..."

# Copiar docker-compose.prod.yml como docker-compose.yml en la VM
$SCP_CMD "$COMPOSE_PROD_FILE" "${VM_USER}@${VM_HOST}:${VM_DEPLOY_PATH}/docker-compose.yml"

log_success "Archivo copiado"

# -----------------------------------------------------------------------------
# Deploy on VM
# -----------------------------------------------------------------------------

log_info "Desplegando en la VM..."

$SSH_CMD << EOF
    cd $VM_DEPLOY_PATH

    # Configure docker for Artifact Registry (if not already)
    gcloud auth configure-docker ${GCP_REGION}-docker.pkg.dev --quiet 2>/dev/null || true

    # Pull latest image
    docker pull ${AR_IMAGE_NAME}:latest

    # Export IMAGE_NAME for docker-compose
    export IMAGE_NAME="${AR_IMAGE_NAME}"

    # Stop existing container and start new one
    docker compose down --remove-orphans || true
    docker compose up -d

    # Cleanup old images
    docker image prune -f
EOF

log_success "Deploy completado en VM"

# -----------------------------------------------------------------------------
# Cleanup Artifact Registry (keep only latest)
# -----------------------------------------------------------------------------

log_info "Limpiando imágenes antiguas en Artifact Registry..."

# List all digests except the one tagged as latest
LATEST_DIGEST=$(gcloud artifacts docker images describe "${AR_IMAGE_NAME}:latest" --format='value(image_summary.digest)' 2>/dev/null || echo "")

if [ -n "$LATEST_DIGEST" ]; then
    # Get all digests and delete those that aren't latest
    gcloud artifacts docker images list "${AR_IMAGE_NAME}" --format='value(version)' 2>/dev/null | while read -r digest; do
        if [ "$digest" != "$LATEST_DIGEST" ] && [ -n "$digest" ]; then
            log_info "Eliminando imagen antigua: $digest"
            gcloud artifacts docker images delete "${AR_IMAGE_NAME}@${digest}" --quiet --delete-tags 2>/dev/null || true
        fi
    done
fi

log_success "Limpieza completada"

# -----------------------------------------------------------------------------
# Show Logs
# -----------------------------------------------------------------------------

log_info "Logs del contenedor:"
echo ""

$SSH_CMD "cd $VM_DEPLOY_PATH && docker compose logs --tail=50"

echo ""
log_success "Deploy finalizado exitosamente 🚀"
