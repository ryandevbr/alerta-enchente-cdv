# Alerta Enchente CDV

## Documentacao Tecnica — Sistema de Apoio à Decisão

**Versao:** 3.1
**Data:** 26 de setembro de 2026
**Autor:** Ryan Lucas de Freitas Martins (ryandevbr)
**Projeto:** Sistema de Alerta Antecipado de Cheias do Rio Piracicaba
**Localidade:** Cachoeira do Vale, Timoteo (MG)
**Licenca:** MIT

---

## Sumario

1. Visao Geral
2. O Problema e a Solucao
3. Arquitetura do Sistema
4. Fontes de Dados
5. Estudo Hidrologico
6. Componentes do Sistema
7. Banco de Dados
8. Scripts Python
9. Edge Functions
10. Frontend
11. Automacao
12. Seguranca
13. Manual de Operacao
14. Guia de Replicacao
15. Estado Atual e Proximos Passos
16. Licenca e Creditos
17. Anexos

---

## 1. Visao Geral

### 1.1 O que e

O Alerta Enchente CDV e um sistema de alerta antecipado de cheias do Rio Piracicaba, focado em proteger a comunidade de Cachoeira do Vale, bairro de Timoteo (MG). O sistema monitora em tempo real estacoes fluviometricas da bacia e a operacao da barragem da UHE Sa Carvalho (CEMIG), oferecendo ate 10 horas de antecedencia antes que uma onda de cheia chegue ao bairro.

### 1.2 Componentes principais

| Componente | Funcao |
|---|---|
| Site publico | Informa a populacao em tempo real (4 abas) |
| Chat comunitario | Moradores trocam informacoes durante eventos |
| Painel de moderacao | Moderadores modera o chat e vê metricas |
| Bot Telegram | Alertas automaticos, moderacao remota e disparo de workflows |
| Pipeline de dados | Ingestao ANA e CEMIG, calculo de cache, alertas |
| Monitoramento de saude | Avisa quando algum componente para de funcionar |

### 1.3 Estado atual

Sistema 100% operacional em nuvem, sem dependencia de computador local. Todos os scripts rodam em GitHub Actions, disparaveis via Telegram em 1 toque. Frontend hospedado na Vercel. Backend no Supabase.

---

## 2. O Problema e a Solucao

### 2.1 O problema

A comunidade de Cachoeira do Vale sofre com cheias periodicas do Rio Piracicaba. A unica ferramenta disponivel hoje e o aplicativo PROX da CEMIG, que:

- Nao antecipa — avisa quando o rio ja esta na cota de inundacao
- Nao da tempo para a populacao tirar moveis, documentos ou evacuar

Isso significa que moradores so descobrem a cheia quando ela ja esta na porta de casa.

### 2.2 A solucao

O Alerta Enchente CDV entrega antecedencia real:

1. Monitora Nova Era (montante, cerca de 90 km rio acima) — 10h de antecedencia
2. Monitora a UHE Sa Carvalho (barragem intermediaria) — 4h de antecedencia
3. Dispara alertas no Telegram e no site assim que o nivel cruza limiares criticos
4. Da contexto via comparativo historico e chat comunitario

---

## 3. Arquitetura do Sistema

### 3.1 Diagrama geral

```
Fontes externas                GitHub Actions
-----------------              --------------
API ANA          ----->  atualizador_tempo_real.py  ----->  historico_ana
Site CEMIG       ----->  coletor_cemig.py           ----->  historico_cemig
                                                              |
                                                              v
                                              atualizador_cache_site.py  ----->  cache_site
                                                              |
                                                 -------------+-------------
                                                 |                         |
                                                 v                         v
                                       alertas_qualitativos.py    monitor_saude.py
                                                 |                         |
                                                 +------------+------------+
                                                              |
                                                              v
                                                           Telegram

                             Supabase
                             --------
                historico_ana    historico_cemig    cache_site
                chat_mensagens   chat_admins        alertas_estado
                push_subscriptions                  sync_status

                             Vercel
                             ------
                index_base.html  ----->  Site publico (4 abas)
                moderacao.html   ----->  Painel de moderacao e metricas
                manifest.json, sw.js  -->  PWA
```

### 3.2 Fluxo de dados

Ciclo completo (disparo manual via Telegram ou cron futuro):

