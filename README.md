# Ayana Bot (Discord)

Bot Discord em Python com comandos *slash* organizados em **cogs**, moderação básica, logs e tratamento global de erros.

## Funcionalidades
- Estrutura modular com cogs
- Comandos utilitários:
  - `/help`
  - `/ping`
  - `/userinfo [membro]`
  - `/serverinfo`
- Comandos de moderação:
  - `/clear <quantidade>`
  - `/kick <membro> [motivo]`
  - `/ban <membro> [motivo]`
  - `/restaurar` (somente dono do sistema)
- Logs no terminal e em arquivo (`logs/bot.log`)
- Tratamento global de erros para comandos *slash*

## Requisitos
- Python 3.10+
- Conta e aplicação no Discord Developer Portal

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
```

Observações:
- `DISCORD_TOKEN`: token da aba **Bot** no Discord Developer Portal.
- `GUILD_ID`: ID do servidor para sincronização rápida dos comandos (opcional, mas recomendado).
- `DONO_ID`: seu ID de usuário no Discord (opcional, usado como `owner_id` do bot).

## Execução
```bash
python main.py
```

## Estrutura do projeto
```txt
ayana-bot/
├── cogs/
│   ├── moderation.py
│   └── utility.py
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
- Kick Members
- Ban Members

## Logs e erros
- Os logs são gravados no console e em `logs/bot.log` (com rotação automática).
- Erros comuns de permissão/uso em DM/checks são tratados com resposta amigável.
- Erros inesperados são registrados com stack trace no log.
