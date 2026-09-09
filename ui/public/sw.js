// MailVerdict's service worker.
//
// Two jobs, and nothing else: raise a notification for a push message,
// and focus (or open) the app when that notification is clicked. It
// caches nothing -- offline support is a separate decision, and every
// "rebuild needs a restart" trap this application already has would be
// far worse layered under a cache this file does not manage.

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = {};
  }
  const title = payload.title || "MailVerdict";
  const options = {
    body: payload.body || undefined,
    tag: payload.tag || undefined,
    data: { url: payload.url || "/" },
    icon: "/icon-192.png",
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    (async () => {
      const clientsList = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of clientsList) {
        if ("focus" in client) {
          await client.focus();
          if ("navigate" in client) {
            try {
              await client.navigate(url);
            } catch {
              // Focusing the existing window is still most of the value.
            }
          }
          return;
        }
      }
      await self.clients.openWindow(url);
    })(),
  );
});