```
1. atualizador_tempo_real.py  ->  historico_ana
   (ingestao de Nova Era, Timoteo, Guilman, Sa Carvalho)

2. coletor_cemig.py  ->  historico_cemig
   (coleta de defluencia, afluencia, volume util)

3. atualizador_cache_site.py  ->  cache_site
   - status_atual      (nivel Timoteo + CEMIG)
   - onda_desfasada    (projecao do slider)
   - comparativo_anual (nivel em outros anos)
   - defluencia_48h    (serie temporal)

4. alertas_qualitativos.py  ->  Telegram
   (verifica faixas de nivel e defluencia)

5. monitor_saude.py  ->  Telegram
   (verifica se os dados estao frescos)
```

---

## 4. Fontes de Dados

### 4.1 ANA — Agencia Nacional de Aguas

**API:** HidroWebService
**Autenticacao:** Token JWT via credenciais (validade 60 min)
**Endpoint principal:** `HidroinfoanaSerieTelemetricaDetalhada/v1`

Estacoes monitoradas:

| Codigo | Nome | Papel | Intervalo |
|---|---|---|---|
| 56661000 | Nova Era | Montante principal | 15 min |
| 56696000 | Mario de Carvalho (Timoteo) | Jusante, referencia | 15 min |
| 56675080 | UHE Guilman Jusante | Intermediaria | 60 min |
| 56688080 | Sa Carvalho Barramento | Reservatorio | 60 min |

Dados extraidos: `data_hora`, `nivel_cm`, `chuva_mm`, `vazao_m3s`

Particularidades da API:

- Parametro `Data de Busca` e o limite superior
- `Range=DIAS_30` retorna 30 dias anteriores a data informada
- Nao expoe flag de qualidade dos dados

### 4.2 CEMIG — UHE Sa Carvalho

**Fonte:** Coleta automatizada do site publico (Playwright + Chromium headless)
**URL:** `https://www.cemig.com.br/usinas/uhe-sa-carvalho/`

Dados extraidos:

| Dado | Unidade | Frequencia |
|---|---|---|
| Nivel do reservatorio | m | 15 min |
| Volume util | % | 15 min |
| Afluencia | m3/s | 15 min |
| Defluencia | m3/s | 15 min |
| Chuva | mm | 15 min |

Seletores HTML (por ID):

```
nivel_atual_valor       volume_util_valor
afluencia_valor         defluencia_valor
chuva_valor
```

Gatilhos operacionais oficiais (do PAE CEMIG):

| Vazao | Significado |
|---|---|
| 30 m3/s | Atencao (baseline historica cerca de 17 m3/s) |
| 550 m3/s | Vazao de restricao — CEMIG comunica População |
| 800 m3/s | Limiar historico de problemas em Coronel Fabriciano |

### 4.3 Open-Meteo (previsao do tempo)

Usado na aba "Previsao" do site. Gratuito, sem autenticacao.
Endpoint: `https://api.open-meteo.com/v1/forecast`

---

## 5. Estudo Hidrologico

### 5.1 Objetivo

Determinar o tempo que uma onda de cheia leva para percorrer o trecho Nova Era ate Timoteo.

### 5.2 Metodos testados

| Metodo | Resultado | Aprovacao |
|---|---|---|
| Correlacao cruzada (niveis absolutos) | 8h | Enviesado |
| Correlacao em primeiras diferencas (Delta nivel) | 12h | Aprovado |
| Correlacao restrita a estacao chuvosa (dez-fev) | 10h (r=0.9655) | Melhor metodo |
| Pico-a-pico (medida direta) | 11h | Confirmacao |
| Regressao Ridge (features contemporaneas) | RMSE cerca de 50 cm | Reprovado |
| Metodo da variacao (Delta T = alfa x Delta NE) | RMSE cerca de 35 cm | Reprovado |
| Previsao de Delta T com OLS | RMSE cerca de 35 cm | Reprovado |

### 5.3 Conclusao

Tempo de transito oficial: 9 a 10 horas (mediana 10h).

A previsao numerica de nivel em Timoteo e inviavel com os dados disponiveis, porque:

1. Existem 3 barragens entre Nova Era e Timoteo (Guilman, Sa Carvalho, Cocais Grande) que regulam o fluxo
2. A decisao humana de abrir comportas nao esta nos dados
3. Ha contribuicao de chuva local entre as estacoes

