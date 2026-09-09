# API do servidor Domotica

Base URL usada nos exemplos:

```text
https://keys.lmpinto.pt
```

Substituir todos os valores entre `<...>` pelos valores reais. Todos os
endpoints, exceto `GET /health`, exigem:

```http
Authorization: Bearer <TOKEN>
```

Os endpoints de operacao recebem as coordenadas nos headers:

```http
X-Latitude: <LATITUDE>
X-Longitude: <LONGITUDE>
```

Os dias permitidos usam `monday`, `tuesday`, `wednesday`, `thursday`,
`friday`, `saturday` e `sunday`. As horas usam `HH:MM` e sao avaliadas no
timezone `Europe/Lisbon`.

## Estado do servidor

### `GET /health`

Verifica se o servidor HTTP esta ativo. Nao requer autenticacao.

Payload: sem body.

Resposta `200 OK`:

```json
{
  "ok": true
}
```

## Permissoes do utilizador

### `GET /access/capabilities?lat=<LATITUDE>&lon=<LONGITUDE>`

Identifica o utilizador pelo bearer e calcula se pode operar cada porta,
considerando estado, role, horario, dia, distancia maxima e acesso remoto.

Payload: sem body. As coordenadas sao enviadas na query string.

Resposta `200 OK`:

```json
{
  "user": {
    "id": <USER_ID>,
    "name": "<NOME>",
    "role": "<admin|user>"
  },
  "location": {
    "lat": <LATITUDE>,
    "lon": <LONGITUDE>,
    "distance_m": <DISTANCIA_METROS>,
    "remote": <true|false>
  },
  "local_distance_limit_m": 100.0,
  "doors": {
    "APT": {
      "distance_m": <DISTANCIA_METROS>,
      "can_open": <true|false>,
      "mode": "<local|remote>",
      "requires_warning": <true|false>,
      "reason": <null|"user_disabled"|"outside_allowed_schedule"|"distance_not_allowed"|"remote_access_not_allowed">
    },
    "BLD": {
      "distance_m": <DISTANCIA_METROS>,
      "can_open": <true|false>,
      "mode": "<local|remote>",
      "requires_warning": <true|false>,
      "reason": <null|"user_disabled"|"outside_allowed_schedule"|"distance_not_allowed"|"remote_access_not_allowed">
    }
  }
}
```

Resposta `400 Bad Request`, coordenadas ausentes ou invalidas:

```json
{
  "ok": false,
  "error": "invalid coordinates"
}
```

## Porta do apartamento

Os endpoints seguintes repetem todas as validacoes no backend antes de chamar
o dispositivo. Utilizadores `admin` ativos podem operar sem os headers de GPS.

### `GET /apt_door/status`

Consulta o estado da fechadura do apartamento.

Payload: sem body.

Resposta `200 OK`:

```json
{
  "ok": true,
  "output": "<ESTADO_DEVOLVIDO_PELO_SWITCHBOT>"
}
```

### `POST /apt_door/open`

Destranca e abre a porta do apartamento.

Payload: sem body; enviar bearer e coordenadas nos headers.

Resposta `200 OK`:

```json
{
  "ok": true,
  "output": "<RESULTADO_DO_SWITCHBOT>"
}
```

### `POST /apt_door/unlock`

Destranca a porta do apartamento sem abrir.

Payload: sem body; enviar bearer e coordenadas nos headers.

Resposta `200 OK`:

```json
{
  "ok": true,
  "output": "<RESULTADO_DO_SWITCHBOT>"
}
```

### `POST /apt_door/lock`

Tranca a porta do apartamento.

Payload: sem body; enviar bearer e coordenadas nos headers.

Resposta `200 OK`:

```json
{
  "ok": true,
  "output": "<RESULTADO_DO_SWITCHBOT>"
}
```

## Porta do predio

### `POST /bld_door`

Pressiona o SwitchBot que abre a porta do predio.

Payload: sem body; enviar bearer e coordenadas nos headers.

Resposta `200 OK`:

```json
{
  "ok": true,
  "output": "<RESULTADO_DO_SWITCHBOT>"
}
```

## Administracao de utilizadores

Todos os endpoints desta seccao exigem o bearer de um utilizador `admin`
ativo. As respostas nunca incluem o token nem o seu hash, exceto o token
completo devolvido uma unica vez pelo `POST /admin/users`.
O admin autentica-se apenas com o seu proprio bearer; nao precisa dos tokens
dos restantes utilizadores.

Representacao normal de um utilizador:

```json
{
  "id": <USER_ID>,
  "name": "<NOME>",
  "role": "<admin|user>",
  "enabled": <true|false>,
  "allow_remote": <true|false>,
  "max_distance_m": <NUMERO_OU_NULL>,
  "schedules": [
    {
      "allowed_days": ["<DIA_1>", "<DIA_2>"],
      "start_time": "<HH:MM>",
      "end_time": "<HH:MM>"
    }
  ]
}
```

