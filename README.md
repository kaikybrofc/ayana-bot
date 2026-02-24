# Ayana Bot (Discord)

Bot Discord em Python com comandos *slash* organizados em **cogs**, moderação básica, logs e tratamento global de erros.

## Funcionalidades
- Estrutura modular com cogs
- Sistema de nivel e XP por mensagens (contabiliza toda mensagem de usuario)
- Comandos utilitários:
  - `/help` (lista geral) e `/help comando:<nome>` (detalhado)
  - `/ping`
  - `/userinfo [membro]`
  - `/serverinfo`
  - `/rank [membro]` (card em canvas)
  - `/leaderboard [limite]` (ranking em canvas)
- Comandos de imagens (NekoSia):
  - `/nekosia [category] [count] [additional_tags] [blacklisted_tags] [rating]`
  - `/nekosia_id <image_id>`
  - `/nekosia_tags [tipo] [termo]`
- Comandos de moderação:
  - `/clear <quantidade>`
  - `/kick <membro> [motivo]`
  - `/ban <membro> [motivo]`
  - `/unban <usuario_banido_ou_id> [motivo]`
  - `/timeout <membro> <duracao> [motivo]` (ex.: `30m`, `2h`, `1d`)
  - `/untimeout <membro> [motivo]`
  - `/warn <membro> <motivo>`
  - `/warnings <membro>`
  - `/clearwarnings <membro>`
  - `/infractions <membro> [limite]`
  - `/settings`
  - `/setmodlog [canal]`
  - `/setautomodlog [canal]`
  - `/setwarnpolicy ...`
  - `/setautomod ...`
  - `/addroleall <cargo> [include_bots]`
  - `/restaurar` (somente dono do sistema)
- Sistema de boas-vindas configurável:
  - `/welcomesettings`
  - `/setwelcome ...`
  - `/welcometest [membro]`
- Sistema de avisos persistente em MySQL
- Expiração de warns configurável por servidor
- Escalonamento automático por warns (timeout/ban)
- AutoMod básico (anti-spam, anti-link, anti-mention flood)
- Configuração por servidor em `guild_settings` (moderacao, automod e welcome)
- Auditoria unificada em `infractions`
- Logs no terminal e em arquivo (`logs/bot.log`)
- Tratamento global de erros para comandos *slash*

## Requisitos
- Python 3.10+
- Conta e aplicação no Discord Developer Portal
- MySQL 8+ (ou compatível)

## Configuração
1. Crie e ative seu ambiente virtual.
2. Instale as dependências:

```bash
pip install -r requirements.txt
```

3. Copie o arquivo de exemplo e configure suas variáveis:

```bash
cp .env.example .env
```

4. Edite o `.env`:

```env
DISCORD_TOKEN=seu_token_do_bot
GUILD_ID=123456789012345678
DONO_ID=123456789012345678
DB_HOST=localhost
DB_PORT=3306
DB_USER=seu_usuario_mysql
DB_PASSWORD=sua_senha_mysql
DB_NAME=ayana
DB_POOL_LIMIT=10
```

Observações:
- `DISCORD_TOKEN`: token da aba **Bot** no Discord Developer Portal.
- `GUILD_ID`: ID do servidor para sincronização rápida dos comandos (opcional, mas recomendado).
- `DONO_ID`: seu ID de usuário no Discord (opcional, usado como `owner_id` do bot).
- `DB_HOST`/`DB_PORT`: host e porta do MySQL.
- `DB_USER`/`DB_PASSWORD`: credenciais do usuário MySQL.
- `DB_NAME`: nome do banco que sera usado pelo bot.
- `DB_POOL_LIMIT`: limite maximo de conexoes no pool.
- Se o banco informado em `DB_NAME` nao existir, o bot cria automaticamente na inicializacao.
- O bot tambem cria/atualiza automaticamente as tabelas `warnings`, `guild_settings`, `infractions` e `user_levels`.

## Execução
```bash
python main.py
```

## Estrutura do projeto
```txt
ayana-bot/
├── cogs/
│   ├── leveling.py
│   ├── moderation.py
│   ├── nekosia.py
│   ├── welcome.py
│   └── utility.py
├── warn_store.py
├── .env.example
├── .gitignore
├── main.py
├── requirements.txt
└── README.md
```

## Permissões recomendadas do bot
Para os comandos de moderação funcionarem corretamente, conceda ao bot:
- Manage Messages
- Read Message History
- Manage Channels
- Manage Guild
- Kick Members
- Ban Members
- Moderate Members
- View Channel + Send Messages (nos canais de log)

## Intents
- Ative `MESSAGE CONTENT INTENT` no Discord Developer Portal para o AutoMod funcionar.
- Ative `SERVER MEMBERS INTENT` para funcionalidades de welcome e cargos em massa funcionarem em todos os membros.

## Logs e erros
- Os logs são gravados no console e em `logs/bot.log` (com rotação automática).
- Erros comuns de permissão/uso em DM/checks são tratados com resposta amigável.
- Erros inesperados são registrados com stack trace no log.