Implicacao: o sistema migrou de "previsao em cm" para alertas qualitativos por faixa.

### 5.4 Topologia da bacia

```
Santa Barbara ---+
                 +---> Sao Goncalo do Rio Abaixo ---+
Rio Piracicaba --+                                   |
                                                     +---> NOVA ERA
Joao Monlevade --------------------------------------+
                                                          |
                                                          v
                                                 UHE GUILMAN AMORIM
                                                          |
                                                          v
                                                 UHE SA CARVALHO
                                                 (Antonio Dias + Severo)
                                                          |
                                                          v
                                                     ANTONIO DIAS
                                                          |
                                                          v
                                                       TIMOTEO
```

---

## 6. Componentes do Sistema

### 6.1 Site publico (index_base.html)

**Hospedagem:** Vercel
**URL:** `https://alertaenchetecdv.app`
**Tecnologias:** HTML, Tailwind CSS, JavaScript puro, Mapbox GL, Chart.js

Abas:

| Aba | Funcao |
|---|---|
| Entenda | Manual do usuario: como funciona o sistema, niveis de alerta, canais |
| A Caminho | Mapa 3D, paineis (SAD, CEMIG), comparacao historica |
| Previsao | Grafico de precipitacao (12h) via Open-Meteo |
| Chat | Chat comunitario em tempo real |

Funcionalidades:

- Painel SAD: nivel atual de Timoteo com badge dinamico (normal, alerta, critico)
- Painel CEMIG: afluencia, defluencia, volume util com cores por faixa
- Sparkline 48h: mini-grafico de defluencia no painel desktop
- Comparativo: modal com nivel em outros anos
- Dica de onboarding: aparece na primeira visita
- PWA: instalavel no celular (Fase 1 completa)

### 6.2 Painel de moderacao (moderacao.html)

**Hospedagem:** Vercel
**Autenticacao:** Supabase Auth com validacao em `chat_admins`

Abas:

| Aba | Funcao |
|---|---|
| Fila de Moderacao | Mensagens pendentes |
| Reportadas | Mensagens com denuncias |
| Historico | Mensagens rejeitadas |
| Metricas | Dashboard de engajamento |

Atalhos de teclado: A (aprovar), R (rejeitar), S (pular).

Dashboard de metricas:

- KPIs: total, taxa de aprovacao, autores unicos, reportadas
- Grafico de mensagens por dia (barras empilhadas)
- Doughnut de distribuicao por status
- Distribuicao por hora do dia
- Top 10 ruas por volume
- Filtro de periodo: 7, 30 ou 90 dias

### 6.3 Chat comunitario

**Backend:** Supabase Realtime (WebSocket)
**Cadastro:** local (nome e rua em `localStorage`, sem login)

Moderacao:

- Filtro de palavras inadequadas (trigger SQL)
- Filtro de fake news (retencao automatica)
- Rate limit (1 mensagem a cada 20 segundos por IP)
- Botao de denuncia com auto-rejeicao em 3 denuncias
- Moderacao pelo painel web ou Telegram

Contador de usuarios online: Supabase Presence.

---

## 7. Banco de Dados

### 7.1 Tabelas

| Tabela | Descricao |
|---|---|
| historico_ana | Leituras ANA (nivel, chuva, vazao) |
| historico_cemig | Dados operacionais Sa Carvalho |
| cache_site | Cache consumido pelo frontend (4 chaves) |
| chat_mensagens | Mensagens do chat |
| chat_admins | Moderadores autorizados |
| alertas_estado | Estado anti-spam de alertas |
| sync_status | Health check das execucoes |
| push_subscriptions | Subscriptions de push (PWA) |

### 7.2 Chaves em cache_site

| Chave | Conteudo |
|---|---|
| status_atual | Nivel Timoteo e dados CEMIG |
| onda_desfasada | Projecao para o slider |
| comparativo_anual | Nivel em outros anos |
| defluencia_48h | Serie para sparkline |

### 7.3 RPCs principais

