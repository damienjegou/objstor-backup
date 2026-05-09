#!/usr/bin/env python3
"""
Script de sauvegarde OpenStack Swift
Sauvegarde les objets d'un conteneur source vers un conteneur de backup
avec gestion des duplications et rétention de 6 mois
"""

import argparse
import logging
import sqlite3
import sys
import tomllib
from datetime import datetime, timedelta, UTC
from swiftclient.service import (SwiftService, SwiftError, SwiftCopyObject,
                                 SwiftUploadObject)
from swiftclient.exceptions import ClientException


# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def make_source_objects_gen(parts_gen):
    for page in parts_gen:
        if page["success"]:
            for obj in page["listing"]:
                yield obj


class SwiftBackup:
    """Classe pour gérer les sauvegardes OpenStack Swift"""
    
    def __init__(self, auth_version, auth_url, username, password, tenant_name,
                 region_name, project_name, project_domain_name, source_container,
                 backup_data_container, backup_meta_container, retention_days=180,
                 threads=10):
        """
        Initialise la connexion Swift

        Args:
            auth_url: URL d'authentification OpenStack
            username: Nom d'utilisateur
            password: Mot de passe
            tenant_name: Nom du tenant/projet
            source_containers: Conteneurs source
            backup_container: Conteneur de backup
            backup_prefix: backups dans un sous dossier du conteneur de backup
            retention_days: Durée de rétention en jours (défaut: 180 = 6 mois)
        """
        self.auth_version = auth_version
        self.auth_url = auth_url
        self.username = username
        self.password = password
        self.tenant_name = tenant_name
        self.region_name = region_name
        self.project_name = project_name
        self.project_domain_name = project_domain_name
        self.source_container = source_container
        self.backup_data_container = backup_data_container
        self.backup_meta_container = backup_meta_container
        self.retention_days = retention_days
        self.threads = threads
        self.swift_conn = None

        self.dbfile = "backup.db"
        self.db_conn = None
        self.db_cursor = None
        
    def swift_connect(self):
        """Connects to Swift"""
        connect_options = {
            # auth options
            "auth_version": self.auth_version,
            "os_username": self.username,
            "os_password": self.password,
            "os_project_name": self.project_name,
            "os_project_domain_name": self.project_domain_name,
            "os_auth_url": self.auth_url,
            "os_region_name": self.region_name,
            # other options
            "container_threads": self.threads,
            "object_dd_threads": self.threads,
            "object_uu_threads": self.threads,
            "segment_threads": self.threads,
            "fail_fast": True,
        }
        try:
            self.swift_conn = SwiftService(options=connect_options)
            logger.info("Connexion à Swift établie avec succès")
            return True
        except SwiftError as e:
            logger.error(f"Erreur de connexion à Swift: {e}")
            return False

    def db_connect(self):
        if not self.db_conn:
            self.db_conn = sqlite3.connect(self.dbfile)
        return True

    def db_close(self):
        self.db_conn.close()

    def init_db(self):
        """ Creates metadata db file """
        self.db_connect()
        cursor = self.db_conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS copied_objects (
                object_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                etag TEXT NOT NULL,
                size INTEGER NOT NULL,
                prefix TEXT,
                UNIQUE (name, etag, size)
            )"""
        )
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backups (
                time INTEGER NOT NULL,
                object_id INTEGER NOT NULL,
                PRIMARY KEY (time, object_id)
            )"""
        )
        cursor.close()
        self.db_conn.commit()

    def get_db_older_files(self):
        now_ts = int(datetime.now(UTC).timestamp())
        cutoff_date = now_ts - (24 * 3600 * self.retention_days)
        self.db_connect()
        cursor = self.db_conn.cursor()

        # get older files
        res = cursor.execute(
            ("SELECT object_id, name, prefix FROM copied_objects WHERE object_id in "
             "(SELECT object_id FROM backups "
             "EXCEPT SELECT object_id FROM backups WHERE time > ?)"),
            (cutoff_date,)
        )
        older_files = res.fetchall()

        # delete older files
        removed_files = 0
        for f in older_files:
            cursor.execute("DELETE FROM copied_objects WHERE object_id=?", (f[0],))
            removed_files += 1
            logger.debug(f"Deleting file {f[2]}/{f[1]}")
        self.db_conn.commit()
        logger.info(f"Files removed from db: {removed_files}")

        # delete older backups
        toremove_backups = cursor.execute(
            "SELECT DISTINCT time FROM backups WHERE time<?", (cutoff_date,)).fetchall()
        toremove_backups = [b[0] for b in toremove_backups]
        logger.info(f"Backups to remove from db: {toremove_backups}")
        cursor.execute("DELETE FROM backups WHERE time<?", (cutoff_date,))
        self.db_conn.commit()
        cursor.close()

        return ("{}/{}/{}".format(
            self.source_container, f[2], f[1])
                for f in older_files)

    def db_insert_new_obj(self, obj_name, etag, size, prefix, now_ts):
        self.db_connect()
        cursor = self.db_conn.cursor()
        
        cursor.execute(
            "INSERT INTO copied_objects (name, etag, size, prefix) VALUES (?, ?, ?, ?)",
            (obj_name, etag, size, prefix)
        )
        object_rowid = cursor.lastrowid
        cursor.execute(
            "INSERT INTO backups (time, object_id) VALUES (?, ?)",
            (now_ts, object_rowid)
        )

        cursor.close()
        self.db_conn.commit()

    def db_insert_existing_obj(self, obj_name, etag, size, date):
        self.db_connect()
        cursor = self.db_conn.cursor()

        res = cursor.execute(
            "SELECT object_id FROM copied_objects WHERE name=? AND etag=? AND size=?",
            (obj_name, etag, size)
        )
        object_id = res.fetchone()[0]
        cursor.execute(
            ("INSERT INTO backups (time, object_id) VALUES (?, ?)"),
            (date, object_id)
        )
        cursor.close()
        self.db_conn.commit()

    def db_insert_obj(self, cursor, obj_name, etag, size, prefix, now_ts):
        new = False
        res = cursor.execute(
            "SELECT object_id FROM copied_objects WHERE name=? AND etag=? AND size=?",
            (obj_name, etag, size)
        )
        row = res.fetchone()
        if row:
            object_id = row[0]
        else:
            new = True
            cursor.execute(
                ("INSERT INTO copied_objects (name, etag, size, prefix) "
                 "VALUES (?, ?, ?, ?)"),
                (obj_name, etag, size, prefix)
            )
            object_id = cursor.lastrowid

        cursor.execute(
            ("INSERT INTO backups (time, object_id) VALUES (?, ?)"),
            (now_ts, object_id)
        )

        return new

    def db_mult_insert_obj(self, objects, now_ts):
        """
        Args:
            - list of tuples (obj_name, etag, size, prefix)
            - int : object creation and last seen timestamp
        """
        self.db_connect()
        cursor = self.db_conn.cursor()

        new_objects = list()
        
        for obj in objects:
            obj_name, etag, size, prefix = obj

            res = cursor.execute(
                ("SELECT object_id FROM copied_objects "
                 "WHERE name=? AND etag=? AND size=?"),
                (obj_name, etag, size)
            )
            row = res.fetchone()
            if row:
                object_id = row[0]
            else:
                new_objects.append((obj_name, etag, size))
                cursor.execute(
                    ("INSERT INTO copied_objects (name, etag, size, prefix) "
                     "VALUES (?, ?, ?, ?)"),
                    (obj_name, etag, size, prefix)
                )
                object_id = cursor.lastrowid

            cursor.execute(
                ("INSERT INTO backups (time, object_id) VALUES (?, ?)"),
                (now_ts, object_id)
            )

        cursor.close()
        self.db_conn.commit()

        return new_objects

    def db_list_backups(self):
        self.db_connect()
        cursor = self.db_conn.cursor()
        
        res = cursor.execute("SELECT DISTINCT time FROM backups")
        backups = res.fetchall()
        cursor.close()

        return [i[0] for i in backups]

    def get_files_at_restore_point(self, restore_date):
        """
        Récupère les fichiers qui existaient à une date de restauration donnée
        
        Args:
            restore_date: datetime object ou timestamp de la date de restauration
            
        Returns:
            Liste des tuples (name, etag, size, first_backup, last_seen)
        """
        if isinstance(restore_date, datetime):
            restore_ts = int(restore_date.timestamp())
        else:
            restore_ts = int(restore_date)

        self.db_connect()
        cursor = self.db_conn.cursor()
        
        # Sélectionner les fichiers :
        # - sauvegardés avant ou à la date de restauration
        #   (first_backup <= restore_date)
        # - encore présents à la date de restauration (last_seen >= restore_date)
        # res = cursor.execute("""
        #     SELECT name, etag, size, first_backup, last_seen
        #     FROM copied_objects
        #     WHERE first_backup <= ? AND last_seen >= ?
        #     ORDER BY name
        # """, (restore_ts, restore_ts))

        res = cursor.execute(
            ("SELECT copied_objects.name, copied_objects.etag, copied_objects.size, "
             "copied_objects.prefix "
             "FROM backups "
             "JOIN copied_objects ON backups.object_id = copied_objects.object_id "
             "WHERE backups.time=?"),
            (restore_ts,))

        return res.fetchall()

    def get_db_file(self):
        """ get sqlite3 file from object storage """
        obj_path = "{}/{}".format(self.source_container, self.dbfile)
        result = self.swift_conn.download(container=self.backup_meta_container,
                                          objects=[obj_path],
                                          options={"out_file": self.dbfile})
        for down_res in result:
            if down_res['success'] and down_res['object'] == obj_path:
                return True
            elif down_res['object'] == obj_path:
                err = down_res.get("error")
                logger.error(f"Could not get db file at {obj_path} : {err}")
        return False

    def save_db_file(self):
        """ write sqlite3 file to object storage meta container """
        self.db_close()
        
        obj_path = "{}/{}".format(self.source_container, self.dbfile)

        up_res = self.swift_conn.upload(
            self.backup_meta_container,
            [SwiftUploadObject(self.dbfile, object_name=obj_path)]
        )
        for up in up_res:
            if up['success']:
                logger.info("db file saved to backup container")
    
    def ensure_backup_container_exists(self):
        """ Check if backup containers exists """
        try:
            self.swift_conn.stat(container=self.backup_data_container)
            logger.info(f"backup data container '{self.backup_data_container}' "
                        "aleady exists")
            if self.backup_meta_container != self.backup_data_container:
                self.swift_conn.stat(container=self.backup_meta_container)
                logger.info(f"backup meta container '{self.backup_meta_container}' "
                            "aleady exists")
            # check if sqlite db is inside
            if not self.get_db_file():
                self.init_db()
        except ClientException:
            try:
                # self.swift_conn.put_container(self.backup_container)
                # logger.info(f"Conteneur de backup '{self.backup_container}' créé")
                # create and upload sqlite db
                self.init_db()
            except ClientException as e:
                logger.error(f"Erreur lors de la création du conteneur de backup: {e}")
                raise

    def get_object_etag(self, container, obj_name):
        """
        Récupère l'ETag (MD5) d'un objet
        
        Args:
            container: Nom du conteneur
            obj_name: Nom de l'objet
            
        Returns:
            ETag de l'objet ou None si non trouvé
        """
        try:
            obj_path = "{}/{}".format(self.source_container, self.dbfile)
            headers = self.swift_conn.head_object(container, obj_path)
            return headers.get('etag', '').strip('"')
        except ClientException:
            return None

    def object_already_in_backup(self, obj_name, etag, size):
        """
        check if object has already been copied to backup container using sqlite db
        """
        self.db_connect()
        cursor = self.db_conn.cursor()

        cursor.execute(
            ("SELECT name, etag, size FROM copied_objects "
             "WHERE name = ? AND etag = ? AND size = ?"),
            (obj_name, etag, size))
        row = cursor.fetchone()

        if not row:
            return False
        else:
            return True

    def backup_select_copy_objects(self, obj_list):
        now = datetime.now()
        now_ts = int(now.timestamp())
        now_humanreadable = now.strftime('%Y%m%d_%H%M%S')
        copy_objects = list()

        obj_count = 0
        skipped = 0

        # db open
        self.db_connect()
        cursor = self.db_conn.cursor()
        
        for obj in obj_list:
            obj_count += 1

            obj_name = obj.get('name')
            source_etag = obj.get('hash', '')
            obj_size = obj.get('bytes', '')

            # insert object in db, check if object already exists
            new_object = self.db_insert_obj(cursor, obj_name, source_etag, obj_size,
                                            now_humanreadable, now_ts)

            if new_object:
                destination = ("/{dcont}/{scont}/{timestamp}/{obj}"
                               .format(dcont=self.backup_data_container,
                                       scont=self.source_container,
                                       timestamp=now_humanreadable,
                                       obj=obj_name))
                copy_objects.append(
                    SwiftCopyObject(
                        obj_name,
                        options={"destination": destination}))
            else:
                skipped += 1
                logger.debug(f"Objet '{obj_name}' déjà sauvegardé (identique)")

        # close db
        cursor.close()
        self.db_conn.commit()

        return copy_objects, obj_count, skipped


    def backup_objects(self, obj_list):
        """
        Sauvegarde une liste d'objets du conteneur source vers le conteneur de backup
        
        Args:
            obj_list: object list from source container
            
        Returns:
            True si sauvegardé, False si déjà existant et identique
        """
        copy_objects, total_obj, skipped_obj = self.backup_select_copy_objects(obj_list)

        try:
            result = self.swift_conn.copy(
                container=self.source_container,
                objects=copy_objects
            )

            copied = 0
            failed = 0
            for r in result:
                if r["action"] == "copy_object":
                    if r["success"]:
                        obj_name = r.get('object')
                        dest_name = r.get('destination')
                        copied += 1
                        logger.debug(f"Object '{obj_name}' saved to '{dest_name}'")
                    else:
                        failed += 1
                        obj_name = r.get('object')
                        err = r.get("error")
                        logger.error(f"Error while saving '{obj_name}': {err}")
            
            return (copied, skipped_obj, failed, total_obj)
            
        except ClientException as e:
            logger.error(f"Error while saving '{obj_name}': {e}")
            return False


    def cleanup_old_backups(self):
        """Supprime les sauvegardes plus anciennes que la période de rétention"""
        try:
            cutoff_date = datetime.now() - timedelta(days=self.retention_days)
            logger.info("Cleaning backups older than "
                        f"{cutoff_date.strftime('%Y-%m-%d')}")
            
            objects = self.get_db_older_files()
            deleted_count = 0

            del_iter = self.swift_conn.delete(
                container=self.backup_data_container,
                objects=objects)
            for del_res in del_iter:
                if del_res['success'] and not del_res['action'] == 'bulk_delete':
                    deleted_count += 1

            logger.info(f"Cleanup finished: {deleted_count} objets deleted")

        except ClientException as e:
            logger.error(f"Error while cleaning older files: {e}")

    def run_backup(self):
        """Exécute le processus complet de sauvegarde"""
        logger.info("=== Début de la sauvegarde Swift ===")
        
        if not self.swift_connect():
            logger.error("Cannot connect to object storage, ending script")
            return False
        
        try:
            # Vérifier/créer le conteneur de backup
            self.ensure_backup_container_exists()
            
            # Lister les objets du conteneur source
            source_objects = make_source_objects_gen(
                self.swift_conn.list(self.source_container)
            )

            copied, skipped, failed, total = self.backup_objects(source_objects)

            logger.info(f"Found {total} objets in container '{self.source_container}'")
            logger.info(f"{copied} new, {skipped} skipped, {failed} failed")
            
            # Nettoyer les anciennes sauvegardes
            self.cleanup_old_backups()

            # save db file to object storage
            self.save_db_file()
            
            logger.info("=== Sauvegarde Swift terminée avec succès ===")
            return True
            
        except ClientException as e:
            logger.error(f"Erreur durant la sauvegarde: {e}")
            return False

    def restore_objects(self, restore_date, dry_run=False, target_container=None):
        """
        Restore objects from backup container to source container
        from a given restore point

        Args:
            restore_date: datetime object or string
              (format: YYYY-MM-DD ou YYYY-MM-DD HH:MM:SS)
            dry_run: If True, only prints files to be restored
              does not copy any data
            target_container: Destination container (default: source_container)

        Returns:
            True if success, else False
        """
        logger.info("=== Starting object store restoration ===")

        if not self.swift_connect():
            logger.error("Cannot connect to object store, ending script")
            return False

        # Parser la date de restauration
        if isinstance(restore_date, str):
            try:
                if restore_date.isdigit():
                    restore_datetime = datetime.fromtimestamp(int(restore_date))
                elif ' ' in restore_date:
                    restore_datetime = datetime.strptime(
                        restore_date, '%Y-%m-%d %H:%M:%S')
                else:
                    restore_datetime = datetime.strptime(restore_date, '%Y-%m-%d')
            except ValueError as e:
                logger.error(f"Invalid date format: {e}")
                logger.error("Formats acceptés: 'YYYY-MM-DD' ou 'YYYY-MM-DD HH:MM:SS'")
                return False
        else:
            restore_datetime = restore_date

        restore_ts = int(restore_datetime.timestamp())
        logger.info("Restore points: "
                    f"{restore_datetime.strftime('%Y-%m-%d %H:%M:%S')}")

        if target_container is None:
            target_container = self.source_container
            logger.warning(f"Restauration vers le conteneur source: {target_container}")
            logger.warning("Les fichiers existants seront écrasés!")
        else:
            logger.info(f"Restauration vers le conteneur: {target_container}")

        try:
            # Récupérer la base de données
            if not self.get_db_file():
                logger.error("Impossible de récupérer la base de données de backup")
                return False

            # Récupérer la liste des fichiers à restaurer
            files_to_restore = self.get_files_at_restore_point(restore_ts)

            if not files_to_restore:
                logger.warning("Aucun fichier trouvé au point de restauration"
                               f" {restore_datetime}")
                return False

            logger.info(f"Trouvé {len(files_to_restore)} fichier(s) à restaurer")

            if dry_run:
                logger.info("=== MODE DRY-RUN: Aucune restauration "
                            "ne sera effectuée ===")
                logger.info("\nFichiers qui seraient restaurés:")
                for name, etag, size in files_to_restore:
                    logger.info(
                        f"  - {name} "
                        f"size: {size} bytes, ETag: {etag} ")
                return True

            # Restaurer les fichiers
            restored = 0
            failed = 0

            copy_objects = []

            # time_humanreadable = restore_datetime.strftime('%Y%m%d_%H%M%S')
            for name, etag, size, prefix in files_to_restore:
                backup_path = ("{source_cont}/{timestamp}/{obj}"
                               .format(source_cont=self.source_container,
                                       timestamp=prefix,
                                       obj=name))

                if backup_path:
                    destination = f"/{target_container}/{name}"
                    copy_objects.append(
                        SwiftCopyObject(
                            backup_path,
                            options={"destination": destination}
                        )
                    )
                    logger.info(f"Prepare restore: {name} from {backup_path}")
                else:
                    logger.error(f"Backup not found for: {name}")
                    failed += 1

            if not copy_objects:
                logger.error("No backup file found to restore")
                return False

            # Effectuer la restauration par lot
            logger.info(f"Restoring {len(copy_objects)} file(s)...")

            result = self.swift_conn.copy(
                container=self.backup_data_container,
                objects=copy_objects
            )

            for r in result:
                if r["action"] == "copy_object":
                    if r["success"]:
                        obj_name = r.get('object', '')
                        dest_name = r.get('destination', '')
                        restored += 1
                        logger.info(f"✓ Restauré: {dest_name}")
                    else:
                        failed += 1
                        obj_name = r.get('object', '')
                        error = r.get('error', 'Erreur inconnue')
                        logger.error(f"✗ Échec restauration: {obj_name} - {error}")

            logger.info("\n=== Restauration terminée ===")
            logger.info(f"Restaurés: {restored}")
            logger.info(f"Échecs: {failed}")
            logger.info(f"Total: {len(files_to_restore)}")

            return failed == 0
            
        except Exception as e:
            logger.error(f"Erreur durant la restauration: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def list_backups(self):
        if not self.swift_connect():
            logger.error("Impossible de se connecter à Swift, arrêt du script")
            return False

        if not self.get_db_file():
            logger.error("Impossible de récupérer la base de données de backup")
            return False

        print("Restore points:")
        for backup_time in self.db_list_backups():
            backup_datetime = datetime.fromtimestamp(int(backup_time))
            backup_hr = backup_datetime.strftime('%Y/%m/%d %H:%M:%S')
            print(f" - {backup_time}: {backup_hr}")



def main():
    """Point d'entrée principal du script"""

    parser = argparse.ArgumentParser(
        description='Script de sauvegarde et restauration OpenStack Swift',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples d'utilisation:

  # Sauvegarde normale
  python swift_backup.py backup

  # List restore points
  python swift_backup.py listbackups

  # Restauration à une date donnée (dry-run)
  python swift_backup.py restore --date "2026-02-01" --dry-run

  # Restauration effective à une date et heure précises
  python swift_backup.py restore --date "2026-02-01 14:30:00"

  # Restauration vers un conteneur différent
  python swift_backup.py restore --date "2026-02-01" --target-container conteneur_test

Variables d'environnement requises:
  OS_AUTH_URL, OS_USERNAME, OS_PASSWORD, OS_PROJECT_NAME,
  OS_REGION_NAME, SOURCE_CONTAINER, BACKUP_CONTAINER
        """
    )

    parser.add_argument(
        'action',
        choices=['backup', 'restore', 'listbackups'],
        help='Action à effectuer: backup, restore ou listbackups'
    )

    parser.add_argument(
        '--date',
        type=str,
        help=('Date de restauration (format: YYYY-MM-DD ou YYYY-MM-DD HH:MM:SS) '
              '- requis pour restore')
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help=('Mode simulation pour la restauration (affiche les fichiers '
              'sans les restaurer)')
    )

    parser.add_argument(
        '--target-container',
        type=str,
        help=('Conteneur de destination pour la restauration '
              '(par défaut: SOURCE_CONTAINER)')
    )

    args = parser.parse_args()
    
    # read TOML config
    with open("/etc/objstor-backup/config.toml", "rb") as fp:
        config = tomllib.load(fp)

    # check required parameters presence
    required_vars = ['auth_url', 'username', 'password', 'project_name', 
                     'region_name', 'source_containers', 'backup_data_container',
                     'backup_meta_container']
    missing_vars = [var for var in required_vars if not config.get(var)]

    if missing_vars:
        logger.error(f"Missing configuration data : {', '.join(missing_vars)}")
        sys.exit(1)

    for source_container in config["source_containers"]:
        current_container_config = config.copy()
        current_container_config["source_container"] = source_container
        del current_container_config["source_containers"]
        # Créer l'instance SwiftBackup
        backup = SwiftBackup(**current_container_config)

        # Exécuter l'action demandée
        if args.action == 'backup':
            success = backup.run_backup()

        elif args.action == 'restore':
            if not args.date:
                logger.error("L'option --date est requise pour la restauration")
                parser.print_help()
                sys.exit(1)

            success = backup.restore_objects(
                restore_date=args.date,
                dry_run=args.dry_run,
                target_container=args.target_container
            )
        elif args.action == 'listbackups':
            backup.list_backups()
            success = True


    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
