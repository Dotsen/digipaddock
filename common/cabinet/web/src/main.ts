import { mount } from "svelte";
import App from "./App.svelte";
import "./app.css";
import { watchInstall } from "./lib/install.svelte";
import { navigate } from "./lib/router.svelte";
import { safeAppPath } from "./lib/swroutes";
import { checkForNewWorker, reloadOnNewWorker } from "./lib/swupdate";

watchInstall();
reloadOnNewWorker(navigator.serviceWorker, () => location.reload());
checkForNewWorker(navigator.serviceWorker, document);

// a notification clicked while the app is open: the service worker asks this page to open its link
navigator.serviceWorker?.addEventListener("message", (event: MessageEvent) => {
  const url = event.data?.navigate;
  if (typeof url === "string") navigate(safeAppPath(url));
});

export default mount(App, { target: document.getElementById("app")! });