| RPC | Funcao |
|---|---|
| enviar_mensagem | Insere mensagem (bypass RLS) |
| reportar_mensagem | Registra denuncia |
| is_chat_admin | Verifica permissao |
| registrar_push_subscription | Salva subscription |
| metricas_kpis | KPIs do dashboard |
| metricas_mensagens_por_dia | Serie temporal |
| metricas_top_ruas | Top ruas |
| metricas_por_hora | Distribuicao horaria |

### 7.4 Triggers

| Trigger | Funcao |
|---|---|
| fn_chat_filtro_conteudo | Palavras inadequadas e fake news |
| fn_chat_rate_limit | 1 mensagem a cada 20 segundos por IP |
| fn_auto_rejeitar_reportada | Auto-rejeicao em 3 denuncias |
| fn_notify_telegram | Notifica via pg_net |

---

## 8. Scripts Python

### 8.1 Scripts em producao

| Arquivo | Funcao |
|---|---|
| atualizador_tempo_real.py | Ingestao de dados ANA |
| coletor_cemig.py | Coleta automatizada da CEMIG |
| atualizador_cache_site.py | Calculo do cache |
| alertas_qualitativos.py | Alertas por faixa |
| monitor_saude.py | Health check |

### 8.2 Arquitetura de cada script

**atualizador_tempo_real.py**

- Autentica na ANA (token JWT)
- Para cada estacao, busca o ultimo registro
- Loop recuando 30 dias por bloco
- Upsert em lotes de 1000

**coletor_cemig.py**

- Playwright com Chromium headless
- Extrai dados via seletores por ID
- Normaliza numeros brasileiros (virgula)
- Upsert em `historico_cemig`

**atualizador_cache_site.py**

- Gera as 4 chaves do cache
- status_atual, onda_desfasada, comparativo_anual, defluencia_48h

**alertas_qualitativos.py**

- Verifica faixas de nivel (ANA) e defluencia (CEMIG)
- Notifica no Telegram ao mudar de faixa
- Anti-spam por cooldown (30 min)

**monitor_saude.py**

- Verifica se os dados estao frescos
- Notifica quando algum componente fica atrasado ou parado

### 8.3 Requirements

```
requests>=2.32.0
supabase>=2.9.0
python-dotenv>=1.0.1
pandas>=2.2.0
numpy>=1.26.0
matplotlib>=3.8.0
scikit-learn>=1.4.0
playwright>=1.45.0
```

---

## 9. Edge Functions

### 9.1 telegram-webhook

**Funcao:** receber callbacks do bot Telegram.

Casos de uso:

1. Moderacao de chat: aprovar, rejeitar, limpar denuncia
2. Disparo de workflows: `/menu` abre painel com botoes para rodar ANA, CEMIG, cache ou tudo

**Seguranca:** validacao do header `X-Telegram-Bot-Api-Secret-Token`.

### 9.2 send-push (parcialmente implementado)

**Funcao:** enviar notificacoes push (Fase 2 do PWA).

**Estado atual:** infraestrutura pronta, autenticacao funcionando, envio precisa ser validado com uma subscription nova.

**Autenticacao:** header `X-Push-Secret` (secret proprio do projeto).

---

## 10. Frontend

### 10.1 Design system

- Cor base: `#0f172a` (azul-marinho escuro)
- Accent: azul (`#3b82f6`), laranja (`#fb923c`), verde, vermelho
- Paineis: glass-panel com backdrop-filter blur
- Tipografia: system fonts e Tailwind

### 10.2 Identidade visual

- Logo: circular com onda, casa e triangulo de alerta
- Favicon: mesma logo
- Open Graph: metatags para preview no WhatsApp

### 10.3 Estrutura do repositorio

```
Alerta Enchente - CDV/
├── .github/workflows/
│   └── atualizador.yml
├── docs/
│   ├── DOCUMENTACAO.md
│   ├── estudos/
│   │   ├── backfill_historico.py
│   │   ├── calculos_vr.py
│   │   ├── exploracao_dados.ipynb
│   │   ├── previsao_clima.py
│   │   └── validar_metodo_variacao.py
│   ├── ferramentas/
│   │   ├── formatador_geojson.py
│   │   └── servidor_local.py
│   └── imagens/
├── supabase/functions/
│   ├── telegram-webhook/
│   └── send-push/
├── atualizador_tempo_real.py
├── coletor_cemig.py
├── atualizador_cache_site.py
├── alertas_qualitativos.py
├── monitor_saude.py
├── index_base.html
├── moderacao.html
├── manifest.json
├── sw.js
├── logo.png
├── img_normal.jpg
├── img_alerta.jpg
├── img_critico.jpg
├── requirements.txt
├── README.md
├── .gitignore
├── .env.example
└── .env (nao versionado)
```

