#!/usr/bin/env python3
"""
Utilitaire pour explorer les points de restauration disponibles
dans la base de données de backup Swift
"""

import sqlite3
import sys
import argparse
from datetime import datetime
from tabulate import tabulate


def connect_db(db_file="backup.db"):
    """Connecte à la base de données SQLite"""
    try:
        conn = sqlite3.connect(db_file)
        return conn
    except sqlite3.Error as e:
        print(f"Erreur de connexion à la base : {e}")
        sys.exit(1)


def list_restore_points(conn):
    """Liste tous les points de restauration uniques disponibles"""
    cursor = conn.cursor()
    
    # Récupérer toutes les dates de first_backup et last_seen uniques
    cursor.execute("""
        SELECT DISTINCT first_backup FROM copied_objects
        UNION
        SELECT DISTINCT last_seen FROM copied_objects
        ORDER BY first_backup
    """)
    
    timestamps = [row[0] for row in cursor.fetchall()]
    
    if not timestamps:
        print("Aucun point de restauration trouvé dans la base de données.")
        return
    
    print("\n=== Points de restauration disponibles ===\n")
    print(f"Premier backup : {datetime.fromtimestamp(min(timestamps)).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Dernier backup : {datetime.fromtimestamp(max(timestamps)).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"\nNombre total de timestamps : {len(timestamps)}")
    print("\n⚠️  Vous pouvez restaurer à n'importe quelle date entre ces deux points.\n")


def count_files_at_date(conn, restore_date):
    """Compte le nombre de fichiers disponibles à une date donnée"""
    cursor = conn.cursor()
    
    # Parser la date
    try:
        if ' ' in restore_date:
            dt = datetime.strptime(restore_date, '%Y-%m-%d %H:%M:%S')
        else:
            dt = datetime.strptime(restore_date, '%Y-%m-%d')
    except ValueError as e:
        print(f"Format de date invalide : {e}")
        print("Formats acceptés : 'YYYY-MM-DD' ou 'YYYY-MM-DD HH:MM:SS'")
        sys.exit(1)
    
    timestamp = int(dt.timestamp())
    
    cursor.execute("""
        SELECT COUNT(*) FROM copied_objects
        WHERE first_backup <= ? AND last_seen >= ?
    """, (timestamp, timestamp))
    
    count = cursor.fetchone()[0]
    
    print(f"\n=== Fichiers disponibles au {dt.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    print(f"Nombre de fichiers : {count}")
    
    if count > 0:
        # Statistiques supplémentaires
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(size) as total_size,
                AVG(size) as avg_size,
                MIN(size) as min_size,
                MAX(size) as max_size
            FROM copied_objects
            WHERE first_backup <= ? AND last_seen >= ?
        """, (timestamp, timestamp))
        
        stats = cursor.fetchone()
        total_size_gb = stats[1] / (1024**3) if stats[1] else 0
        avg_size_mb = stats[2] / (1024**2) if stats[2] else 0
        
        print(f"Taille totale   : {total_size_gb:.2f} GB")
        print(f"Taille moyenne  : {avg_size_mb:.2f} MB")
        print(f"Plus petit      : {stats[3]:,} bytes" if stats[3] else "Plus petit      : N/A")
        print(f"Plus grand      : {stats[4]:,} bytes" if stats[4] else "Plus grand      : N/A")
    
    return count


def list_files_at_date(conn, restore_date, limit=50):
    """Liste les fichiers disponibles à une date donnée"""
    cursor = conn.cursor()
    
    # Parser la date
    try:
        if ' ' in restore_date:
            dt = datetime.strptime(restore_date, '%Y-%m-%d %H:%M:%S')
        else:
            dt = datetime.strptime(restore_date, '%Y-%m-%d')
    except ValueError as e:
        print(f"Format de date invalide : {e}")
        print("Formats acceptés : 'YYYY-MM-DD' ou 'YYYY-MM-DD HH:MM:SS'")
        sys.exit(1)
    
    timestamp = int(dt.timestamp())
    
    cursor.execute("""
        SELECT 
            name,
            size,
            etag,
            first_backup,
            last_seen
        FROM copied_objects
        WHERE first_backup <= ? AND last_seen >= ?
        ORDER BY size DESC
        LIMIT ?
    """, (timestamp, timestamp, limit))
    
    files = cursor.fetchall()
    
    if not files:
        print(f"\nAucun fichier trouvé au {dt.strftime('%Y-%m-%d %H:%M:%S')}")
        return
    
    print(f"\n=== Top {limit} fichiers (par taille) au {dt.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    
    table_data = []
    for name, size, etag, first_backup, last_seen in files:
        size_mb = size / (1024**2)
        first_dt = datetime.fromtimestamp(first_backup).strftime('%Y-%m-%d')
        last_dt = datetime.fromtimestamp(last_seen).strftime('%Y-%m-%d')
        
        table_data.append([
            name[:50] + ('...' if len(name) > 50 else ''),
            f"{size_mb:.2f} MB",
            etag[:8] + '...',
            first_dt,
            last_dt
        ])
    
    headers = ['Nom', 'Taille', 'ETag', '1er backup', 'Dernière vue']
    print(tabulate(table_data, headers=headers, tablefmt='grid'))
    
    # Compter le total
    cursor.execute("""
        SELECT COUNT(*) FROM copied_objects
        WHERE first_backup <= ? AND last_seen >= ?
    """, (timestamp, timestamp))
    
    total = cursor.fetchone()[0]
    if total > limit:
        print(f"\n... et {total - limit} autres fichiers")


def search_files(conn, pattern):
    """Recherche des fichiers par nom"""
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            name,
            size,
            first_backup,
            last_seen
        FROM copied_objects
        WHERE name LIKE ?
        ORDER BY name
    """, (f'%{pattern}%',))
    
    files = cursor.fetchall()
    
    if not files:
        print(f"\nAucun fichier trouvé correspondant à '{pattern}'")
        return
    
    print(f"\n=== Fichiers correspondant à '{pattern}' ===\n")
    
    table_data = []
    for name, size, first_backup, last_seen in files:
        size_mb = size / (1024**2)
        first_dt = datetime.fromtimestamp(first_backup).strftime('%Y-%m-%d %H:%M:%S')
        last_dt = datetime.fromtimestamp(last_seen).strftime('%Y-%m-%d %H:%M:%S')
        
        table_data.append([
            name,
            f"{size_mb:.2f} MB",
            first_dt,
            last_dt
        ])
    
    headers = ['Nom', 'Taille', 'Premier backup', 'Dernière présence']
    print(tabulate(table_data, headers=headers, tablefmt='grid'))
    print(f"\nTotal : {len(files)} fichier(s) trouvé(s)")


def get_db_stats(conn):
    """Affiche des statistiques générales sur la base"""
    cursor = conn.cursor()
    
    # Nombre total de fichiers
    cursor.execute("SELECT COUNT(*) FROM copied_objects")
    total_files = cursor.fetchone()[0]
    
    # Taille totale
    cursor.execute("SELECT SUM(size) FROM copied_objects")
    total_size = cursor.fetchone()[0] or 0
    total_size_gb = total_size / (1024**3)
    
    # Date du premier et dernier backup
    cursor.execute("SELECT MIN(first_backup), MAX(last_seen) FROM copied_objects")
    min_date, max_date = cursor.fetchone()
    
    # Nombre de fichiers actifs (last_seen récent)
    now = int(datetime.now().timestamp())
    cursor.execute("SELECT COUNT(*) FROM copied_objects WHERE last_seen > ?", (now - 86400*7,))
    active_files = cursor.fetchone()[0]
    
    print("\n=== Statistiques de la base de données ===\n")
    print(f"Fichiers totaux         : {total_files:,}")
    print(f"Taille totale           : {total_size_gb:.2f} GB")
    print(f"Fichiers actifs (7j)    : {active_files:,}")
    
    if min_date and max_date:
        first = datetime.fromtimestamp(min_date).strftime('%Y-%m-%d %H:%M:%S')
        last = datetime.fromtimestamp(max_date).strftime('%Y-%m-%d %H:%M:%S')
        print(f"Premier backup          : {first}")
        print(f"Dernier backup          : {last}")
        
        days = (max_date - min_date) / 86400
        print(f"Période couverte        : {days:.1f} jours")


def main():
    parser = argparse.ArgumentParser(
        description='Utilitaire pour explorer les points de restauration Swift',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples d'utilisation:

  # Afficher les statistiques de la base
  python restore_explorer.py stats

  # Lister les points de restauration disponibles
  python restore_explorer.py list-points

  # Compter les fichiers à une date donnée
  python restore_explorer.py count --date "2026-02-01"

  # Lister les fichiers à une date donnée
  python restore_explorer.py list --date "2026-02-01" --limit 100

  # Rechercher des fichiers par nom
  python restore_explorer.py search --pattern "rapport"
        """
    )
    
    parser.add_argument(
        'command',
        choices=['stats', 'list-points', 'count', 'list', 'search'],
        help='Commande à exécuter'
    )
    
    parser.add_argument(
        '--date',
        type=str,
        help='Date pour les commandes count et list (format: YYYY-MM-DD ou YYYY-MM-DD HH:MM:SS)'
    )
    
    parser.add_argument(
        '--limit',
        type=int,
        default=50,
        help='Nombre maximum de fichiers à afficher (défaut: 50)'
    )
    
    parser.add_argument(
        '--pattern',
        type=str,
        help='Motif de recherche pour la commande search'
    )
    
    parser.add_argument(
        '--db',
        type=str,
        default='backup.db',
        help='Chemin vers le fichier de base de données (défaut: backup.db)'
    )
    
    args = parser.parse_args()
    
    # Connecter à la base
    conn = connect_db(args.db)
    
    try:
        if args.command == 'stats':
            get_db_stats(conn)
        
        elif args.command == 'list-points':
            list_restore_points(conn)
        
        elif args.command == 'count':
            if not args.date:
                print("Erreur : L'option --date est requise pour la commande count")
                parser.print_help()
                sys.exit(1)
            count_files_at_date(conn, args.date)
        
        elif args.command == 'list':
            if not args.date:
                print("Erreur : L'option --date est requise pour la commande list")
                parser.print_help()
                sys.exit(1)
            list_files_at_date(conn, args.date, args.limit)
        
        elif args.command == 'search':
            if not args.pattern:
                print("Erreur : L'option --pattern est requise pour la commande search")
                parser.print_help()
                sys.exit(1)
            search_files(conn, args.pattern)
    
    finally:
        conn.close()


if __name__ == '__main__':
    main()
