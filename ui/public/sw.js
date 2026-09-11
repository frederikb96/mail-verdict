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

// An open window is asked to open the URL itself -- the same in-app path a
// click in the bell takes, so nothing reloads. A window that does not
// answer in time (an older build, one still loading) is navigated instead.
function askClientToOpen(client, url) {
  return new Promise((resolve) => {
    const channel = new MessageChannel();
    const timer = setTimeout(() => resolve(false), 1500);
    channel.port1.onmessage = () => {
      clearTimeout(timer);
      resolve(true);
    };
    client.postMessage({ type: "mailverdict:open-url", url }, [channel.port2]);
  });
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    (async () => {
      const clientsList = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      const client = clientsList.find((c) => c.focused) || clientsList[0];
      if (!client) {
        await self.clients.openWindow(url);
        return;
      }
      if ("focus" in client) {
        try {
          await client.focus();
        } catch {
          // Opening the message in it is still most of the value.
        }
      }
      if (await askClientToOpen(client, url)) return;
      if ("navigate" in client) {
        try {
          await client.navigate(url);
        } catch {
          // Focusing the existing window is still most of the value.
        }
      }
    })(),
  );
});
