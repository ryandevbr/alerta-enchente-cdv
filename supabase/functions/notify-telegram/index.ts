// supabase/functions/notify-telegram/index.ts
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const TELEGRAM_TOKEN   = Deno.env.get("TELEGRAM_TOKEN")!;
const TELEGRAM_CHAT_ID = Deno.env.get("TELEGRAM_CHAT_ID")!;

function esc(s: string): string {
    return String(s || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

serve(async (req) => {
    try {
        const body = await req.json();
        const record = body?.record;
        const old    = body?.old_record;

        if (!record) {
            return new Response("sem record", { status: 400 });
        }

        // Decide se deve notificar
        const ehNovoPendente  = record.status === "pendente";
        const ehAutoRejeitada = record.status === "rejeitado"
                             && (record.reportado_count || 0) >= 3
                             && old?.status !== "rejeitado";
        const ehReportada     = record.reportado === true
                             && record.status !== "rejeitado";

        if (!ehNovoPendente && !ehAutoRejeitada && !ehReportada) {
            return new Response("ignorado");
        }

        let cabecalho = "";
        if (ehNovoPendente) {
            cabecalho = "<b>Nova mensagem na fila</b>";
        } else if (ehAutoRejeitada) {
            cabecalho = "<b>Mensagem auto-rejeitada</b> (3+ denúncias)";
        } else if (ehReportada) {
            cabecalho = `<b>Mensagem denunciada</b> (${record.reportado_count || 1}x)`;
        }

        const texto =
            `${cabecalho}\n\n` +
            `<b>${esc(record.autor_nome || "Anônimo")}</b>` +
            `${record.autor_rua ? `, ${esc(record.autor_rua)}` : ""}\n\n` +
            `<i>${esc(record.mensagem || "")}</i>\n\n` +
            `🆔 ID: <code>${record.id}</code>`;

        // ---------- Botões ----------
        let botoes: Array<Array<{ text: string; callback_data: string }>> = [];

        if (ehNovoPendente) {
            botoes = [[
                { text: "Aprovar",  callback_data: `aprovar:${record.id}` },
                { text: "Rejeitar", callback_data: `rejeitar:${record.id}` },
            ]];
        } else if (ehAutoRejeitada) {
            botoes = [[
                { text: "↩Restaurar (aprovar)", callback_data: `aprovar:${record.id}` },
            ]];
        } else if (ehReportada) {
            botoes = [[
                { text: "⚠️ Rejeitar",       callback_data: `rejeitar:${record.id}` },
                { text: "Limpar denúncia", callback_data: `limpar:${record.id}` },
            ]];
        }

        // ---------- Payload ----------
        const payload: Record<string, unknown> = {
            chat_id: TELEGRAM_CHAT_ID,
            text: texto,
            parse_mode: "HTML",
            disable_web_page_preview: true,
        };

        if (botoes.length > 0) {
            payload.reply_markup = { inline_keyboard: botoes };
        }

        // Log para debug — aparecerá em `supabase functions logs`
        console.log("Payload enviado:", JSON.stringify(payload));

        // ---------- Envio ----------
        const r = await fetch(
            `https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            }
        );

        if (!r.ok) {
            const erro = await r.text();
            console.error("Telegram erro:", erro);
            return new Response(`telegram error: ${erro}`, { status: 502 });
        }

        return new Response("ok");
    } catch (e) {
        console.error("Erro:", e);
        return new Response(`erro: ${(e as Error).message}`, { status: 500 });
    }
});