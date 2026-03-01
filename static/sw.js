self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));

self.addEventListener('push', e => {
    if (!e.data) return;
    const d = e.data.json();
    e.waitUntil(self.registration.showNotification(d.title, {
        body: d.body,
        icon: '/static/icons/icon-192.png',
        badge: '/static/icons/icon-192.png',
        tag: d.tag || 'daily-digest',
        data: { url: d.url || '/contacts' }
    }));
});

self.addEventListener('notificationclick', e => {
    e.notification.close();
    const url = (e.notification.data && e.notification.data.url) || '/contacts';
    e.waitUntil(clients.openWindow(url));
});
