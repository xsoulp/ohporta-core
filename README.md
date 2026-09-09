# Domotica server

Servidor HTTP sem dependencias externas para controlar as portas via SwitchBot.

GET  /apt_door/status
POST /apt_door/lock
POST /apt_door/unlock
POST /apt_door/open
POST /bld_door
GET  /health
GET  /access/capabilities?lat=...&lon=...

GET    /admin/users
POST   /admin/users
GET    /admin/users/{id}
PUT    /admin/users/{id}
DELETE /admin/users/{id}

Todos os endpoints, exceto `/health`, usam `Authorization: Bearer <token>`.
Os endpoints `/admin/*` exigem um utilizador ativo com `role=admin`.

## Persistencia e migracao

Os utilizadores sao guardados por omissao em `users.json` (modo `0600`). O
ficheiro contem apenas hashes SHA-256 dos tokens. Na primeira execucao, os
argumentos existentes `--token-file` e `--restricted-token-file` criam:

- id 1, `Admin`, role `admin`, com o bearer principal existente;
- id 2, `User 2`, role `user`, com o bearer restrito existente.

Depois da primeira execucao, o servidor autentica exclusivamente através de
`users.json`; os argumentos dos tokens legados tornam-se opcionais.

Os dias usam nomes ingleses em minusculas (`monday` a `sunday`) e as horas usam
`HH:MM`. Os horarios sao avaliados em `Europe/Lisbon` e podem atravessar a
meia-noite.

## Executar

Primeira execucao/migracao, compativel com a unidade atual:

```sh
python3 switchbot-server.py --host 0.0.0.0 --port 8765 \
  --token-file .switchbot-server.token \
  --restricted-token-file .switchbot-server-restricted.token
```

Execucoes seguintes podem usar apenas:

```sh
python3 switchbot-server.py --host 0.0.0.0 --port 8765
```

## Testes

```sh
python3 -m unittest discover -s tests -v
```
