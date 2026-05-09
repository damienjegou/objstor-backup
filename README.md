# Backup and restoration for Openstack Swift object storage

Creates backup copy from one Openstack Swift object storage to another. Incremental backup by default using sqlite3 to index files.

Tested on OVH public cloud.


## Features

- Supports Openstack Swift
- Server side copy
- Incremental backup

## TODO

- Adds support to restore from cold storage (implements files unfreeze)