---

## 11. Automacao

### 11.1 Workflow do GitHub Actions

**Arquivo:** `.github/workflows/atualizador.yml`

Jobs:

```
atualizar-ana ---+
                 +-- atualizar-cache
coletar-cemig ---+
                 +-- enviar-alertas
                 +-- monitor-saude
```

**Estado atual:** cron comentado, dispara apenas via `workflow_dispatch` (manual ou via Telegram).

Motivo: repositorio privado consome minutos do free tier. Quando virar publico, descomentar o `schedule`.

### 11.2 Comandos Telegram disponiveis

No grupo da Moderação, enviar:

```
/menu
```

Resposta:

```
Painel de controle

[ Rodar tudo ]
[ So ANA ]  [ So CEMIG ]
[ So cache ]
```

### 11.3 Fluxo de disparo

```
Telegram -> /menu -> botao "Rodar tudo"
       |
       v
Edge Function telegram-webhook
       |
       v
GitHub API (workflow_dispatch)
       |
       v
GitHub Actions executa os jobs em sequencia
       |
       v
cache_site atualizado
       |
       v
Site publico reflete dados novos
```

---

## 12. Seguranca

### 12.1 Protecoes implementadas

| Vetor | Mitigacao |
|---|---|
| XSS no chat | `escapeHtml()` em todo conteudo dinamico |
| Spam | Rate limit (1 mensagem a cada 20 segundos por IP) |
| Flood de denuncias | Auto-rejeicao em 3 denuncias |
| Fake news | Trigger de retencao |
| Impersonacao | RLS e validacao de tipo |
| Acesso ao painel | Supabase Auth com tabela `chat_admins` |
| Webhook forjado | Secret token no header |
| Push malicioso | Secret proprio (`X-Push-Secret`) |

### 12.2 Boas praticas

- Nunca expor `service_role_key` no frontend
- Nunca commitar `.env` no Git
- Rotacionar tokens se houver suspeita de vazamento
- Revisar `chat_admins` trimestralmente
- Auditar logs das Edge Functions mensalmente

### 12.3 LGPD

- IP armazenado como hash SHA-256 (nao reversivel)
- Nome e rua fornecidos voluntariamente
- Dados do chat salvos apenas no dispositivo (localStorage)
- Mensagens antigas podem ser apagadas periodicamente

### 12.4 Aviso legal e responsabilidade

O sistema opera como ferramenta de apoio à decisão, não como autoridade oficial de alerta. Toda notificação emitida (site, Telegram, push) carrega o seguinte aviso:

> Ferramenta de apoio à decisão. A evacuação é decisão exclusiva da Defesa Civil. Emergência: 199 ou 193.

Essa medida cumpre três objetivos:

1. Alinhamento legal com o papel institucional da Defesa Civil Municipal
2. Redução de exposição a responsabilização por decisões tomadas a partir de alertas automáticos
3. Reforço ao usuário sobre o canal correto em situações de emergência real

O texto completo do aviso está disponível na aba "Entenda" do site público e no rodapé de todas as páginas.

---

## 13. Manual de Operacao

### 13.1 Atualizar o site (manual)

1. Abrir Telegram
2. Enviar `/menu` no grupo da Moderação
3. Clicar em "Rodar tudo"
4. Aguardar cerca de 2 minutos
5. Recarregar o site

### 13.2 Adicionar moderador

1. Criar usuario em: Supabase Dashboard, Authentication, Users, Add user
2. Copiar UUID
3. Rodar SQL:

```sql
INSERT INTO chat_admins (user_id, nome)
VALUES ('<uuid>', 'Nome do Moderador');
```

### 13.3 Aprovar ou rejeitar via SQL

```sql
-- Aprovar
UPDATE chat_mensagens SET status = 'aprovado' WHERE id = 123;

-- Ver fila
SELECT id, autor_nome, autor_rua, mensagem, reportado_count
FROM chat_mensagens
WHERE status = 'pendente' OR reportado = TRUE
ORDER BY created_at DESC;
```

