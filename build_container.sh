#/usr/bin/sh
#

# build podman container for backend web

IMAGE_NAME="stokeo_swiftbackup"
VERSION="0.5"

# Nom du conteneur temporaire
container_name="stokeo_swiftbackup_build"
# Créer un conteneur Buildah
container_id=$(buildah from alpine:3.23.4)
# Personnalisation de l'image
buildah run $container_id apk add python3
buildah run $container_id python3 -m venv /usr/local/bin/pen15/swiftbackup
buildah copy $container_id requirements.txt /usr/local/bin/pen15/swiftbackup/
buildah run $container_id /usr/local/bin/pen15/swiftbackup/bin/pip install -r /usr/local/bin/pen15/swiftbackup/requirements.txt

buildah copy $container_id swift_backup.py /usr/local/bin/pen15/swiftbackup/

buildah config --cmd "/usr/local/bin/pen15/swiftbackup/bin/python3 /usr/local/bin/pen15/swiftbackup/swift_backup.py backup" "$container_id"
buildah config --label "version=$VERSION" "$container_id"

# Commit de l'image
buildah commit $container_id $IMAGE_NAME:$VERSION
# Nettoyage du conteneur temporaire
buildah rm $container_id
# Pousser l'image vers le registre
#buildah push mon_image:1.0 docker://mon_utilisateur/mon_image:1.0
