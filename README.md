# Feeder: regulatório + setores

Robô que roda 2x ao dia (8h e 17h), varre ANEEL, ARSESP, AGEPAR, ARSAE-MG, o Diário Oficial da União e a imprensa, filtra pelo que importa (revisão tarifária, reajuste, metodologia/WACC, encargos/CDE, curtailment, consultas públicas, empresas da sua cobertura) e te manda **só o que é novo**, por e-mail e/ou Telegram. Tudo fica consolidado numa **página única** (`docs/index.html`), atualizada a cada execução, com os últimos 7 dias e duas abas:

- **Regulatório**: atos e notícias das agências, com a contagem diária por agência, filtros por relevância, tema e agência, busca e marcação de lidos.
- **Setores**: notícias de Utilities, Bancos, Non Banks, Agro (São Martinho, Cosan, 3tentos, SLC, açúcar e etanol, grãos), Combustíveis e Bancos LatAm (Chile, Argentina, Colômbia, México e Peru, buscados em espanhol local). Cada setor é dividido em grupos (empresa, tema ou país), e notícias com resultado, guidance, M&A, rating, dividendos, troca de gestão etc. aparecem como destaque. A "Visão geral" mostra as principais de cada setor.

O e-mail traz tudo do regulatório e só os **destaques** dos setores, para não lotar a caixa. O histórico completo também fica em `data/historico.csv` (abre direto no Excel).

## Instalação (uns 15 minutos, uma vez só)

1. **Crie um repositório privado** no GitHub e suba estes arquivos (pode arrastar a pasta inteira em "Add file → Upload files"). Confira que a pasta `.github/workflows` foi junto.
2. **Configure o e-mail.** Com Gmail: ative a verificação em duas etapas e crie uma *senha de app* em myaccount.google.com/apppasswords.
3. No repositório, vá em **Settings → Secrets and variables → Actions → New repository secret** e crie:

| Secret | Exemplo |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `seuemail@gmail.com` |
| `SMTP_PASSWORD` | a senha de app (16 letras) |
| `EMAIL_TO` | `voce@empresa.com` (vários: separe por vírgula) |

   Opcionais:
   - `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`: crie um bot com o @BotFather, mande uma mensagem pra ele e pegue o chat_id em `https://api.telegram.org/bot<TOKEN>/getUpdates`.
   - `ANTHROPIC_API_KEY`: ativa um resumo de 1 frase por item explicando por que importa (custo de centavos por dia).

4. Vá em **Actions → Feeder regulatório → Run workflow**. A primeira execução só memoriza o que já existe nos sites e te manda um e-mail "Feeder ativado". A partir da segunda, chega só o que for novo.

## Publicar a página do feed (GitHub Pages)

1. No repositório: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
2. Rode o robô uma vez (**Actions → Feeder → Run workflow**). No fim da execução, o endereço da página aparece no resumo do job, algo como `https://seu-usuario.github.io/feeder/`.
3. Para o e-mail e o Telegram trazerem um botão "Abrir o feed completo", vá em **Settings → Secrets and variables → Actions → aba Variables** e crie `FEED_URL` com esse endereço.
4. No celular, abra o endereço e use "Adicionar à tela de início" (Safari: botão de compartilhar; Chrome: menu ⋮) para ter um ícone como se fosse um app.

Sobre privacidade: no plano gratuito do GitHub, o Pages só funciona com repositório **público**. Como o feed só reúne informação pública (atos e notícias), isso costuma ser aceitável. Se preferir manter privado, o GitHub Pro (cerca de US$ 4/mês) libera Pages em repositório privado, ou você pode simplesmente baixar e abrir o `docs/index.html` no navegador.

Os itens marcados como lidos ficam salvos no seu navegador; em outro computador ou no celular a marcação começa do zero.

## Personalizar

Tudo fica no `config.yaml`:
- **Temas e palavras-chave** (`themes`): acrescente termos; acento e maiúsculas não importam. Coloque as empresas da sua cobertura em `empresas`.
- **Ruído** (`exclude`): termos que descartam o item.
- **Setores** (`news` no fim do arquivo): para cobrir uma empresa nova, acrescente o nome em `terms` do grupo certo, ou crie um grupo. `match` define o que precisa aparecer no título; termos com inicial maiúscula diferenciam maiúsculas ("Rumo" não pega "rumo a"). `highlight` define o que vira destaque e `exclude` o que é descartado.
- **Agências** (`sources`): há modelos comentados no final da parte regulatória. Para uma agência nova, basta a URL da página de notícias ou de atos normativos.
  - `require_match: false` = manda tudo que for novo daquela fonte (bom pra agências estaduais, que publicam pouco).
  - `selector`: se uma página trouxer muito link irrelevante, limite a área com um seletor CSS (ex.: `"#conteudo .lista-noticias"`).

## Testar

```bash
pip install -r requirements.txt
python feeder.py --test-sources   # mostra quantos itens cada fonte retornou
python feeder.py --dry-run        # mostra o resumo sem enviar nem memorizar e gera data/preview.html
```

## Pontos de atenção

- **Sites mudam.** Quando uma fonte falha, o e-mail traz um aviso no rodapé com o nome dela. Normalmente é só atualizar a URL no `config.yaml`.
- **Bloqueio de IP estrangeiro.** O GitHub roda em servidores fora do Brasil, e alguns sites estaduais bloqueiam esse acesso. Se uma agência falhar sempre, rode o script no seu computador (Agendador de Tarefas do Windows ou `cron`) com as mesmas variáveis de ambiente; o resto continua igual.
- **Diários oficiais estaduais** (onde saem as deliberações da ARSESP, AGEPAR etc.) não têm busca padronizada. Hoje eles são cobertos pelos sites das agências e pela imprensa; dá pra adicionar busca direta no DOE de um estado específico se for importante.
- O GitHub pode atrasar as execuções agendadas em alguns minutos.