### 13.4 Limpar dados antigos

```sql
-- Mensagens com mais de 30 dias
DELETE FROM chat_mensagens
WHERE created_at < NOW() - INTERVAL '30 days';

-- Historico CEMIG com mais de 1 ano
DELETE FROM historico_cemig
WHERE data_hora < NOW() - INTERVAL '1 year';
```

### 13.5 Ativar cron automatico (apos tornar publico)

Descomentar no `.github/workflows/atualizador.yml`:

```yaml
schedule:
  - cron: '0,30 * * * *'         # ANA
  - cron: '0,15,30,45 * * * *'   # CEMIG
  - cron: '5,35 * * * *'         # Cache
```

### 13.6 Resetar estados de alerta

```sql
-- Resetar alerta de cota
UPDATE alertas_estado
SET ultima_faixa = 'normal', notificado_em = NULL
WHERE estacao = '56696000';

-- Resetar monitor de saude
DELETE FROM alertas_estado WHERE estacao LIKE 'saude_%';
```

### 13.7 Backfill de nova estacao

1. Editar `ESTACOES_BACKFILL` em `docs/estudos/backfill_historico.py`
2. Rodar localmente: `python docs/estudos/backfill_historico.py`
3. Adicionar codigo em `ESTACOES` no `atualizador_tempo_real.py`

---

## 14. Guia de Replicacao

### 14.1 O que precisa ser trocado

| Item | Onde | O que trocar |
|---|---|---|
| Codigos ANA | `atualizador_tempo_real.py`, `atualizador_cache_site.py`, `alertas_qualitativos.py` | Codigos das estacoes da nova bacia |
| Codigo CEMIG | `coletor_cemig.py` | URL da usina, se houver barragem |
| Limiares de nivel | `alertas_qualitativos.py`, `index_base.html` | 780/890 cm pelos valores da nova regua |
| Limiares de defluencia | `alertas_qualitativos.py` | 30, 100 e 550 m3/s |
| Lag de transito | `atualizador_cache_site.py`, `index_base.html` | Novo valor, calculado com o estudo |
| Coordenadas Mapbox | `index_base.html` | Centro do novo bairro |
| GeoJSON da mancha | `index_base.html` | Poligonos da nova area de risco |
| Logo | `logo.png` | Nova identidade visual |
| Textos do site | `index_base.html`, aba Entenda | Nomes de bairros, rios, barragens |
| Credenciais | `.env`, GitHub Secrets, Supabase Secrets | Todas as chaves |

### 14.2 Checklist de criacao

```
[ ] 1.  Criar conta Supabase e novo projeto
[ ] 2.  Rodar todos os SQLs (tabelas, RPCs, triggers, RLS)
[ ] 3.  Configurar Realtime nas tabelas necessarias
[ ] 4.  Criar bot Telegram via BotFather
[ ] 5.  Configurar Edge Functions (telegram-webhook, send-push)
[ ] 6.  Configurar secrets no Supabase
[ ] 7.  Criar repositorio GitHub privado
[ ] 8.  Configurar secrets no GitHub Actions
[ ] 9.  Fazer deploy do site na Vercel
[ ] 10. Fazer deploy do painel na Vercel
[ ] 11. Configurar dominio personalizado
[ ] 12. Rodar backfill inicial dos dados historicos
[ ] 13. Testar todos os alertas
[ ] 14. Testar moderacao via Telegram
[ ] 15. Divulgar para a comunidade
```

---

## 15. Estado Atual e Proximos Passos

### 15.1 Funcionalidades entregues

| Item | Status |
|---|---|
| Ingestao ANA | Concluido |
| Ingestao CEMIG | Concluido |
| Cache do site | Concluido |
| Alertas automaticos | Concluido |
| Monitoramento de saude | Concluido |
| Bot Telegram (moderacao e trigger) | Concluido |
| Site publico (4 abas) | Concluido |
| Chat comunitario | Concluido |
| Painel de moderacao | Concluido |
| Comparativo historico | Concluido |
| Grafico de defluencia 48h | Concluido |
| Dashboard de metricas | Concluido |
| Logo e Open Graph | Concluido |
| PWA — Fase 1 (instalavel) | Concluido |
| PWA — Fase 2 (push) | Parcial |
| README | Concluido |
| Limpeza do repositorio | Concluido |

