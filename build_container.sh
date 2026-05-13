#/usr/bin/sh
#

# build podman container for backend web

IMAGE_NAME="objstor-backup"
VERSION="0.1"

# Nom du conteneur temporaire
container_name="objstor-backup_build"
# Créer un conteneur Buildah
container_id=$(buildah from alpine:3.23.4)
# Personnalisation de l'image
buildah run $container_id apk add python3
buildah run $container_id python3 -m venv /usr/bin/objstor-backup
buildah copy $container_id requirements.txt /usr/bin/objstor-backup/
buildah run $container_id /usr/bin/objstor-backup/bin/pip install -r /usr/bin/objstor-backup/requirements.txt

buildah copy $container_id objstor-backup.py /usr/bin/objstor-backup/

buildah config --cmd "/usr/bin/objstor-backup/bin/python3 /usr/bin/objstor-backup/objstor-backup.py backup" "$container_id"
buildah config --label "version=$VERSION" "$container_id"

# Commit de l'image
buildah commit $container_id $IMAGE_NAME:$VERSION
# Nettoyage du conteneur temporaire
buildah rm $container_id
# Pousser l'image vers le registre
#buildah push mon_image:1.0 docker://mon_utilisateur/mon_image:1.0