`max_distance_m: null` significa que nao existe limite maximo de distancia.
Isso nao ignora `allow_remote`: se `allow_remote` for `false`, a operacao
continua bloqueada quando a distancia for considerada remota.

Cada item de `schedules` e um periodo independente. O utilizador pode operar
quando pelo menos um periodo corresponde ao dia e hora atuais. Uma lista vazia
nao permite operar em nenhum horario. Para compatibilidade, os campos antigos
`allowed_days`, `start_time` e `end_time` ainda podem ser enviados sem
`schedules`; o servidor converte-os num unico periodo.

### `GET /admin/users`

Lista todos os utilizadores.

Payload: sem body.

Resposta `200 OK`:

```json
{
  "users": [
    {
      "id": <USER_ID>,
      "name": "<NOME>",
      "role": "<admin|user>",
      "enabled": <true|false>,
      "allow_remote": <true|false>,
      "max_distance_m": <NUMERO_OU_NULL>,
      "schedules": [
        {
          "allowed_days": ["<DIA_1>", "<DIA_2>"],
          "start_time": "<HH:MM>",
          "end_time": "<HH:MM>"
        }
      ]
    }
  ]
}
```

### `POST /admin/users`

Cria um utilizador com `role=user` e gera um token seguro. `name` e
obrigatorio; os restantes campos sao opcionais.

Payload:

```json
{
  "name": "<NOME>",
  "enabled": <true|false>,
  "allow_remote": <true|false>,
  "max_distance_m": <NUMERO_OU_NULL>,
  "schedules": [
    {
      "allowed_days": ["monday", "tuesday", "wednesday"],
      "start_time": "08:00",
      "end_time": "12:00"
    },
    {
      "allowed_days": ["monday", "tuesday", "wednesday"],
      "start_time": "14:00",
      "end_time": "20:00"
    }
  ]
}
```

Resposta `201 Created`:

```json
{
  "id": <USER_ID>,
  "name": "<NOME>",
  "role": "user",
  "enabled": <true|false>,
  "allow_remote": <true|false>,
  "max_distance_m": <NUMERO_OU_NULL>,
  "schedules": [
    {
      "allowed_days": ["<DIA_1>", "<DIA_2>"],
      "start_time": "<HH:MM>",
      "end_time": "<HH:MM>"
    }
  ],
  "token": "<TOKEN_COMPLETO_GUARDAR_AGORA>"
}
```

### `GET /admin/users/{id}`

Consulta um utilizador pelo ID.

Exemplo de endpoint:

```text
GET /admin/users/<USER_ID>
```

Payload: sem body.

Resposta `200 OK`: representacao normal do utilizador, sem token.

Resposta `404 Not Found`:

```json
{
  "ok": false,
  "error": "user_not_found"
}
```

### `PUT /admin/users/{id}`

Altera um ou mais campos editaveis do utilizador. Nao e necessario enviar os
campos que ficam iguais. `id`, `role` e token nao podem ser alterados.

Payload de exemplo:

```json
{
  "name": "<NOVO_NOME>",
  "enabled": <true|false>,
  "allow_remote": <true|false>,
  "max_distance_m": <NUMERO_OU_NULL>,
  "schedules": [
    {
      "allowed_days": ["monday", "friday"],
      "start_time": "08:00",
      "end_time": "12:00"
    },
    {
      "allowed_days": ["saturday", "sunday"],
      "start_time": "10:00",
      "end_time": "14:00"
    }
  ]
}
```

Resposta `200 OK`: representacao atualizada do utilizador, sem token.

### `DELETE /admin/users/{id}`

Apaga permanentemente um utilizador.

Payload: sem body.

Resposta `204 No Content`: sem body.

## Respostas de erro comuns

Bearer ausente ou invalido:

```text
HTTP 401 Unauthorized
WWW-Authenticate: Bearer
<sem body>
```

Utilizador normal a tentar aceder a `/admin/*`:

```json
{
  "ok": false,
  "error": "admin_required"
}
```

Operacao recusada pelas permissoes:

```json
{
  "ok": false,
  "error": "<RAZAO>",
  "reason": "<RAZAO>",
  "distance_m": <DISTANCIA_METROS>
}
```

`<RAZAO>` pode ser `user_disabled`, `outside_allowed_schedule`,
`distance_not_allowed` ou `remote_access_not_allowed`.

Coordenadas obrigatorias ausentes ou invalidas numa operacao de utilizador:

```json
{
  "ok": false,
  "error": "coordenadas GPS obrigatorias ou invalidas"
}
```

Payload administrativo invalido:

```json
{
  "ok": false,
  "error": "<DESCRICAO_DO_CAMPO_INVALIDO>"
}
```

Falha ao executar o dispositivo:

```json
{
  "ok": false,
  "error": "<DESCRICAO_DA_FALHA>"
}
```
