// supabase/functions/telegram-webhook/index.ts
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const TELEGRAM_TOKEN = Deno.env.get("TELEGRAM_TOKEN")!;
const WEBHOOK_SECRET = Deno.env.get("TELEGRAM_WEBHOOK_SECRET")!;
const SUPABASE_URL   = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE   = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const GITHUB_TOKEN   = Deno.env.get("GITHUB_TOKEN")!;
const GITHUB_REPO    = Deno.env.get("GITHUB_REPO")!;      // "user/repo"
const GITHUB_WORKFLOW= Deno.env.get("GITHUB_WORKFLOW")!;  // "atualizador.yml"

async function telegram(method: string, body: object) {
    return fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/${method}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
}

async function atualizarMensagem(
    id: number,
    patch: Record<string, unknown>
): Promise<{ ok: boolean; erro?: string }> {
    const r = await fetch(
        `${SUPABASE_URL}/rest/v1/chat_mensagens?id=eq.${id}`,
        {
            method: "PATCH",
            headers: {
                "apikey": SERVICE_ROLE,
                "Authorization": `Bearer ${SERVICE_ROLE}`,
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            body: JSON.stringify(patch),
        }
    );
    if (!r.ok) return { ok: false, erro: await r.text() };
    const linhas = await r.json();
    if (!Array.isArray(linhas) || linhas.length === 0) {
        return { ok: false, erro: "mensagem não encontrada" };
    }
    return { ok: true };
}


// GitHub — dispara workflow via API

async function dispararWorkflow(
    tipo: string
): Promise<{ ok: boolean; erro?: string }> {
    // Monta os inputs conforme o botão clicado
    let inputs: Record<string, boolean> = {};

    if (tipo === "run_ana") {
        inputs = { pular_cemig: true, pular_cache: true };
    } else if (tipo === "run_cemig") {
        inputs = { pular_ana: true, pular_cache: true };
    } else if (tipo === "run_cache") {
        inputs = { pular_ana: true, pular_cemig: true };
    } else if (tipo === "run_all") {
        inputs = {}; // tudo false, roda tudo
    } else {
        return { ok: false, erro: "tipo desconhecido" };
    }

    try {
        const r = await fetch(
            `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${GITHUB_WORKFLOW}/dispatches`,
            {
                method: "POST",
                headers: {
                    "Accept": "application/vnd.github+json",
                    "Authorization": `Bearer ${GITHUB_TOKEN}`,
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    ref: "main",
                    inputs: inputs,
                }),
            }
        );

        if (r.status === 204) {
            return { ok: true };
        }

        const erro = await r.text();
        return { ok: false, erro: `HTTP ${r.status}: ${erro.slice(0, 200)}` };
    } catch (e) {
        return { ok: false, erro: String(e) };
    }
}


// Menus do Telegram

async function enviarMenu(chatId: number) {
    await telegram("sendMessage", {
        chat_id: chatId,
        text: "<b>Painel de controle — Alerta CDV</b>\n\n" +
              "Escolha o que deseja executar:",
        parse_mode: "HTML",
        reply_markup: {
            inline_keyboard: [
                [{ text: "▶️ Rodar tudo", callback_data: "run_all" }],
                [
                    { text: "Só ANA",   callback_data: "run_ana" },
                    { text: "Só CEMIG", callback_data: "run_cemig" },
                ],
                [{ text: "Só cache", callback_data: "run_cache" }],
            ],
        },
    });
}


// Handler principal