### 15.2 Roadmap pendente

| Item | Esforco |
|---|---|
| Finalizar push notifications (Fase 3) | 30 min |
| Tornar repositorio publico | 15 min |
| Divulgar para a comunidade e coletar feedback | Continuo |

### 15.3 Pendencias conhecidas

- VAPID keys precisam ser validadas em uma nova subscription
- Repositorio ainda privado durante o desenvolvimento
- Cron automatico ainda desligado (rodando manual via Telegram)

---

## 16. Licenca e Creditos

### 16.1 Licenca

Este projeto e distribuido sob a licenca MIT.

Voce pode:

- Usar comercialmente
- Modificar
- Distribuir
- Sublicenciar
- Usar em projetos privados

Voce deve:

- Manter o aviso de copyright original

Voce nao pode:

- Responsabilizar o autor por danos

### 16.2 Creditos

Autor: Ryan Lucas de Freitas Martins (ryandevbr)
Fontes de dados: ANA (Agencia Nacional de Aguas) e CEMIG

### 16.3 Agradecimentos

- Equipe da CEMIG pelo PAE publico
- Equipe da ANA pelo HidroWebService
- Comunidade de Cachoeira do Vale pelo feedback

---

## 17. Anexos

### 17.1 Glossario

| Termo | Significado |
|---|---|
| ANA | Agencia Nacional de Aguas |
| CEMIG | Companhia Energetica de Minas Gerais |
| Defluencia | Vazao de agua que sai de uma barragem (m3/s) |
| Afluencia | Vazao que chega a um reservatorio (m3/s) |
| PAE | Plano de Acao de Emergencia |
| Qr | Vazao de restricao (550 m3/s) |
| TR | Tempo de Retorno |
| Lag | Tempo de transito da onda (h) |
| UHE | Usina Hidreletrica |
| PCH | Pequena Central Hidreletrica |
| ZAS | Zona de Autossalvamento |
| RLS | Row Level Security (Supabase) |
| RPC | Remote Procedure Call (funcao SQL) |

### 17.2 Comandos uteis

**Supabase CLI**

```powershell
supabase functions deploy <nome> --no-verify-jwt
supabase functions list
supabase secrets set CHAVE=valor
supabase secrets list
```

**Git**

```powershell
git add .
git commit -m "mensagem"
git push origin main

# Forcar workflow manual (se tiver gh instalado)
gh workflow run atualizador.yml
```

**Telegram**

```powershell
# Ver webhook
Invoke-RestMethod -Uri "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"

# Re-registrar webhook
Invoke-RestMethod -Uri "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<URL>&secret_token=<SECRET>"
```

### 17.3 Variaveis de ambiente

```env
# Supabase
SUPABASE_URL=
SUPABASE_KEY=

# ANA
ANA_USER=
ANA_PASS=

# Telegram
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_WEBHOOK_SECRET=

# GitHub
GITHUB_TOKEN=
GITHUB_REPO=
GITHUB_WORKFLOW=

# Push (VAPID)
VAPID_PUBLIC_KEY=
VAPID_PRIVATE_KEY=
VAPID_SUBJECT=
PUSH_SECRET=

# Mapbox
MAPBOX_KEY=
```

### 17.4 Links uteis

- Site em producao: https://alertaenchetecdv.app
- Repositorio: https://github.com/ryandevbr/Alerta_Enchente_CDV
- Supabase Dashboard: https://supabase.com/dashboard
- Vercel Dashboard: https://vercel.com/dashboard
- PAE CEMIG: https://www.cemig.com.br/wp-content/uploads/2025/07/PAE_UHE_Sa_Carvalho_revG.pdf
- Portal ANA: https://www.snirh.gov.br/hidrotelemetria/

---

## Fim da documentacao

Versao: 3.1
Data: 26 de setembro de 2026
Proxima revisao: quando o repositorio for tornado publico

---

Nota sobre manutencao: este documento deve ser atualizado sempre que:

- Uma nova funcionalidade for adicionada
- Um limiar ou parametro for alterado
- A infraestrutura mudar (Supabase, Vercel, GitHub)
- Uma nova integracao for implementada
