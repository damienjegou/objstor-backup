#!/bin/bash
# Script d'exemple pour tester la fonction de restauration Swift

echo "====================================================================="
echo "  Script de test de restauration OpenStack Swift"
echo "====================================================================="
echo ""

# Charger la configuration
if [ -f /etc/swift_backup.conf ]; then
    source /etc/swift_backup.conf
    echo "✓ Configuration chargée depuis /etc/swift_backup.conf"
else
    echo "✗ Erreur : /etc/swift_backup.conf introuvable"
    echo "  Veuillez créer ce fichier avec vos credentials OpenStack"
    exit 1
fi

# Vérifier que les variables sont définies
required_vars=("OS_AUTH_URL" "OS_USERNAME" "OS_PASSWORD" "OS_PROJECT_NAME" 
               "SOURCE_CONTAINER" "BACKUP_CONTAINER")

missing_vars=()
for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        missing_vars+=("$var")
    fi
done

if [ ${#missing_vars[@]} -gt 0 ]; then
    echo "✗ Variables manquantes : ${missing_vars[*]}"
    exit 1
fi

echo "✓ Toutes les variables d'environnement sont définies"
echo ""

# Menu interactif
echo "Que souhaitez-vous faire ?"
echo ""
echo "1) Afficher les statistiques de la base de backup"
echo "2) Lister les points de restauration disponibles"
echo "3) Compter les fichiers à une date donnée"
echo "4) Simuler une restauration (dry-run)"
echo "5) Restaurer vers un conteneur de test"
echo "6) Restaurer vers le conteneur source (ATTENTION !)"
echo "7) Rechercher un fichier"
echo "0) Quitter"
echo ""
read -p "Votre choix : " choice

case $choice in
    1)
        echo ""
        echo "=== Téléchargement de la base de données ==="
        python3 -c "
from swiftclient.service import SwiftService
import os

options = {
    'auth_version': os.getenv('OS_AUTH_VERSION', '3'),
    'os_username': os.getenv('OS_USERNAME'),
    'os_password': os.getenv('OS_PASSWORD'),
    'os_project_name': os.getenv('OS_PROJECT_NAME'),
    'os_auth_url': os.getenv('OS_AUTH_URL'),
    'os_region_name': os.getenv('OS_REGION_NAME'),
}

swift = SwiftService(options=options)
result = swift.download(
    container=os.getenv('BACKUP_CONTAINER'),
    objects=['backup.db'],
    options={'out_directory': '.'}
)

for r in result:
    if r['success']:
        print('✓ Base de données téléchargée')
    else:
        print('✗ Erreur:', r.get('error', 'Inconnue'))
"
        echo ""
        python3 restore_explorer.py stats
        ;;
        
    2)
        echo ""
        python3 restore_explorer.py list-points
        ;;
        
    3)
        echo ""
        read -p "Date (YYYY-MM-DD ou YYYY-MM-DD HH:MM:SS) : " date
        python3 restore_explorer.py count --date "$date"
        ;;
        
    4)
        echo ""
        read -p "Date de restauration (YYYY-MM-DD ou YYYY-MM-DD HH:MM:SS) : " date
        echo ""
        echo "=== Simulation de restauration (dry-run) ==="
        python3 swift_backup.py restore --date "$date" --dry-run
        ;;
        
    5)
        echo ""
        read -p "Date de restauration (YYYY-MM-DD ou YYYY-MM-DD HH:MM:SS) : " date
        read -p "Nom du conteneur de test (sera créé si inexistant) : " test_container
        echo ""
        echo "=== Restauration vers $test_container ==="
        echo "⚠️  Cette opération va copier les fichiers du backup vers $test_container"
        read -p "Continuer ? (oui/non) : " confirm
        
        if [ "$confirm" = "oui" ]; then
            python3 swift_backup.py restore --date "$date" --target-container "$test_container"
        else
            echo "Opération annulée"
        fi
        ;;
        
    6)
        echo ""
        echo "⚠️  ATTENTION : RESTAURATION VERS LE CONTENEUR SOURCE ⚠️"
        echo ""
        echo "Cette opération va :"
        echo "  - Écraser les fichiers existants dans $SOURCE_CONTAINER"
        echo "  - Restaurer l'état du conteneur à la date demandée"
        echo "  - NE PAS supprimer les fichiers créés après cette date"
        echo ""
        read -p "Date de restauration (YYYY-MM-DD ou YYYY-MM-DD HH:MM:SS) : " date
        echo ""
        echo "Voulez-vous d'abord faire une simulation ?"
        read -p "Faire un dry-run ? (oui/non) : " dryrun_confirm
        
        if [ "$dryrun_confirm" = "oui" ]; then
            python3 swift_backup.py restore --date "$date" --dry-run
            echo ""
            read -p "Continuer avec la restauration réelle ? (oui/non) : " final_confirm
        else
            read -p "Êtes-vous ABSOLUMENT sûr de vouloir restaurer ? (oui/non) : " final_confirm
        fi
        
        if [ "$final_confirm" = "oui" ]; then
            echo ""
            echo "=== Restauration en cours ==="
            python3 swift_backup.py restore --date "$date"
        else
            echo "Opération annulée"
        fi
        ;;
        
    7)
        echo ""
        read -p "Motif de recherche (ex: rapport, .pdf, 2026) : " pattern
        python3 restore_explorer.py search --pattern "$pattern"
        ;;
        
    0)
        echo "Au revoir !"
        exit 0
        ;;
        
    *)
        echo "Choix invalide"
        exit 1
        ;;
esac

echo ""
echo "====================================================================="
echo "  Opération terminée"
echo "====================================================================="
