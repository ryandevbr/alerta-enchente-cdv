
// Service Worker — Alerta Enchente CDV
// Cache básico + Push Notifications


const CACHE_NAME = 'alerta-cdv-v2';
const ASSETS_TO_CACHE = ['/', '/index_base.html', '/logo.png', '/manifest.json'];

// ---- Install ----
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(ASSETS_TO_CACHE).catch((err) => {
                console.warn('[SW] Falha no pré-cache:', err);
            });
        })
    );
    self.skipWaiting();
});

// ---- Activate ----
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
            );
        })
    );
    self.clients.claim();
});

// ---- Fetch ----
self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return;
    const url = new URL(req.url);
    if (url.origin !== self.location.origin) return;
    if (url.pathname.startsWith('/rest/') || url.pathname.startsWith('/functions/')) return;

    event.respondWith(
        fetch(req)
            .then((resp) => {
                if (resp.ok && resp.type === 'basic') {
                    const clone = resp.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(req, clone));
                }
                return resp;
            })
            .catch(() => caches.match(req).then((cached) => {
                if (cached) return cached;
                if (req.mode === 'navigate') return caches.match('/');
            }))
    );
});


// PUSH — recebe notificação enviada pelo backend

self.addEventListener('push', (event) => {
    let data = { title: 'Alerta CDV', body: 'Nova atualização.', url: '/', tag: 'alerta-cdv', icon: '/logo.png', badge: '/logo.png' };

    if (event.data) {
        try {
            data = { ...data, ...event.data.json() };
        } catch (err) {
            console.warn('[SW] Payload inválido:', err);
            data.body = event.data.text();
        }
    }

    const options = {
        body: data.body,
        icon: data.icon,
        badge: data.badge,
        tag: data.tag,
        requireInteraction: true,   // fica visível até o usuário interagir
        vibrate: [200, 100, 200],
        data: { url: data.url },
    };

    event.waitUntil(
        self.registration.showNotification(data.title, options)
    );
});


// NOTIFICATION CLICK — abre o site quando o usuário toca

self.addEventListener('notificationclick', (event) => {
    event.notification.close();

    const targetUrl = event.notification.data?.url || '/';

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then((clientList) => {
                // Se já tem aba aberta, foca nela
                for (const client of clientList) {
                    if (client.url.includes(self.location.origin)) {
                        return client.focus();
                    }
                }
                // Senão abre uma nova
                return clients.openWindow(targetUrl);
            })
    );
});
