=================
// Edge Function: send-push
// Envia notificações push para todas as subscriptions ativas.
//
// Body esperado:
//   { titulo: string, corpo: string, url?: string, tag?: string }
//
// Autenticação: header "X-Push-Secret" deve bater com o env PUSH_SECRET
=================

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import webpush from "npm:web-push@3.6.7";

const SUPABASE_URL      = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE      = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const VAPID_PUBLIC_KEY  = Deno.env.get("VAPID_PUBLIC_KEY")!;
const VAPID_PRIVATE_KEY = Deno.env.get("VAPID_PRIVATE_KEY")!;
const VAPID_SUBJECT     = Deno.env.get("VAPID_SUBJECT") || "mailto:admin@alertaenchentecdv.app";
const PUSH_SECRET       = Deno.env.get("PUSH_SECRET")!;

webpush.setVapidDetails(VAPID_SUBJECT, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY);

interface Payload {
    titulo: string;
    corpo: string;
    url?: string;
    tag?: string;
}

async function listarSubscriptions() {
    const r = await fetch(
        `${SUPABASE_URL}/rest/v1/push_subscriptions?ativo=eq.true&select=id,endpoint,p256dh,auth,falhas`,
        {
            headers: {
                "apikey": SERVICE_ROLE,
                "Authorization": `Bearer ${SERVICE_ROLE}`,
            },
        }
    );
    if (!r.ok) throw new Error(`Erro ao listar subscriptions: ${r.status} ${await r.text()}`);
    return await r.json();
}

async function marcarFalha(id: number, desativar: boolean) {
    const patch = desativar
        ? { ativo: false, falhas: 999, atualizado_em: new Date().toISOString() }
        : { falhas: 1, atualizado_em: new Date().toISOString() };

    await fetch(`${SUPABASE_URL}/rest/v1/push_subscriptions?id=eq.${id}`, {
        method: "PATCH",
        headers: {
            "apikey": SERVICE_ROLE,
            "Authorization": `Bearer ${SERVICE_ROLE}`,
            "Content-Type": "application/json",
        },
        body: JSON.stringify(patch),
    });
}

async function marcarSucesso(id: number) {
    await fetch(`${SUPABASE_URL}/rest/v1/push_subscriptions?id=eq.${id}`, {
        method: "PATCH",
        headers: {
            "apikey": SERVICE_ROLE,
            "Authorization": `Bearer ${SERVICE_ROLE}`,
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            falhas: 0,
            atualizado_em: new Date().toISOString(),
        }),
    });
}

serve(async (req) => {
    try {
        if (req.method !== "POST") {
            return new Response("Method not allowed", { status: 405 });
        }

        // ---------- AUTENTICAÇÃO ----------
        const secretRecebido = req.headers.get("X-Push-Secret") || "";

        // Se PUSH_SECRET não está configurado, retorna erro detalhado
        if (!PUSH_SECRET) {
            return new Response(JSON.stringify({
                erro: "PUSH_SECRET não configurado no servidor",
                dica: "Rode: supabase secrets set PUSH_SECRET=<valor> e faça redeploy",
            }), { status: 500, headers: { "Content-Type": "application/json" } });
        }

        // Se não bate, retorna diagnóstico (sem expor o secret inteiro)
        if (secretRecebido !== PUSH_SECRET) {
            return new Response(JSON.stringify({
                erro: "Secret inválido",
                debug: {
                    recebido_tamanho: secretRecebido.length,
                    esperado_tamanho: PUSH_SECRET.length,
                    recebido_inicio: secretRecebido.slice(0, 8),
                    esperado_inicio: PUSH_SECRET.slice(0, 8),
                    recebido_fim: secretRecebido.slice(-8),
                    esperado_fim: PUSH_SECRET.slice(-8),
                }
            }), { status: 401, headers: { "Content-Type": "application/json" } });
        }

        // ---------- PAYLOAD ----------
        const payload: Payload = await req.json();

        if (!payload.titulo || !payload.corpo) {
            return new Response("Campos 'titulo' e 'corpo' são obrigatórios", { status: 400 });
        }

        const pushBody = JSON.stringify({
            title: payload.titulo,
            body: payload.corpo,
            url: payload.url || "/",
            tag: payload.tag || "alerta-cdv",
            icon: "/logo.png",
            badge: "/logo.png",
            timestamp: Date.now(),
        });

        // ---------- ENVIO ----------
        const subs = await listarSubscriptions();
        console.log(`[PUSH] Enviando para ${subs.length} subscriptions...`);

        let sucessos = 0, falhas = 0, desativadas = 0;

        for (const sub of subs) {
            const pushSub = {
                endpoint: sub.endpoint,
                keys: { p256dh: sub.p256dh, auth: sub.auth },
            };

            try {
                await webpush.sendNotification(pushSub, pushBody);
                await marcarSucesso(sub.id);
                sucessos++;
            } catch (err: any) {
                falhas++;
                const status = err?.statusCode;

                if (status === 404 || status === 410) {
                    await marcarFalha(sub.id, true);
                    desativadas++;
                    console.log(`[PUSH] Subscription ${sub.id} desativada (${status})`);
                } else {
                    await marcarFalha(sub.id, false);
                    console.warn(`[PUSH] Falha ${sub.id}: ${status || err.message}`);
                }
            }
        }

        return new Response(JSON.stringify({
            ok: true,
            total: subs.length,
            sucessos,
            falhas,
            desativadas,
        }), {
            headers: { "Content-Type": "application/json" },
        });

    } catch (e) {
        console.error("[PUSH] Erro:", e);
        return new Response(`erro: ${(e as Error).message}`, { status: 500 });
    }
});
