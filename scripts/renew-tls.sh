#!/bin/sh
set -eu
umask 077
docker run --rm \
    -v /opt/coursewatch/certbot/config:/etc/letsencrypt \
    -v /var/www/coursewatch-acme:/var/www/certbot \
    certbot/certbot:v5.8.0 renew --quiet
nginx -t
systemctl reload nginx
