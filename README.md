# 🌸 Ayana Bot

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Discord.py](https://img.shields.io/badge/discord.py-2.4.0-blue.svg)](https://discordpy.readthedocs.io/en/stable/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MySQL](https://img.shields.io/badge/MySQL-8.0%2B-orange.svg)](https://www.mysql.com/)

Um bot multifuncional para Discord desenvolvido em Python, focado em moderação avançada, sistema de níveis com interface gráfica (Canvas) e integração com APIs de imagens.

---

## 🚀 Funcionalidades Principais

### 🛡️ Moderação & AutoMod
- **Sistema de Warns Persistente**: Avisos armazenados em MySQL com expiração configurável.
- **Escalonamento Automático**: Punições automáticas (Timeout/Ban) baseadas no acúmulo de avisos.
- **AutoMod Inteligente**: Proteção contra Anti-Spam, Anti-Link e Flood de menções.
- **Logs de Auditoria**: Registro detalhado de infrações e ações administrativas em canais dedicados.
- **Hierarquia de Segurança**: Verificação rigorosa de cargos para impedir abusos.

### 📈 Sistema de Níveis (Leveling)
- **XP por Mensagem**: Contabilização dinâmica de experiência com cooldown para evitar spam.
- **Rank Cards**: Geração de cartões de perfil personalizados usando Pillow (Canvas) com barra de progresso.
- **Leaderboard Visual**: Ranking do servidor renderizado em imagem de alta qualidade.
- **Persistência de Dados**: Progresso salvo de forma robusta no banco de dados.

### 🖼️ Integração NekoSia
- **Busca de Imagens**: Acesso à API NekoSia com filtros por categoria, tags e animes.
- **Filtro de Conteúdo**: Sistema inteligente que alterna entre `safe` e `suggestive` dependendo do canal (NSFW check).

### 🏠 Boas-Vindas & Utilidades
- **Welcome System**: Mensagens de entrada totalmente configuráveis.
- **Comandos Utilitários**: Informações de usuário, servidor, ping avançado e muito mais.
- **Help Dinâmico**: Menu de ajuda detalhado com busca por comandos específicos.

---

## 🛠️ Tecnologias Utilizadas

- **Linguagem**: [Python 3.10+](https://www.python.org/)
- **Framework**: [Discord.py 2.4](https://discordpy.readthedocs.io/)
- **Banco de Dados**: [MySQL](https://www.mysql.com/) / [aiomysql](https://github.com/aio-libs/aiomysql)
- **Processamento de Imagem**: [Pillow](https://python-pillow.org/) & [Pilmoji](https://github.com/dtimofeev/pilmoji)
- **Outros**: `aiohttp`, `python-dotenv`, `regex`.

---

## 📋 Pré-requisitos

- Python 3.10 ou superior.
- Instância do MySQL 8.0+.
- Token do bot no [Discord Developer Portal](https://discord.com/developers/applications).

---

## ⚙️ Configuração & Instalação

1. **Clone o repositório:**
   ```bash
   git clone https://github.com/seu-usuario/ayana-bot.git
   cd ayana-bot
   ```

2. **Crie um ambiente virtual:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/macOS
   # ou
   .\venv\Scripts\activate  # Windows
   ```

3. **Instale as dependências:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure as variáveis de ambiente:**
   Copie o arquivo `.env.example` para `.env` e preencha os campos:
   ```bash
   cp .env.example .env
   ```

   **Exemplo de `.env`:**
   ```env
   DISCORD_TOKEN=seu_token_aqui
   GUILD_ID=123456789012345678
   DONO_ID=123456789012345678
   
   # Database
   DB_HOST=localhost
   DB_USER=root
   DB_PASSWORD=sua_senha
   DB_NAME=ayana
   
   # Intents (Ative no Portal do Desenvolvedor)
   ENABLE_MEMBERS_INTENT=true
   ENABLE_MESSAGE_CONTENT_INTENT=true
   ```

---

## 📂 Estrutura do Projeto

```text
ayana-bot/
├── cogs/                # Módulos de comandos (Cogs)
│   ├── leveling.py      # Sistema de XP e Ranking
│   ├── moderation.py    # Moderação e AutoMod
│   ├── nekosia.py       # Integração com API de imagens
│   ├── utility.py       # Comandos gerais
│   └── welcome.py       # Sistema de boas-vindas
├── logs/                # Arquivos de log do sistema
├── main.py              # Ponto de entrada do bot
├── warn_store.py        # Core de persistência e lógica de avisos
├── requirements.txt     # Dependências do projeto
└── .env                 # Configurações sensíveis
```

---

## 🗄️ Persistência & Banco de Dados

O Ayana Bot utiliza **MySQL** para garantir que nenhuma informação seja perdida em reinicializações. O esquema é criado automaticamente na primeira execução:

- **`guild_settings`**: Armazena configurações individuais por servidor (canais de log, limites de warn, AutoMod).
- **`warnings`**: Registro de avisos, incluindo data, moderador e motivo.
- **`infractions`**: Histórico unificado de banimentos, expulsões e timeouts.
- **`user_levels`**: Controle de XP, nível e data da última mensagem para cada usuário.

---

## ⚙️ Customização Avançada

O bot oferece comandos extensivos de configuração para administradores:

- **Sistema de Avisos**:
  - `/setwarnpolicy`: Defina o limite de warns para timeout e banimento automático.
  - `/setwarnexpiration`: Configure em quantos dias um aviso expira.
- **AutoMod**:
  - `/setautomod`: Ative/Desative proteção contra spam, links e flood.
  - `/addbypassrole`: Defina cargos que ignoram as restrições do AutoMod.
- **Boas-Vindas**:
  - `/setwelcome`: Customize mensagens usando placeholders como `{user_mention}`, `{guild_name}` e `{member_count}`.
  - `/welcomesettings`: Ajuste o canal de envio e o uso de auto-roles.

---

## 🎮 Comandos Disponíveis

| Comando | Categoria | Descrição |
| :--- | :--- | :--- |
| `/help` | Utilitários | Lista todos os comandos ou detalhes de um específico. |
| `/rank` | Nível | Mostra seu cartão de nível e XP atual. |
| `/leaderboard`| Nível | Exibe o ranking de XP do servidor em imagem. |
| `/kick` | Moderação | Expulsa um membro do servidor. |
| `/ban` | Moderação | Bane permanentemente um usuário. |
| `/timeout` | Moderação | Silencia um membro temporariamente. |
| `/warn` | Moderação | Aplica um aviso formal a um membro. |
| `/clear` | Moderação | Limpa mensagens do canal atual. |
| `/slowmode` | Moderação | Ajusta o cooldown de mensagens de um canal. |
| `/nekosia` | Imagens | Busca imagens variadas da API NekoSia. |
| `/serverinfo` | Utilitários | Exibe informações técnicas do servidor. |

> *Para uma lista completa e detalhada, utilize `/help` dentro do Discord.*

---

## 🔐 Permissões Recomendadas

Para o pleno funcionamento de todos os sistemas, o bot necessita das seguintes permissões:
- `Manage Messages`, `Moderate Members`, `Kick Members`, `Ban Members`.
- `Manage Channels` (para logs), `View Audit Log`.
- `Embed Links`, `Attach Files`, `Read Message History`.

---

## 📝 Licença

Este projeto está sob a licença **MIT**. Veja o arquivo [LICENSE](LICENSE) para mais detalhes.

---
<p align="center">Desenvolvido com ❤️ por @Kaikybrofc</p>