serve(async (req) => {
    // Segurança: só aceita requisições do Telegram
    const secret = req.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (secret !== WEBHOOK_SECRET) {
        return new Response("unauthorized", { status: 401 });
    }

    try {
        const update = await req.json();

        // ---------- Comandos de texto ----------
        if (update.message && update.message.text) {
            const texto = update.message.text.trim();
            const chatId = update.message.chat.id;

            if (["/menu", "/start", "/atualizar"].includes(texto)) {
                await enviarMenu(chatId);
            }
            // (outros comandos podem ser adicionados aqui)

            return new Response("ok");
        }

        // ---------- Cliques em botões ----------
        if (!update.callback_query) {
            return new Response("ok");
        }

        const cb = update.callback_query;
        const data = cb.data || "";

        // ================= Workflow (botões de trigger) =================
        if (data.startsWith("run_")) {
            // Feedback imediato ao usuário
            await telegram("answerCallbackQuery", {
                callback_query_id: cb.id,
                text: "⏳ Disparando workflow...",
            });

            const resultado = await dispararWorkflow(data);

            const nomes: Record<string, string> = {
                run_all:   "Rodar tudo",
                run_ana:   "Só ANA",
                run_cemig: "Só CEMIG",
                run_cache: "Só cache",
            };
            const acao = nomes[data] || data;

            // Substitui a mensagem original pelo resultado
            const textoResultado = resultado.ok
                ? `✅ <b>Workflow disparado</b>\n\n▶️ ${acao}\n\n` +
                  `Acompanhe em: https://github.com/${GITHUB_REPO}/actions`
                : `❌ <b>Falha ao disparar</b>\n\n▶️ ${acao}\n\n` +
                  `Erro: <code>${resultado.erro}</code>`;

            await telegram("editMessageText", {
                chat_id: cb.message.chat.id,
                message_id: cb.message.message_id,
                text: textoResultado,
                parse_mode: "HTML",
                disable_web_page_preview: true,
                reply_markup: {
                    inline_keyboard: [[
                        { text: "🔄 Novo comando", callback_data: "menu_reset" }
                    ]],
                },
            });

            return new Response("ok");
        }

        // Botão que reabre o menu
        if (data === "menu_reset") {
            await telegram("answerCallbackQuery", {
                callback_query_id: cb.id,
                text: "Painel reaberto",
            });
            await telegram("editMessageText", {
                chat_id: cb.message.chat.id,
                message_id: cb.message.message_id,
                text: "<b>Painel de controle — Alerta CDV</b>\n\nEscolha o que deseja executar:",
                parse_mode: "HTML",
                reply_markup: {
                    inline_keyboard: [
                        [{ text: "▶️ Rodar tudo", callback_data: "run_all" }],
                        [
                            { text: "Só ANA",   callback_data: "run_ana" },
                            { text: "Só CEMIG", callback_data: "run_cemig" },
                        ],
                        [{ text: "Só cache", callback_data: "run_cache" }],
                    ],
                },
            });
            return new Response("ok");
        }

        // ================= Moderação (comportamento existente) =================
        const [acao, idStr] = data.split(":");
        const msgId = parseInt(idStr, 10);

        if (!msgId || isNaN(msgId)) {
            return new Response("callback inválido", { status: 400 });
        }

        let patch: Record<string, unknown> = {};
        let resposta = "";
        let sufixoMsg = "";

        switch (acao) {
            case "aprovar":
                patch = { status: "aprovado" };
                resposta = "✅ Aprovada";
                sufixoMsg = `\n\n✅ <b>Aprovada</b> por ${cb.from.first_name}`;
                break;
            case "rejeitar":
                patch = { status: "rejeitado" };
                resposta = "❌ Rejeitada";
                sufixoMsg = `\n\n❌ <b>Rejeitada</b> por ${cb.from.first_name}`;
                break;
            case "limpar":
                patch = { reportado: false, reportado_count: 0 };
                resposta = "Denúncias limpas";
                sufixoMsg = `\n\n<b>Denúncias limpas</b> por ${cb.from.first_name}`;
                break;
            default:
                return new Response("ação desconhecida", { status: 400 });
        }

        const res = await atualizarMensagem(msgId, patch);

        if (!res.ok) {
            await telegram("answerCallbackQuery", {
                callback_query_id: cb.id,
                text: `❌ Erro: ${res.erro}`,
                show_alert: true,
            });
            return new Response("erro", { status: 500 });
        }

        await telegram("answerCallbackQuery", {
            callback_query_id: cb.id,
            text: resposta,
        });

        await telegram("editMessageText", {
            chat_id: cb.message.chat.id,
            message_id: cb.message.message_id,
            text: (cb.message.text || "") + sufixoMsg,
            parse_mode: "HTML",
            disable_web_page_preview: true,
        });

        return new Response("ok");
    } catch (e) {
        console.error("Erro no webhook:", e);
        return new Response(`erro: ${(e as Error).message}`, { status: 500 });
    }
});
