#!/usr/bin/env python3
"""
OpenStack Swift Backup Script
Backs up objects from a source container to a backup container
with duplicate handling and 6-month retention
"""

import argparse
import logging
import os
import sqlite3
import sys
import tomllib
from datetime import datetime, timedelta, UTC
from swiftclient.service import (SwiftService, SwiftError, SwiftCopyObject,
                                 SwiftUploadObject)
from swiftclient.exceptions import ClientException


# Logging configuration
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
    """Class for managing OpenStack Swift backups"""
    def __init__(self, auth_version, auth_url, username, password, tenant_name,
                 region_name, project_name, project_domain_name, source_container,
                 backup_data_container, backup_meta_container, retention_days=180,
                 threads=10):
        """
        Initializes Swift connection

        Args:
            auth_url: OpenStack authentication URL
            username: Username
            password: Password
            tenant_name: Tenant/project name
            source_container: Source container
            backup_data_container: Backup data container
            backup_meta_container: Backup metadata container
            retention_days: Retention period in days (default: 180 = 6 months)
            threads: Number of threads to use
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

        self.dbfile = f"{self.source_container}-backup.db"
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
            logger.info(f"Connected to object storage, container {self.source_container}")
            return True
        except SwiftError as e:
            logger.error(f"Failed to connect to object storage: {e}")
            return False

    def db_connect(self):
        if not self.db_conn:
            self.db_conn = sqlite3.connect(self.dbfile)
        return True

    def db_close(self):
        self.db_conn.close()

    def init_db(self):
        """Creates metadata db file"""
        # clean any remaining metadata file
        try:
            os.remove(self.dbfile)
        except FileNotFoundError:
            pass

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
            )""")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backups (
                time INTEGER NOT NULL,
                object_id INTEGER NOT NULL,
                PRIMARY KEY (time, object_id)
            )""")
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
        Retrieves files that existed at a given restore date

        Args:
            restore_date: datetime object or timestamp of the restore date

        Returns:
            List of tuples (name, etag, size, first_backup, last_seen)
        """
        if isinstance(restore_date, datetime):
            restore_ts = int(restore_date.timestamp())
        else:
            restore_ts = int(restore_date)

        self.db_connect()
        cursor = self.db_conn.cursor()
        
        # Select files:
        # - backed up before or at the restore date
        #   (first_backup <= restore_date)
        # - still present at the restore date (last_seen >= restore_date)
        res = cursor.execute(
            ("SELECT copied_objects.name, copied_objects.etag, copied_objects.size, "
             "copied_objects.prefix "
             "FROM backups "
             "JOIN copied_objects ON backups.object_id = copied_objects.object_id "
             "WHERE backups.time=?"),
            (restore_ts,))

        return res.fetchall()

    def get_db_file(self):
        """get sqlite3 file from object storage"""
        obj_path = "{}/{}".format(self.source_container, self.dbfile)
        result = self.swift_conn.download(container=self.backup_meta_container,
                                          objects=[obj_path],
                                          options={"out_file": self.dbfile})
        for down_res in result:
            if down_res['success'] and down_res['object'] == obj_path:
                return True
            elif down_res['object'] == obj_path:
                err = down_res.get("error")
                logger.error(f"Could not get db file at {obj_path}: {err}")
        return False

    def save_db_file(self):
        """write sqlite3 file to object storage meta container"""
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
        """Check if backup containers exists"""
        try:
            self.swift_conn.stat(container=self.backup_data_container)
            logger.info(f"backup data container '{self.backup_data_container}' "
                        "already exists")
            if self.backup_meta_container != self.backup_data_container:
                self.swift_conn.stat(container=self.backup_meta_container)
                logger.info(f"backup meta container '{self.backup_meta_container}' "
                            "already exists")
            # check if sqlite db is inside
            if not self.get_db_file():
                self.init_db()
        except ClientException:
            try:
                # self.swift_conn.put_container(self.backup_container)
                # logger.info(f"Backup container '{self.backup_container}' created")
                # create and upload sqlite db
                self.init_db()
            except ClientException as e:
                logger.error(f"Error creating backup container: {e}")
                raise

    def get_object_etag(self, container, obj_name):
        """
        Retrieves the ETag (MD5) of an object

        Args:
            container: Container name
            obj_name: Object name

        Returns:
            Object's ETag or None if not found
        """
        try:
            obj_path = "{}/{}".format(self.source_container, self.dbfile)
            headers = self.swift_conn.head_object(container, obj_path)
            return headers.get('etag', '').strip('"')
        except ClientException:
            return None

    def object_already_in_backup(self, obj_name, etag, size):
        """
        Check if object has already been copied to backup container using sqlite db
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
                logger.debug(f"Object '{obj_name}' already backed up (identical)")

        # close db
        cursor.close()
        self.db_conn.commit()

        return copy_objects, obj_count, skipped


    def backup_objects(self, obj_list):
        """
        Backs up a list of objects from the source container to the backup container

        Args:
            obj_list: object list from source container

        Returns:
            True if backed up, False if already existing and identical
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
        """Deletes backups older than the retention period"""
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

            logger.info(f"Cleanup finished: {deleted_count} objects deleted")

        except ClientException as e:
            logger.error(f"Error while cleaning older files: {e}")

    def run_backup(self):
        """Executes the complete backup process"""
        logger.info(f"Starting backup {self.source_container}")
        
        if not self.swift_connect():
            logger.error("Cannot connect to object storage, ending script")
            return False
        
        try:
            # Check/create the backup container
            self.ensure_backup_container_exists()
            
            # List objects from source container
            source_objects = make_source_objects_gen(
                self.swift_conn.list(self.source_container)
            )

            copied, skipped, failed, total = self.backup_objects(source_objects)

            logger.info(f"Found {total} objects in container '{self.source_container}'")
            logger.info(f"{copied} new, {skipped} skipped, {failed} failed")
            
            # Clean up old backups
            self.cleanup_old_backups()

            # save db file to object storage
            self.save_db_file()
            
            logger.info(f"Backup container {self.source_container} success")
            return True
            
        except ClientException as e:
            logger.error(f"Error during backup: {e}")
            return False

    def restore_objects(self, restore_date, dry_run=False, target_container=None):
        """
        Restore objects from backup container to source container
        from a given restore point

        Args:
            restore_date: datetime object or string
              (format: YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)
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

        # Parse restore date
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
                logger.error("Accepted formats: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'")
                return False
        else:
            restore_datetime = restore_date

        restore_ts = int(restore_datetime.timestamp())
        logger.info("Restore points: "
                    f"{restore_datetime.strftime('%Y-%m-%d %H:%M:%S')}")

        if target_container is None:
            target_container = self.source_container
            logger.warning(f"Restore to the object storage container: {target_container}")
            logger.warning("Existing files will be overwritten!")
        else:
            logger.info(f"Restore to container: {target_container}")

        try:
            # Retrieve the database
            if not self.get_db_file():
                logger.error("Could not retrieve backup database")
                return False

            # Retrieve list of files to restore
            files_to_restore = self.get_files_at_restore_point(restore_ts)

            if not files_to_restore:
                logger.warning("No files found at restore point "
                               f"{restore_datetime}")
                return False

            logger.info(f"Found {len(files_to_restore)} file(s) to restore")

            if dry_run:
                logger.info("=== DRY-RUN MODE: No restore will be performed ===")
                logger.info("\nFiles that would be restored:")
                for name, etag, size, prefix in files_to_restore:
                    logger.info(
                        f"  - {name} "
                        f"size: {size} bytes, ETag: {etag} ")
                return True

            # Restore files
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

            # Perform batch restore
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
                        logger.info(f"✓ Restored: {dest_name}")
                    else:
                        failed += 1
                        obj_name = r.get('object', '')
                        error = r.get('error', 'Unknown error')
                        logger.error(f"✗ Restore failed: {obj_name} - {error}")

            logger.info("\n=== Restore complete ===")
            logger.info(f"Restored: {restored}")
            logger.info(f"Failed: {failed}")
            logger.info(f"Total: {len(files_to_restore)}")

            return failed == 0
            
        except Exception as e:
            logger.error(f"Error during restore: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def list_backups(self):
        if not self.swift_connect():
            logger.error("Could not connect to Swift, stopping script")
            return False

        if not self.get_db_file():
            logger.error("Could not retrieve backup database")
            return False

        print("Restore points:")
        for backup_time in self.db_list_backups():
            backup_datetime = datetime.fromtimestamp(int(backup_time))
            backup_hr = backup_datetime.strftime('%Y/%m/%d %H:%M:%S')
            print(f" - {backup_time}: {backup_hr}")



def main():
    """Main entry point for the script"""

    parser = argparse.ArgumentParser(
        description='OpenStack Swift Backup and Restore Script',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage examples:

  # Normal backup
  python swift_backup.py backup

  # List restore points
  python swift_backup.py listbackups

  # Dry-run restoration at a given date
  python swift_backup.py restore --date "2026-02-01" --dry-run

  # Effective restoration at a specific date and time
  python swift_backup.py restore --date "2026-02-01 14:30:00"

  # Restoration to a different container
  python swift_backup.py restore --date "2026-02-01" --target-container test_container
        """
    )

    parser.add_argument(
        'action',
        choices=['backup', 'restore', 'listbackups'],
        help='Action to perform: backup, restore, or listbackups'
    )

    parser.add_argument(
        '--date',
        type=str,
        help=('Restore date (format: YYYY-MM-DD or YYYY-MM-DD HH:MM:SS) '
              '- required for restore')
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help=('Dry-run mode for restoration (displays files '
              'without restoring them)')
    )

    parser.add_argument(
        '--target-container',
        type=str,
        help=('Destination container for restoration '
              '(default: SOURCE_CONTAINER)')
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
        logger.error(f"Missing configuration data: {', '.join(missing_vars)}")
        sys.exit(1)

    for source_container in config["source_containers"]:
        current_container_config = config.copy()
        current_container_config["source_container"] = source_container
        del current_container_config["source_containers"]
        # Create SwiftBackup instance
        backup = SwiftBackup(**current_container_config)

        # Execute requested action
        if args.action == 'backup':
            success = backup.run_backup()

        elif args.action == 'restore':
            if not args.date:
                logger.error("The --date option is required for restoration")
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
