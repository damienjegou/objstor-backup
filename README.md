# Backup and restoration for Openstack Swift object storage

Creates backup copy from one Openstack Swift object storage to another. Incremental backup by default using sqlite3 to index files.

Tested on OVH public cloud.


## Features

- Backup and restore from one object storage to another
- Incremental backup : only modified files are copied for storage efficiency
- Server side copy
- Supports Openstack Swift

## Configuration

Config file sample data :

```
# OpenStack Authentification
auth_version = "3"
auth_url = ""
username = ""
password = ""
project_name = ""
project_domain_name = ""
tenant_name = ""
region_name = ""
threads = 10

# Containers to backup
source_containers = ["source_container"]
# container for backup data files, can be cold storage
backup_data_container = "backup_data_container"
# container for backup metadata sqlite db
# must be hot storage
# can be the same as backup_data_container
backup_meta_container = "backup_meta_container"

# Retention
# backups retention lenght in days (180 = 6 months)
retention_days = 180
```

Default config file place and name : /etc/objstor-backup/config.toml
Custom config file path can be set with command line argument --config
```
python3 objstor-backup.py backup --config ~/.objstor-backup.conf
```

## Usage

### Podman

```
# build container
sh build_container.sh
```

Modify config file to fit your environment, then copy config.toml in folder objstor-backup_config

```
podman run objstor-backup:latest -v objstor-backup_config:/etc/objstor-backup backup
```

### Virtual Environment

In project directory, creates a python virtual environment and install dependencies
```
python3 -m venv .
source bin/activate
pip install -r requirements.txt

# Fill config file before running commands
# run backup
python3 objstor-backup.py backup

# list restore points
python3 objstor-backup.py listbackups

# Dry run restore data
python3 objstor-backup.py restore --date --dry-run

# restore data
python3 objstor-backup.py restore --date
```

